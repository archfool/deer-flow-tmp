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
    StepStatus,
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
async def test_skill_registry_rejects_undeclared_output_keys() -> None:
    """Executable Skill 的输出白名单必须在运行时真实生效。"""

    async def invalid_handler(context):
        """返回契约未声明的字段以触发统一边界校验。"""

        del context
        return SkillExecutionResult(output={"allowed": 1, "leaked": 2})

    skills = SkillRegistry()
    skills.register(
        SkillDefinition(
            name="contracted",
            version="1",
            output_schema={
                "type": "object",
                "properties": {"allowed": {"type": "integer"}},
                "additionalProperties": False,
            },
        ),
        invalid_handler,
    )
    definition = TaskDefinition(
        name="contract-test",
        version="1",
        steps=(WorkflowStep(id="work", skill="contracted"),),
    )
    engine = WorkflowEngine(
        definitions=[definition],
        skills=skills,
        repository=InMemoryTaskRepository(),
    )
    task = await engine.start(task_name="contract-test", owner_id="agent-1")

    failed = await engine.advance(task.id, owner_id="agent-1")

    assert failed.status is TaskStatus.FAILED
    assert "undeclared output keys" in (failed.steps["work"].error or "")


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
async def test_engine_time_budget_stops_after_completed_wave_and_can_continue() -> None:
    """软时间预算只切分执行波次，不丢失已经完成的步骤。"""

    async def complete_step(context):
        """返回当前步骤 ID，便于断言波次边界。"""

        return SkillExecutionResult(output={"step": context.step.id})

    skills = SkillRegistry()
    skills.register(SkillDefinition(name="complete", version="1"), complete_step)
    definition = TaskDefinition(
        name="budgeted",
        version="1",
        steps=(
            WorkflowStep(id="first", skill="complete"),
            WorkflowStep(id="second", skill="complete", depends_on=("first",)),
        ),
    )
    engine = WorkflowEngine(
        definitions=[definition],
        skills=skills,
        repository=InMemoryTaskRepository(),
    )
    task = await engine.start(task_name="budgeted", owner_id="agent-1")

    partial = await engine.advance(
        task.id,
        owner_id="agent-1",
        time_budget_seconds=0,
    )
    assert partial.status is TaskStatus.BLOCKED
    assert partial.steps["first"].status is StepStatus.COMPLETED
    assert partial.steps["second"].status is StepStatus.PENDING

    completed = await engine.advance(task.id, owner_id="agent-1")
    assert completed.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_engine_recovers_running_step_left_by_interrupted_process() -> None:
    """显式恢复会重试进程中断后遗留的 RUNNING 步骤。"""

    async def complete_step(context):
        """返回被恢复步骤的执行结果。"""

        return SkillExecutionResult(output={"step": context.step.id})

    repository = InMemoryTaskRepository()
    skills = SkillRegistry()
    skills.register(SkillDefinition(name="complete", version="1"), complete_step)
    definition = TaskDefinition(
        name="recoverable",
        version="1",
        steps=(WorkflowStep(id="work", skill="complete"),),
    )
    engine = WorkflowEngine(
        definitions=[definition],
        skills=skills,
        repository=repository,
    )
    task = await engine.start(task_name="recoverable", owner_id="agent-1")
    orphaned = await repository.get(task.id, owner_id="agent-1")
    assert orphaned is not None
    orphaned.status = TaskStatus.RUNNING
    orphaned.steps["work"].status = StepStatus.RUNNING
    await repository.save(orphaned, expected_revision=orphaned.revision)

    completed = await engine.advance(
        task.id,
        owner_id="agent-1",
        recover_interrupted=True,
    )
    assert completed.status is TaskStatus.COMPLETED
    assert completed.steps["work"].attempts == 1


@pytest.mark.asyncio
async def test_engine_cancellation_restores_current_wave_for_retry() -> None:
    """请求取消时当前执行波恢复为 PENDING，避免任务永久卡住。"""

    started = asyncio.Event()
    attempts = 0

    async def cancellable_step(context):
        """首次执行等待取消，第二次执行正常完成。"""

        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await asyncio.Event().wait()
        return SkillExecutionResult(output={"step": context.step.id})

    skills = SkillRegistry()
    skills.register(SkillDefinition(name="cancellable", version="1"), cancellable_step)
    definition = TaskDefinition(
        name="cancel-retry",
        version="1",
        steps=(WorkflowStep(id="work", skill="cancellable"),),
    )
    engine = WorkflowEngine(
        definitions=[definition],
        skills=skills,
        repository=InMemoryTaskRepository(),
    )
    task = await engine.start(task_name="cancel-retry", owner_id="agent-1")
    advancing = asyncio.create_task(engine.advance(task.id, owner_id="agent-1"))
    await started.wait()
    advancing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await advancing

    interrupted = await engine.get(task.id, owner_id="agent-1")
    assert interrupted is not None
    assert interrupted.steps["work"].status is StepStatus.PENDING

    completed = await engine.advance(task.id, owner_id="agent-1")
    assert completed.status is TaskStatus.COMPLETED
    assert completed.steps["work"].attempts == 2


@pytest.mark.asyncio
async def test_engine_retries_failed_skill_until_declared_attempt_limit() -> None:
    """Skill 瞬时失败时按声明次数跨推进轮次重试。"""

    attempts = 0

    async def transient_step(context):
        """首次失败，第二次返回正常结果。"""

        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient model failure")
        return SkillExecutionResult(output={"step": context.step.id})

    skills = SkillRegistry()
    skills.register(
        SkillDefinition(
            name="transient",
            version="1",
            max_attempts=2,
        ),
        transient_step,
    )
    definition = TaskDefinition(
        name="retryable",
        version="1",
        steps=(WorkflowStep(id="work", skill="transient"),),
    )
    engine = WorkflowEngine(
        definitions=[definition],
        skills=skills,
        repository=InMemoryTaskRepository(),
    )
    task = await engine.start(task_name="retryable", owner_id="agent-1")

    retryable = await engine.advance(task.id, owner_id="agent-1")
    assert retryable.status is TaskStatus.BLOCKED
    assert retryable.steps["work"].status is StepStatus.PENDING
    assert retryable.steps["work"].attempts == 1

    completed = await engine.advance(task.id, owner_id="agent-1")
    assert completed.status is TaskStatus.COMPLETED
    assert completed.steps["work"].attempts == 2


@pytest.mark.asyncio
async def test_skill_registry_enforces_declared_timeout() -> None:
    """异步 Skill 超过声明时限时必须形成可审计失败。"""

    async def slow_step(_context):
        """等待时间超过 Skill 超时。"""

        await asyncio.sleep(0.05)
        return SkillExecutionResult(output={"done": True})

    skills = SkillRegistry()
    skills.register(
        SkillDefinition(
            name="slow",
            version="1",
            timeout_seconds=0.01,
        ),
        slow_step,
    )
    definition = TaskDefinition(
        name="timeout-task",
        version="1",
        steps=(WorkflowStep(id="work", skill="slow"),),
    )
    engine = WorkflowEngine(
        definitions=[definition],
        skills=skills,
        repository=InMemoryTaskRepository(),
    )
    task = await engine.start(
        task_name="timeout-task",
        owner_id="agent-1",
    )

    failed = await engine.advance(task.id, owner_id="agent-1")

    assert failed.status is TaskStatus.FAILED
    assert failed.steps["work"].status is StepStatus.FAILED
    assert failed.steps["work"].error is not None
    assert failed.steps["work"].error.startswith("TimeoutError:")


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
