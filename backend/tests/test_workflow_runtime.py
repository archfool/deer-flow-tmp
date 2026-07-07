"""可复用工作流运行时的契约测试。

保险任务层依赖这些行为，但测试刻意保持领域无关。工作流是一组可持久化的步骤
状态；每个对话轮次只推进已经就绪的步骤。这样主 Agent 可以离开一个任务、处理
另一个任务，再恢复第一个任务，而无需把对话交给长期运行的子 Agent。
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from deerflow.workflows import (
    InMemoryTaskRepository,
    InputRequest,
    SkillDefinition,
    SkillExecutionResult,
    SkillRegistry,
    TaskDefinition,
    TaskStatus,
    WorkflowEngine,
    WorkflowStep,
)


def test_task_definition_rejects_dependency_cycles() -> None:
    """定义属于配置，因此非法图必须在加载时失败。"""

    with pytest.raises(ValidationError, match="cycle"):
        TaskDefinition(
            name="cyclic",
            version="1",
            steps=(
                WorkflowStep(id="a", skill="skill-a", depends_on=("b",)),
                WorkflowStep(id="b", skill="skill-b", depends_on=("a",)),
            ),
        )


@pytest.mark.asyncio
async def test_engine_runs_independent_steps_in_parallel_and_joins_results() -> None:
    """已就绪的同级步骤并行执行，依赖步骤等待二者全部完成。"""

    active = 0
    maximum_active = 0

    async def parallel_handler(context):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        # 主动让出控制权，确保同级 Handler 能在当前 Handler 退出前启动。
        await asyncio.sleep(0.01)
        active -= 1
        return SkillExecutionResult(output={"step": context.step.id})

    async def join_handler(context):
        return SkillExecutionResult(output={"joined": sorted(value["step"] for value in context.dependency_outputs.values())})

    skills = SkillRegistry()
    skills.register(SkillDefinition(name="parallel", version="1"), parallel_handler)
    skills.register(SkillDefinition(name="join", version="1"), join_handler)

    definition = TaskDefinition(
        name="parallel-task",
        version="1",
        steps=(
            WorkflowStep(id="left", skill="parallel"),
            WorkflowStep(id="right", skill="parallel"),
            WorkflowStep(id="join", skill="join", depends_on=("left", "right")),
        ),
    )
    engine = WorkflowEngine(
        definitions=[definition],
        skills=skills,
        repository=InMemoryTaskRepository(),
    )

    task = await engine.start(
        task_name="parallel-task",
        owner_id="agent-1",
        subject_id="customer-1",
    )
    completed = await engine.advance(task.id, owner_id="agent-1")

    assert completed.status is TaskStatus.COMPLETED
    assert maximum_active == 2
    assert completed.steps["join"].output == {"joined": ["left", "right"]}


@pytest.mark.asyncio
async def test_waiting_task_can_be_suspended_resumed_and_supplied_input() -> None:
    """等待人工输入的任务不会接管或锁死对话。"""

    async def needs_age(context):
        age = context.input_data.get("age")
        if age is None:
            return SkillExecutionResult(
                input_requests=(
                    InputRequest(
                        path="/age",
                        prompt="请提供年龄",
                        reason="年龄是该计算不可替代的客户事实",
                    ),
                )
            )
        return SkillExecutionResult(output={"age": age})

    skills = SkillRegistry()
    skills.register(SkillDefinition(name="needs-age", version="1"), needs_age)
    definition = TaskDefinition(
        name="resumable",
        version="1",
        steps=(WorkflowStep(id="collect-age", skill="needs-age"),),
    )
    engine = WorkflowEngine(
        definitions=[definition],
        skills=skills,
        repository=InMemoryTaskRepository(),
    )

    task = await engine.start(
        task_name="resumable",
        owner_id="agent-1",
        subject_id="customer-1",
    )
    waiting = await engine.advance(task.id, owner_id="agent-1")
    assert waiting.status is TaskStatus.WAITING_INPUT
    assert waiting.pending_inputs[0].path == "/age"

    suspended = await engine.suspend(task.id, owner_id="agent-1", reason="临时处理另一项任务")
    assert suspended.status is TaskStatus.SUSPENDED

    resumed = await engine.resume(task.id, owner_id="agent-1")
    assert resumed.status is TaskStatus.READY

    completed = await engine.advance(task.id, owner_id="agent-1", input_patch={"age": 38})
    assert completed.status is TaskStatus.COMPLETED
    assert completed.steps["collect-age"].output == {"age": 38}


@pytest.mark.asyncio
async def test_repository_enforces_owner_isolation() -> None:
    """仅知道任务 ID 不足以访问其他用户的任务。"""

    definition = TaskDefinition(name="empty", version="1", steps=())
    engine = WorkflowEngine(
        definitions=[definition],
        skills=SkillRegistry(),
        repository=InMemoryTaskRepository(),
    )
    task = await engine.start(task_name="empty", owner_id="agent-1", subject_id="customer-1")

    assert await engine.get(task.id, owner_id="agent-2") is None
