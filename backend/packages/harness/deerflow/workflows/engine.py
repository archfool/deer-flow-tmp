"""以确定性方式推进持久化工作流任务实例。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from deerflow.workflows.models import (
    SkillExecutionContext,
    StepStatus,
    TaskInstance,
    TaskStatus,
    WorkflowStep,
    utc_now,
)
from deerflow.workflows.registry import DefinitionRegistry, SkillRegistry
from deerflow.workflows.repository import TaskRepository


class WorkflowStateError(RuntimeError):
    """请求的状态迁移与当前状态不兼容时抛出。"""


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """递归合并本轮输入，同时保留同级已有数据。"""

    merged = deepcopy(target)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


class WorkflowEngine:
    """以有界、可持久化的执行波次推进任务 DAG。"""

    def __init__(
        self,
        *,
        definitions: list,
        skills: SkillRegistry,
        repository: TaskRepository,
    ) -> None:
        self._definitions = DefinitionRegistry(definitions)
        self._skills = skills
        self._repository = repository

    async def start(
        self,
        *,
        task_name: str,
        owner_id: str,
        subject_id: str | None = None,
        thread_id: str | None = None,
        parent_task_id: str | None = None,
        input_data: dict[str, Any] | None = None,
        definition_version: str | None = None,
    ) -> TaskInstance:
        definition = self._definitions.get(task_name, definition_version)
        task = TaskInstance.from_definition(
            definition,
            owner_id=owner_id,
            subject_id=subject_id,
            thread_id=thread_id,
            parent_task_id=parent_task_id,
            input_data=input_data,
        )
        return await self._repository.create(task)

    async def get(self, task_id: str, *, owner_id: str) -> TaskInstance | None:
        return await self._repository.get(task_id, owner_id=owner_id)

    async def list(
        self,
        *,
        owner_id: str,
        subject_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[TaskInstance]:
        return await self._repository.list(owner_id=owner_id, subject_id=subject_id, thread_id=thread_id)

    async def _load_required(self, task_id: str, owner_id: str) -> TaskInstance:
        task = await self._repository.get(task_id, owner_id=owner_id)
        if task is None:
            # 对“不存在”和“所有者不匹配”使用相同错误，防止调用方枚举
            # 其他用户的任务。
            raise KeyError(f"task not found: {task_id}")
        return task

    async def _save(self, task: TaskInstance) -> TaskInstance:
        task.updated_at = utc_now()
        return await self._repository.save(task, expected_revision=task.revision)

    @staticmethod
    def _dependency_outputs(task: TaskInstance, step: WorkflowStep) -> dict[str, dict[str, Any]]:
        return {dependency: deepcopy(task.steps[dependency].output) for dependency in step.depends_on}

    @staticmethod
    def _ready_steps(task: TaskInstance, definition, attempted: set[str]) -> list[WorkflowStep]:
        ready: list[WorkflowStep] = []
        for step in definition.steps:
            state = task.steps[step.id]
            if step.id in attempted or state.status not in {StepStatus.PENDING, StepStatus.WAITING_INPUT}:
                continue
            if all(task.steps[dependency].status is StepStatus.COMPLETED for dependency in step.depends_on):
                ready.append(step)
        return ready

    @staticmethod
    def _refresh_summary_status(task: TaskInstance) -> None:
        states = list(task.steps.values())
        if not states or all(state.status in {StepStatus.COMPLETED, StepStatus.SKIPPED} for state in states):
            task.status = TaskStatus.COMPLETED
            task.pending_inputs = ()
            return
        if any(state.status is StepStatus.FAILED for state in states):
            task.status = TaskStatus.FAILED
            return
        if any(state.status is StepStatus.RUNNING for state in states):
            task.status = TaskStatus.RUNNING
            return
        if any(state.status is StepStatus.WAITING_CONFIRMATION for state in states):
            task.status = TaskStatus.WAITING_CONFIRMATION
            return
        waiting = [request for state in states for request in state.input_requests if state.status is StepStatus.WAITING_INPUT]
        if waiting:
            # 多个并行维度 Skill 可能请求同一个客户事实。按路径去重，并保持
            # 确定性的输出顺序。
            deduplicated = {request.path: request for request in waiting}
            task.pending_inputs = tuple(deduplicated[path] for path in sorted(deduplicated))
            task.status = TaskStatus.WAITING_INPUT
            return
        task.status = TaskStatus.BLOCKED

    async def advance(
        self,
        task_id: str,
        *,
        owner_id: str,
        input_patch: dict[str, Any] | None = None,
        confirmations: dict[str, bool] | None = None,
        max_waves: int = 32,
    ) -> TaskInstance:
        """推进当前所有可达步骤，直到遇到稳定屏障。

        虽然 `TaskDefinition` 已拒绝静态循环，但 `max_waves` 仍可防止错误的
        动态注册工作流无限占用一个对话轮次。
        """

        task = await self._load_required(task_id, owner_id)
        if task.status.terminal:
            return task
        if task.status is TaskStatus.SUSPENDED:
            raise WorkflowStateError("suspended task must be resumed before it can advance")

        if input_patch:
            task.input_data = _deep_merge(task.input_data, input_patch)
            # 新回答可能与所有等待中的 Skill 有关，因此把它们恢复为可重试状态；
            # 在新结果返回前，旧的部分输出仍保留给 Handler 和审计序列化使用。
            for state in task.steps.values():
                if state.status is StepStatus.WAITING_INPUT:
                    state.input_requests = ()
                    state.status = StepStatus.PENDING

        for step_id, approved in (confirmations or {}).items():
            state = task.steps.get(step_id)
            if state is None or state.status is not StepStatus.WAITING_CONFIRMATION:
                raise WorkflowStateError(f"step is not awaiting confirmation: {step_id}")
            if approved:
                state.status = StepStatus.COMPLETED
                task.output_data[step_id] = deepcopy(state.output)
            else:
                state.status = StepStatus.FAILED
                state.error = "user rejected confirmation"
            state.confirmation_request = None
            state.updated_at = utc_now()

        task.status = TaskStatus.RUNNING
        task.pending_inputs = ()
        task = await self._save(task)
        definition = self._definitions.get(task.task_name, task.definition_version)
        attempted: set[str] = set()

        for _wave in range(max_waves):
            ready = self._ready_steps(task, definition, attempted)
            if not ready:
                break

            contexts: list[SkillExecutionContext] = []
            for step in ready:
                state = task.steps[step.id]
                state.status = StepStatus.RUNNING
                state.attempts += 1
                state.started_at = state.started_at or utc_now()
                state.updated_at = utc_now()
                attempted.add(step.id)
                contexts.append(
                    SkillExecutionContext(
                        task=task.model_copy(deep=True),
                        step=step,
                        input_data=deepcopy(task.input_data),
                        dependency_outputs=self._dependency_outputs(task, step),
                    )
                )

            # 调用外部代码前先持久化 RUNNING。即使进程崩溃，也会留下可观察状态，
            # 后续恢复任务可以进行对账，而不是误认为步骤从未开始。
            task = await self._save(task)
            results = await asyncio.gather(
                *(self._skills.invoke(context) for context in contexts),
                return_exceptions=True,
            )

            for step, result in zip(ready, results, strict=True):
                state = task.steps[step.id]
                state.updated_at = utc_now()
                if isinstance(result, BaseException):
                    state.status = StepStatus.FAILED
                    state.error = f"{type(result).__name__}: {result}"
                    task.error = f"step {step.id} failed: {result}"
                    continue

                state.output = deepcopy(result.output)
                state.error = None
                if result.input_requests:
                    state.status = StepStatus.WAITING_INPUT
                    state.input_requests = result.input_requests
                elif result.confirmation_request is not None:
                    state.status = StepStatus.WAITING_CONFIRMATION
                    state.confirmation_request = result.confirmation_request
                else:
                    state.status = StepStatus.COMPLETED
                    state.input_requests = ()
                    state.confirmation_request = None
                    task.output_data[step.id] = deepcopy(result.output)

            self._refresh_summary_status(task)
            task = await self._save(task)
            if task.status in {TaskStatus.WAITING_CONFIRMATION, TaskStatus.FAILED, TaskStatus.COMPLETED}:
                break

        self._refresh_summary_status(task)
        return await self._save(task)

    async def suspend(self, task_id: str, *, owner_id: str, reason: str = "") -> TaskInstance:
        task = await self._load_required(task_id, owner_id)
        if task.status.terminal:
            raise WorkflowStateError("terminal task cannot be suspended")
        task.status = TaskStatus.SUSPENDED
        task.suspension_reason = reason
        return await self._save(task)

    async def resume(self, task_id: str, *, owner_id: str) -> TaskInstance:
        task = await self._load_required(task_id, owner_id)
        if task.status is not TaskStatus.SUSPENDED:
            raise WorkflowStateError("only a suspended task can be resumed")
        task.status = TaskStatus.READY
        task.suspension_reason = None
        return await self._save(task)

    async def cancel(self, task_id: str, *, owner_id: str, reason: str = "") -> TaskInstance:
        task = await self._load_required(task_id, owner_id)
        if task.status.terminal:
            return task
        task.status = TaskStatus.CANCELLED
        task.error = reason or None
        return await self._save(task)
