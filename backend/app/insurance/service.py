"""协调客户档案、Adapter 与工作流任务的应用服务。"""

from __future__ import annotations

from typing import Any

from app.insurance.adapters import CustomerDataAdapter, MockCustomerDataAdapter
from app.insurance.coverage_review import (
    COVERAGE_REVIEW_TASK_NAME,
    build_coverage_review_skill_registry,
    build_coverage_review_task_definition,
    build_draft_parameters,
)
from app.insurance.profile import CustomerProfileService
from deerflow.subject_memory import MemoryUpdateSource, SubjectMemoryRepository
from deerflow.workflows import TaskInstance, TaskRepository, WorkflowEngine


class InsuranceService:
    """供 HTTP Route 和模型可见 Tool 使用的窄接口。

    所有者解析保留在本类之外，使授权边界清晰且易于测试：每个方法都要求传入
    已认证的所有者 ID，并把它同时传递给客户档案与任务 Repository。
    """

    def __init__(
        self,
        *,
        task_repository: TaskRepository,
        memory_repository: SubjectMemoryRepository,
        data_adapter: CustomerDataAdapter | None = None,
    ) -> None:
        self.profiles = CustomerProfileService(memory_repository)
        self._task_repository = task_repository
        self._data_adapter = data_adapter or MockCustomerDataAdapter()

    def _engine(self) -> WorkflowEngine:
        return WorkflowEngine(
            definitions=[build_coverage_review_task_definition()],
            skills=build_coverage_review_skill_registry(),
            repository=self._task_repository,
        )

    async def start_coverage_review(
        self,
        *,
        owner_id: str,
        customer_id: str,
        thread_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> TaskInstance:
        profile_and_version = await self.profiles.get(owner_id=owner_id, customer_id=customer_id)
        if profile_and_version is None:
            raise KeyError(f"customer profile not found: {customer_id}")
        profile, version = profile_and_version

        # 当前 Mock 不返回任何变更。真实 Adapter 应在生成不可变快照前，
        # 通过 CustomerProfileService 以 AUTHORITATIVE_API 来源提交转换后的 Patch。
        await self._data_adapter.fetch_profile_patch(owner_id=owner_id, customer_id=customer_id)
        parameters = build_draft_parameters()
        engine = self._engine()
        task = await engine.start(
            task_name=COVERAGE_REVIEW_TASK_NAME,
            owner_id=owner_id,
            subject_id=customer_id,
            thread_id=thread_id,
            parent_task_id=parent_task_id,
            input_data={
                "profile": profile.model_dump(mode="json"),
                "profile_version": version,
                "parameters": parameters.model_dump(mode="json"),
            },
        )
        return await engine.advance(task.id, owner_id=owner_id)

    async def advance_task(self, task_id: str, *, owner_id: str) -> TaskInstance:
        engine = self._engine()
        task = await engine.get(task_id, owner_id=owner_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task.subject_id is None:
            raise ValueError("coverage-review task has no bound customer")
        profile_and_version = await self.profiles.get(owner_id=owner_id, customer_id=task.subject_id)
        if profile_and_version is None:
            raise KeyError(f"customer profile not found: {task.subject_id}")
        profile, version = profile_and_version
        return await engine.advance(
            task_id,
            owner_id=owner_id,
            input_patch={"profile": profile.model_dump(mode="json"), "profile_version": version},
        )

    async def get_task(self, task_id: str, *, owner_id: str) -> TaskInstance | None:
        return await self._engine().get(task_id, owner_id=owner_id)

    async def list_tasks(
        self,
        *,
        owner_id: str,
        customer_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[TaskInstance]:
        return await self._engine().list(owner_id=owner_id, subject_id=customer_id, thread_id=thread_id)

    async def suspend_task(self, task_id: str, *, owner_id: str, reason: str = "") -> TaskInstance:
        return await self._engine().suspend(task_id, owner_id=owner_id, reason=reason)

    async def resume_task(self, task_id: str, *, owner_id: str) -> TaskInstance:
        return await self._engine().resume(task_id, owner_id=owner_id)

    async def cancel_task(self, task_id: str, *, owner_id: str, reason: str = "") -> TaskInstance:
        return await self._engine().cancel(task_id, owner_id=owner_id, reason=reason)

    async def update_profile_and_advance(
        self,
        *,
        owner_id: str,
        task_id: str,
        customer_id: str,
        expected_version: int,
        patch: dict[str, Any],
        inferred: bool,
        reason: str = "",
        thread_id: str | None = None,
    ) -> tuple[TaskInstance | None, Any]:
        source = MemoryUpdateSource.MODEL_INFERENCE if inferred else MemoryUpdateSource.USER_EXPLICIT
        profile, version, candidate = await self.profiles.update(
            owner_id=owner_id,
            customer_id=customer_id,
            expected_version=expected_version,
            patch=patch,
            source=source,
            reason=reason,
            thread_id=thread_id,
            task_id=task_id,
        )
        if candidate is not None:
            return None, candidate
        return await self.advance_task(task_id, owner_id=owner_id), {"profile": profile, "version": version}
