"""企业级保障检视 DeerFlow 工作流端到端测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.insurance.coverage_review import ReviewAction, ReviewDecision
from app.insurance.mock_data import (
    build_complete_mock_profile,
    build_incomplete_mock_profile,
)
from app.insurance.service import InsuranceService
from deerflow.subject_memory import InMemorySubjectMemoryRepository
from deerflow.workflows import (
    InMemoryTaskRepository,
    TaskStatus,
    WorkflowStateError,
)


def _run(value: Any) -> Any:
    """执行异步领域操作并返回结果。"""

    return asyncio.run(value)


def _service() -> InsuranceService:
    """构造完全内存化、使用 Mock Tool 的保险服务。"""

    return InsuranceService(
        task_repository=InMemoryTaskRepository(),
        memory_repository=InMemorySubjectMemoryRepository(),
    )


def test_complete_profile_stops_at_review_then_generates_customer_html_after_approval() -> None:
    """完整客户必须先出对内报告并等待批准，批准后才生成 HTML。"""

    service = _service()
    profile, _version = _run(
        service.profiles.create(
            owner_id="agent-a",
            profile=build_complete_mock_profile(),
        )
    )

    waiting = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=profile.customer_id,
            thread_id="thread-1",
            trigger_ids=["A2"],
        )
    )

    assert waiting.status is TaskStatus.WAITING_CONFIRMATION
    assert waiting.steps["internal-report"].status.value == "completed"
    assert waiting.steps["agent-review"].status.value == "waiting_confirmation"
    assert waiting.steps["customer-report"].status.value == "pending"
    internal = waiting.steps["internal-report"].output["internal_report"]
    runtime_context = waiting.input_data["runtime_context"]
    assert runtime_context["context_hash"].startswith("sha256:")
    assert internal["audit_manifest"]["runtime_context"] == runtime_context
    assert set(internal["audit_manifest"]["generation_audits"]) == {
        "customer-analysis",
        "verbalize-5-2",
        "verbalize-5-3",
        "meeting-support",
    }
    assert "## 二、测算明细与关键假设" in internal["markdown"]
    assert "### 八维测算解读：为什么是这个结果" in internal["markdown"]
    assert "伤残保障" in internal["markdown"]
    assert internal["audit_manifest"]["why_blocks"]
    assert internal["audit_manifest"]["action_block"]
    assert all(
        token not in internal["markdown"]
        for token in (
            "Knowledge Skill",
            "reason_code",
            "framework_code",
            "Kernel Hash",
            "EMERGENCY_LIQUIDITY_ALERT",
            "WEALTH_CERTAINTY",
            "structure_misallocation",
            "review_scope",
            "customer_goal",
            "recent_concern",
            "risk_attitude",
        )
    )
    assert "职业为企业管理" in internal["markdown"]

    completed = _run(
        service.decide_coverage_review(
            waiting.id,
            owner_id="agent-a",
            decision=ReviewDecision(
                action=ReviewAction.APPROVE,
                reviewer_id="agent-a",
                expected_review_revision=1,
                expected_kernel_hash=waiting.steps["freeze-kernel"].output["kernel"]["kernel_hash"],
                note="事实和报告已确认",
            ),
        )
    )

    assert completed.status is TaskStatus.COMPLETED
    customer = completed.steps["customer-report"].output["customer_report"]
    assert "<!doctype html>" in customer["html"]
    assert customer["manifest"]["approval_record"]["reviewer_id"] == "agent-a"
    assert customer["manifest"]["runtime_context"] == runtime_context
    assert customer["manifest"]["generation_audits"]["customer-copy"]["runtime_context_hash"] == runtime_context["context_hash"]
    assert customer["manifest"]["knowledge_refs"] == internal["meeting_plan"]["knowledge_refs"]
    assert customer["kernel_hash"] == completed.steps["freeze-kernel"].output["kernel"]["kernel_hash"]
    assert "具体产品、投保保额、缴费方案或收益承诺" in customer["html"]
    assert "165 万元" in customer["html"]
    assert "基础及扩展医疗责任" in customer["html"]
    assert "层责任" not in customer["html"]
    assert "责任层" not in customer["html"]
    assert "家庭财务摘要" in customer["html"]
    assert "未来检视安排" in customer["html"]
    assert "治疗与康复费用基础" in customer["html"]
    assert "CNY_PER_YEAR" not in customer["html"]
    assert "responsibility_tier" not in customer["html"]
    assert "reason_code" not in customer["html"]
    assert "WEALTH_CERTAINTY" not in customer["html"]
    assert "structure_misallocation" not in customer["html"]
    assert "review_scope" not in customer["html"]
    assert len(customer["view_model"]["priority_actions"]) == (len(internal["action_block"]["actions"]) + len(internal["action_block"]["maintain_items"]))
    assert all(mode != "fallback" for mode in customer["view_model"]["generation_modes"].values())


def test_runtime_context_tampering_blocks_resume() -> None:
    """任务级模型或规则上下文被篡改后不得继续推进。"""

    repository = InMemoryTaskRepository()
    service = InsuranceService(
        task_repository=repository,
        memory_repository=InMemorySubjectMemoryRepository(),
    )
    profile, _version = _run(
        service.profiles.create(
            owner_id="agent-a",
            profile=build_complete_mock_profile(),
        )
    )
    waiting = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=profile.customer_id,
        )
    )
    persisted = _run(repository.get(waiting.id, owner_id="agent-a"))
    assert persisted is not None
    persisted.input_data["runtime_context"]["model_name"] = "tampered-model"
    _run(repository.save(persisted, expected_revision=persisted.revision))

    with pytest.raises(WorkflowStateError, match="runtime context hash mismatch"):
        _run(
            service.advance_task(
                waiting.id,
                owner_id="agent-a",
                trigger_ids=["A2"],
            )
        )


def test_missing_trigger_waits_for_agent_then_resumes_same_task() -> None:
    """缺少触发场景时 Workflow 应中断并在补充后继续。"""

    service = _service()
    profile, _version = _run(
        service.profiles.create(
            owner_id="agent-a",
            profile=build_complete_mock_profile(),
        )
    )

    waiting = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=profile.customer_id,
        )
    )

    assert waiting.status is TaskStatus.WAITING_INPUT
    assert {item.path for item in waiting.pending_inputs} == {"/trigger_ids"}

    resumed = _run(
        service.advance_task(
            waiting.id,
            owner_id="agent-a",
            trigger_ids=["D2"],
        )
    )

    assert resumed.id == waiting.id
    assert resumed.status is TaskStatus.WAITING_CONFIRMATION
    assert resumed.steps["resolve-triggers"].output["trigger_binding"]["primary_trigger_id"] == "D2"


def test_stale_kernel_hash_cannot_approve_customer_report() -> None:
    """代理人看到的内核已过期时必须拒绝批准。"""

    service = _service()
    profile, _version = _run(
        service.profiles.create(
            owner_id="agent-a",
            profile=build_complete_mock_profile(),
        )
    )
    waiting = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=profile.customer_id,
            trigger_ids=["A2"],
        )
    )

    with pytest.raises(ValueError, match="stale"):
        _run(
            service.decide_coverage_review(
                waiting.id,
                owner_id="agent-a",
                decision=ReviewDecision(
                    action=ReviewAction.APPROVE,
                    reviewer_id="agent-a",
                    expected_review_revision=1,
                    expected_kernel_hash="sha256:stale-kernel",
                ),
            )
        )

    current = _run(service.get_task(waiting.id, owner_id="agent-a"))
    assert current is not None
    assert current.status is TaskStatus.WAITING_CONFIRMATION


def test_reviewer_identity_is_bound_and_rejection_never_generates_customer_html() -> None:
    """复核人必须来自认证上下文，拒绝后不得生成对客产物。"""

    service = _service()
    profile, _version = _run(
        service.profiles.create(
            owner_id="agent-a",
            profile=build_complete_mock_profile(),
        )
    )
    waiting = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=profile.customer_id,
            trigger_ids=["A2"],
        )
    )
    kernel_hash = waiting.steps["freeze-kernel"].output["kernel"]["kernel_hash"]

    with pytest.raises(ValueError, match="reviewer identity"):
        _run(
            service.decide_coverage_review(
                waiting.id,
                owner_id="agent-a",
                decision=ReviewDecision(
                    action=ReviewAction.APPROVE,
                    reviewer_id="agent-b",
                    expected_review_revision=1,
                    expected_kernel_hash=kernel_hash,
                ),
            )
        )

    rejected = _run(
        service.decide_coverage_review(
            waiting.id,
            owner_id="agent-a",
            decision=ReviewDecision(
                action=ReviewAction.REJECT,
                reviewer_id="agent-a",
                expected_review_revision=1,
                expected_kernel_hash=kernel_hash,
                note="事实尚未完成核对",
            ),
        )
    )

    assert rejected.status is TaskStatus.FAILED
    assert rejected.steps["agent-review"].error == "user rejected confirmation"
    assert rejected.steps["customer-report"].output == {}


def test_missing_precise_field_waits_once_and_uses_confirmed_answer() -> None:
    """负债未知时必须追问，代理人确认无负债后才能调用精算。"""

    service = _service()
    profile, _version = _run(
        service.profiles.create(
            owner_id="agent-a",
            profile=build_incomplete_mock_profile(),
        )
    )

    waiting = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=profile.customer_id,
            trigger_ids=["A2"],
        )
    )

    assert waiting.status is TaskStatus.WAITING_INPUT
    assert {item.path for item in waiting.pending_inputs} == {"/answers/large_loan_wan"}

    resumed = _run(
        service.advance_task(
            waiting.id,
            owner_id="agent-a",
            answers={"large_loan_wan": 0},
        )
    )

    assert resumed.status is TaskStatus.WAITING_CONFIRMATION
    request = resumed.steps["calculate-gaps"].output["calculation_request"]
    assert request["large_loan_wan"] == "0"


def test_fact_revision_invalidates_kernel_reports_and_prior_approval_path() -> None:
    """事实修订必须生成新内核并重跑对内报告和复核。"""

    service = _service()
    profile, version = _run(
        service.profiles.create(
            owner_id="agent-a",
            profile=build_complete_mock_profile(),
        )
    )
    first = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=profile.customer_id,
            trigger_ids=["A2"],
        )
    )
    first_hash = first.steps["freeze-kernel"].output["kernel"]["kernel_hash"]

    revised = _run(
        service.decide_coverage_review(
            first.id,
            owner_id="agent-a",
            decision=ReviewDecision(
                action=ReviewAction.REVISE_FACTS,
                reviewer_id="agent-a",
                expected_review_revision=1,
                expected_kernel_hash=first_hash,
                expected_profile_version=version,
                fact_patches={"family_expense_yuan_month": 30000},
                note="代理人确认月支出为3万元",
            ),
        )
    )

    second_kernel = revised.steps["freeze-kernel"].output["kernel"]
    assert revised.status is TaskStatus.WAITING_CONFIRMATION
    assert second_kernel["revision"] == 2
    assert second_kernel["kernel_hash"] != first_hash
    assert revised.input_data["approval_record"] is None

    completed = _run(
        service.decide_coverage_review(
            revised.id,
            owner_id="agent-a",
            decision=ReviewDecision(
                action=ReviewAction.APPROVE,
                reviewer_id="agent-a",
                expected_review_revision=2,
                expected_kernel_hash=second_kernel["kernel_hash"],
            ),
        )
    )

    assert completed.status is TaskStatus.COMPLETED
    assert completed.steps["customer-report"].output["customer_report"]["kernel_hash"] == second_kernel["kernel_hash"]


def test_natural_language_fact_feedback_rebuilds_kernel_and_returns_to_review() -> None:
    """复核框中的纠正语句必须提取最后的新值并重新进入复核闸门。"""

    service = _service()
    profile, version = _run(
        service.profiles.create(
            owner_id="agent-a",
            profile=build_complete_mock_profile(),
        )
    )
    first = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=profile.customer_id,
            trigger_ids=["A2"],
        )
    )
    first_kernel = first.steps["freeze-kernel"].output["kernel"]

    revised = _run(
        service.apply_coverage_review_feedback(
            first.id,
            owner_id="agent-a",
            action=ReviewAction.REVISE_FACTS,
            note="家庭月支出不是2万，是3万。",
            expected_review_revision=1,
            expected_kernel_hash=first_kernel["kernel_hash"],
            expected_profile_version=version,
        )
    )

    assert revised.status is TaskStatus.WAITING_CONFIRMATION
    assert revised.input_data["answers"]["family_expense_yuan_month"] == 30000
    assert revised.input_data["review_revision"] == 2
    assert revised.steps["freeze-kernel"].output["kernel"]["revision"] == 2
    assert revised.steps["freeze-kernel"].output["kernel"]["kernel_hash"] != first_kernel["kernel_hash"]
    assert revised.steps["agent-review"].status.value == "waiting_confirmation"
    assert revised.steps["customer-report"].output == {}


def test_narrative_revision_does_not_recalculate_kernel() -> None:
    """纯叙事修改只重跑报告相关节点，不改变诊断内核。"""

    service = _service()
    profile, _version = _run(
        service.profiles.create(
            owner_id="agent-a",
            profile=build_complete_mock_profile(),
        )
    )
    first = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=profile.customer_id,
            trigger_ids=["D2"],
        )
    )
    first_hash = first.steps["freeze-kernel"].output["kernel"]["kernel_hash"]
    first_attempts = first.steps["freeze-kernel"].attempts

    revised = _run(
        service.apply_coverage_review_feedback(
            first.id,
            owner_id="agent-a",
            action=ReviewAction.REVISE_NARRATIVE,
            note="称呼改为王女士，解释简洁一些。",
            expected_review_revision=1,
            expected_kernel_hash=first_hash,
        )
    )

    assert revised.status is TaskStatus.WAITING_CONFIRMATION
    assert revised.steps["freeze-kernel"].output["kernel"]["kernel_hash"] == first_hash
    assert revised.steps["freeze-kernel"].attempts == first_attempts
    assert revised.input_data["coverage_review_revision"] == 1
    assert revised.input_data["review_revision"] == 2
    assert revised.steps["agent-review"].output["review_packet"]["revision"] == 2
    markdown = revised.steps["internal-report"].output["internal_report"]["markdown"]
    assert "采用简洁的解释节奏" in markdown
    assert "解释密度:" not in markdown

    with pytest.raises(ValueError, match="review revision"):
        _run(
            service.decide_coverage_review(
                revised.id,
                owner_id="agent-a",
                decision=ReviewDecision(
                    action=ReviewAction.APPROVE,
                    reviewer_id="agent-a",
                    expected_review_revision=1,
                    expected_kernel_hash=first_hash,
                ),
            )
        )

    completed = _run(
        service.decide_coverage_review(
            revised.id,
            owner_id="agent-a",
            decision=ReviewDecision(
                action=ReviewAction.APPROVE,
                reviewer_id="agent-a",
                expected_review_revision=2,
                expected_kernel_hash=first_hash,
            ),
        )
    )
    customer = completed.steps["customer-report"].output["customer_report"]
    assert customer["view_model"]["revision"] == 2
    assert customer["view_model"]["customer_name"] == "演示甲"
    assert customer["view_model"]["salutation"] == "王女士，你好。"
    assert customer["manifest"]["approval_record"]["review_revision"] == 2


def test_suspended_parent_remains_recoverable_when_child_task_runs() -> None:
    """临时子任务不能破坏被挂起的父保障检视任务。"""

    service = _service()
    first_profile = build_incomplete_mock_profile()
    second_profile = build_complete_mock_profile()
    second_profile.customer_id = "mock-child-task-customer"
    first, _ = _run(service.profiles.create(owner_id="agent-a", profile=first_profile))
    second, _ = _run(service.profiles.create(owner_id="agent-a", profile=second_profile))

    parent = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=first.customer_id,
            trigger_ids=["A2"],
        )
    )
    suspended = _run(
        service.suspend_task(
            parent.id,
            owner_id="agent-a",
            reason="临时处理另一个客户",
        )
    )
    child = _run(
        service.start_coverage_review(
            owner_id="agent-a",
            customer_id=second.customer_id,
            thread_id="thread-1",
            parent_task_id=parent.id,
            trigger_ids=["D2"],
        )
    )

    assert suspended.status is TaskStatus.SUSPENDED
    assert child.status is TaskStatus.WAITING_CONFIRMATION
    assert child.parent_task_id == parent.id
    resumed = _run(service.resume_task(parent.id, owner_id="agent-a"))
    assert resumed.status is TaskStatus.READY
