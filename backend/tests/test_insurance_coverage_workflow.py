"""可恢复保障检视任务的端到端内存契约测试。"""

from __future__ import annotations

import pytest

from app.insurance.mock_data import build_complete_mock_profile, build_incomplete_mock_profile
from app.insurance.service import InsuranceService
from deerflow.subject_memory import InMemorySubjectMemoryRepository, MemoryUpdateSource
from deerflow.workflows import InMemoryTaskRepository, TaskStatus


def _service() -> InsuranceService:
    return InsuranceService(
        task_repository=InMemoryTaskRepository(),
        memory_repository=InMemorySubjectMemoryRepository(),
    )


@pytest.mark.asyncio
async def test_complete_profile_runs_parallel_dimensions_and_both_reports() -> None:
    service = _service()
    profile, _version = await service.profiles.create(
        owner_id="agent-a",
        profile=build_complete_mock_profile(),
    )

    task = await service.start_coverage_review(
        owner_id="agent-a",
        customer_id=profile.customer_id,
        thread_id="thread-1",
    )

    assert task.status is TaskStatus.COMPLETED
    assert task.steps["customer-report"].output["report_type"] == "customer"
    assert task.steps["internal-report"].output["report_type"] == "internal"
    assert "客户顾虑与心理画像" not in task.steps["customer-report"].output["markdown"]
    assert "客户顾虑与心理画像" in task.steps["internal-report"].output["markdown"]


@pytest.mark.asyncio
async def test_missing_fact_waits_then_profile_update_resumes_same_task() -> None:
    service = _service()
    profile, version = await service.profiles.create(
        owner_id="agent-a",
        profile=build_incomplete_mock_profile(),
    )
    task = await service.start_coverage_review(
        owner_id="agent-a",
        customer_id=profile.customer_id,
    )

    assert task.status is TaskStatus.WAITING_INPUT
    assert "/financial/liabilities" in {request.path for request in task.pending_inputs}

    updated, updated_version, candidate = await service.profiles.update(
        owner_id="agent-a",
        customer_id=profile.customer_id,
        expected_version=version,
        # 空列表表示明确确认没有负债，与此前表示未知的 `None` 有意保持不同。
        patch={"/financial/liabilities": []},
        source=MemoryUpdateSource.USER_EXPLICIT,
        reason="用户确认当前无负债",
        task_id=task.id,
    )
    assert updated is not None
    assert updated_version == version + 1
    assert candidate is None

    completed = await service.advance_task(task.id, owner_id="agent-a")
    assert completed.status is TaskStatus.COMPLETED
    assert completed.input_data["profile_version"] == updated_version


@pytest.mark.asyncio
async def test_temporary_child_task_does_not_destroy_suspended_parent() -> None:
    service = _service()
    waiting_profile, _ = await service.profiles.create(
        owner_id="agent-a",
        profile=build_incomplete_mock_profile(),
    )
    complete_profile = build_complete_mock_profile()
    complete_profile.customer_id = "mock-child-task-customer"
    await service.profiles.create(owner_id="agent-a", profile=complete_profile)

    parent = await service.start_coverage_review(
        owner_id="agent-a",
        customer_id=waiting_profile.customer_id,
        thread_id="thread-1",
    )
    suspended = await service.suspend_task(
        parent.id,
        owner_id="agent-a",
        reason="临时处理另一个客户任务",
    )
    child = await service.start_coverage_review(
        owner_id="agent-a",
        customer_id=complete_profile.customer_id,
        thread_id="thread-1",
        parent_task_id=parent.id,
    )

    assert suspended.status is TaskStatus.SUSPENDED
    assert child.status is TaskStatus.COMPLETED
    assert child.parent_task_id == parent.id

    resumed = await service.resume_task(parent.id, owner_id="agent-a")
    assert resumed.status is TaskStatus.READY
    assert resumed.steps["analyze-life"].input_requests
