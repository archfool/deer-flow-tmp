"""保障检视自然语言入口、表格闸门与双报告交付回归测试。"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from app.insurance.agent import CoverageReviewRoutingMiddleware
from app.insurance.coverage_review import ReviewAction, ReviewDecision
from app.insurance.intake import parse_coverage_review_intake
from app.insurance.mock_data import hydrate_registered_mock_intake
from app.insurance.models import Gender
from app.insurance.service import InsuranceService
from app.insurance.ui import build_intake_ui, build_task_ui
from deerflow.subject_memory import InMemorySubjectMemoryRepository
from deerflow.workflows import (
    InMemoryTaskRepository,
    StepStatus,
    TaskStatus,
)


def _run(value):
    """在不依赖 pytest-asyncio 的环境中执行协程。"""

    return asyncio.run(value)


def _service() -> InsuranceService:
    """构造隔离的企业级保障检视服务。"""

    return InsuranceService(
        task_repository=InMemoryTaskRepository(),
        memory_repository=InMemorySubjectMemoryRepository(),
    )


def test_zhangsan_minimal_input_stops_at_structured_question_gate() -> None:
    """张三最小输入不得直接生成 Markdown 报告。"""

    intake = parse_coverage_review_intake(
        "张三，33岁，男，IT。已婚，年收入50万。只有医疗险。",
        thread_id="thread-zhangsan",
    )
    task = _run(
        _service().start_coverage_review_intake(
            owner_id="agent-1",
            intake=intake,
        )
    )
    envelope = build_task_ui(task)

    assert intake.customer_name == "张三"
    assert intake.occupation == "IT"
    assert task.status is TaskStatus.WAITING_INPUT
    assert {field.path for field in envelope.form_fields} == {
        "/answers/spouse_annual_income_wan",
        "/answers/family_expense_yuan_month",
        "/answers/large_loan_wan",
        "/answers/existing_wealth_reserve_wan",
        "/answers/existing_retirement_cashflow_yuan_year",
    }
    assert envelope.internal_report_url is None
    assert envelope.customer_report_url is None
    assert task.steps["internal-report"].output == {}
    assert task.steps["customer-report"].output == {}


def test_registered_mock_customer_loads_profile_and_policy_report_by_name() -> None:
    """演示甲应通过姓名命中完整客户档案和中保信 Mock 保单。"""

    service = _service()
    intake = hydrate_registered_mock_intake(
        parse_coverage_review_intake(
            "帮我给演示甲做一个保障检视",
            thread_id="thread-registered-mock",
        )
    )

    task = _run(
        service.start_coverage_review_intake(
            owner_id="agent-1",
            intake=intake,
        )
    )

    assert intake.missing_identity_fields == ()
    assert task.status is TaskStatus.WAITING_CONFIRMATION
    assert len(task.input_data["profile"]["members"]) == 3
    assert len(task.input_data["profile"]["policies"]) == 4
    policy_report = task.steps["extract-policy-report"].output["policy_report"]
    assert policy_report["total_active_policies"] == 4
    assert {item["category"] for item in policy_report["policy_facts"]} >= {
        "身故保障",
        "疾病保障",
    }


def test_question_gate_review_gate_and_two_report_formats() -> None:
    """补录后先出对内版，批准后才出样例结构的对客 HTML。"""

    service = _service()
    intake = parse_coverage_review_intake(
        "张三，33岁，男，IT。已婚，年收入50万。只有医疗险。",
        thread_id="thread-zhangsan",
    )
    waiting = _run(service.start_coverage_review_intake(owner_id="agent-1", intake=intake))
    review = _run(
        service.advance_task(
            waiting.id,
            owner_id="agent-1",
            answers={
                "spouse_annual_income_wan": 20,
                "family_expense_yuan_month": 20000,
                "large_loan_wan": 0,
                "existing_wealth_reserve_wan": 0,
                "existing_retirement_cashflow_yuan_year": 0,
            },
        )
    )

    assert review.status is TaskStatus.WAITING_CONFIRMATION
    internal = review.steps["internal-report"].output["internal_report"]["markdown"]
    assert "## 一、客户速读与核心抓手" in internal
    assert "## 二、测算明细与关键假设" in internal
    assert "## 三、必须补问的信息" in internal
    assert "## 八、下一步动作" in internal
    assert "医疗保障已有部分" in internal
    assert review.steps["customer-report"].output == {}

    kernel = review.steps["freeze-kernel"].output["kernel"]
    completed = _run(
        service.decide_coverage_review(
            review.id,
            owner_id="agent-1",
            decision=ReviewDecision(
                action=ReviewAction.APPROVE,
                reviewer_id="agent-1",
                expected_review_revision=1,
                expected_kernel_hash=kernel["kernel_hash"],
            ),
        )
    )
    html = completed.steps["customer-report"].output["customer_report"]["html"]

    assert completed.status is TaskStatus.COMPLETED
    assert "致张三的一份家庭保障体检" in html
    assert ">01</span>你的家庭" in html
    assert ">03</span>八维保障体检" in html
    assert ">04</span>先补哪个？一张行动路线图" in html
    assert "医疗保障已有部分已形成有效基础" in html
    assert build_task_ui(completed).customer_report_url


def test_name_only_intake_completes_all_real_workflow_gates() -> None:
    """只有客户姓名时必须依次经过两轮补录、复核和双报告交付。"""

    service = _service()
    intake = parse_coverage_review_intake(
        "帮我给张三做一个保障检视",
        thread_id="thread-name-only",
    )
    intake_ui = build_intake_ui(intake)

    assert intake.customer_name == "张三"
    assert intake.missing_identity_fields == ("age", "gender")
    assert intake_ui.status == "intake_required"
    assert {field.path for field in intake_ui.form_fields} == {"/age", "/gender"}

    waiting = _run(
        service.start_coverage_review_intake(
            owner_id="agent-1",
            intake=intake.model_copy(update={"age": 33, "gender": Gender.MALE}),
        )
    )
    waiting_ui = build_task_ui(waiting)

    assert waiting.status is TaskStatus.WAITING_INPUT
    assert len(waiting_ui.progress_steps) == 9
    assert waiting_ui.progress_steps[0].status == "completed"
    assert waiting_ui.progress_steps[1].status == "completed"
    assert waiting_ui.progress_steps[2].status == "waiting"
    assert {field.path for field in waiting_ui.form_fields} == {
        "/answers/annual_income_wan",
        "/answers/spouse_annual_income_wan",
        "/answers/family_expense_yuan_month",
        "/answers/large_loan_wan",
        "/answers/existing_disease_coverage_wan",
        "/answers/existing_medical_responsibility_tier",
        "/answers/existing_disability_coverage_wan",
        "/answers/existing_care_coverage_wan",
        "/answers/existing_death_coverage_wan",
        "/answers/existing_wealth_reserve_wan",
        "/answers/existing_retirement_cashflow_yuan_year",
        "/answers/existing_legacy_reserve_wan",
    }

    review = _run(
        service.advance_task(
            waiting.id,
            owner_id="agent-1",
            answers={
                "annual_income_wan": 50,
                "spouse_annual_income_wan": 0,
                "family_expense_yuan_month": 20000,
                "large_loan_wan": 0,
                "existing_disease_coverage_wan": 0,
                "existing_medical_responsibility_tier": 0,
                "existing_disability_coverage_wan": 0,
                "existing_care_coverage_wan": 0,
                "existing_death_coverage_wan": 0,
                "existing_wealth_reserve_wan": 0,
                "existing_retirement_cashflow_yuan_year": 0,
                "existing_legacy_reserve_wan": 0,
            },
        )
    )
    assert review.status is TaskStatus.WAITING_CONFIRMATION
    review_ui = build_task_ui(review)
    assert review_ui.internal_report_url
    assert review_ui.customer_report_url is None
    assert review_ui.review_packet is not None
    assert len(review_ui.review_packet.dimension_facts) == 8
    assert "年龄：33岁" in review_ui.review_packet.customer_identity
    assert "性别：男" in review_ui.review_packet.customer_identity
    visible_payload = review_ui.review_packet.model_dump_json()
    assert "internal_report" not in visible_payload
    assert "audit_manifest" not in visible_payload
    assert "reason_code" not in visible_payload
    assert "EMERGENCY_LIQUIDITY_ALERT" not in visible_payload

    kernel = review.steps["freeze-kernel"].output["kernel"]
    completed = _run(
        service.decide_coverage_review(
            review.id,
            owner_id="agent-1",
            decision=ReviewDecision(
                action=ReviewAction.APPROVE,
                reviewer_id="agent-1",
                expected_review_revision=1,
                expected_kernel_hash=kernel["kernel_hash"],
            ),
        )
    )
    completed_ui = build_task_ui(completed)
    assert completed.status is TaskStatus.COMPLETED
    assert completed_ui.internal_report_url
    assert completed_ui.customer_report_url


def test_language_generation_failure_can_retry_without_recalculating_input() -> None:
    """语言节点失败后应保留输入并恢复到代理人复核闸门。"""

    repository = InMemoryTaskRepository()
    service = InsuranceService(
        task_repository=repository,
        memory_repository=InMemorySubjectMemoryRepository(),
    )
    intake = parse_coverage_review_intake(
        "张三，33岁，男，IT。已婚，年收入50万。只有医疗险。",
        thread_id="thread-retry-language",
    )
    waiting = _run(
        service.start_coverage_review_intake(
            owner_id="agent-1",
            intake=intake,
        )
    )
    review = _run(
        service.advance_task(
            waiting.id,
            owner_id="agent-1",
            answers={
                "spouse_annual_income_wan": 20,
                "family_expense_yuan_month": 20000,
                "large_loan_wan": 0,
                "existing_wealth_reserve_wan": 0,
                "existing_retirement_cashflow_yuan_year": 0,
            },
        )
    )
    failed = review.model_copy(deep=True)
    failed.status = TaskStatus.FAILED
    failed.error = "step customer-analysis failed"
    failed.steps["customer-analysis"].status = StepStatus.FAILED
    failed.steps["customer-analysis"].error = "RuntimeError: 受约束模型文案生成失败"
    failed = _run(
        repository.save(
            failed,
            expected_revision=review.revision,
        )
    )

    failed_ui = build_task_ui(failed)
    retried = _run(
        service.advance_task(
            failed.id,
            owner_id="agent-1",
        )
    )

    assert failed_ui.can_retry is True
    assert failed_ui.progress_steps[5].status == "failed"
    assert retried.status is TaskStatus.WAITING_CONFIRMATION
    assert retried.steps["customer-analysis"].attempts == 1
    assert retried.steps["calculate-gaps"].attempts == 1


@pytest.mark.parametrize(
    "report_error",
    (
        "ValueError: customer report contains forbidden pattern: 收益率",
        ("ValueError: customer calculation explanation misses derivation component: 建议商业医疗责任范围"),
    ),
)
def test_customer_report_validation_failure_can_regenerate_from_customer_copy(
    report_error: str,
) -> None:
    """可修复的对客校验失败应从文案节点重建且不重跑测算。"""

    repository = InMemoryTaskRepository()
    service = InsuranceService(
        task_repository=repository,
        memory_repository=InMemorySubjectMemoryRepository(),
    )
    intake = hydrate_registered_mock_intake(
        parse_coverage_review_intake(
            "帮我给演示甲做一个保障检视",
            thread_id="thread-retry-customer-report",
        )
    )
    review = _run(
        service.start_coverage_review_intake(
            owner_id="agent-1",
            intake=intake,
        )
    )
    completed = _run(
        service.decide_coverage_review(
            review.id,
            owner_id="agent-1",
            decision=ReviewDecision(
                action=ReviewAction.APPROVE,
                reviewer_id="agent-1",
                expected_review_revision=1,
                expected_kernel_hash=review.steps["freeze-kernel"].output["kernel"]["kernel_hash"],
            ),
        )
    )
    failed = completed.model_copy(deep=True)
    failed.status = TaskStatus.FAILED
    failed.error = "step customer-report failed"
    failed.steps["customer-report"].status = StepStatus.FAILED
    failed.steps["customer-report"].error = report_error
    failed.steps["finalize"].status = StepStatus.PENDING
    failed.steps["finalize"].output = {}
    failed = _run(
        repository.save(
            failed,
            expected_revision=completed.revision,
        )
    )

    retried = _run(
        service.advance_task(
            failed.id,
            owner_id="agent-1",
        )
    )

    assert build_task_ui(failed).can_retry is True
    assert retried.status is TaskStatus.COMPLETED
    assert retried.steps["calculate-gaps"].attempts == 1
    assert retried.steps["customer-copy"].attempts == 1


def test_agent_middleware_bypasses_freeform_model_for_coverage_review() -> None:
    """保障检视输入必须由 FSM 直接派发 Tool，Tool 结果直接结束本轮。"""

    middleware = CoverageReviewRoutingMiddleware()
    dispatched = middleware._route({"messages": [HumanMessage(content="张三，33岁，男，IT。已婚，年收入50万。只有医疗险。")]})

    assert dispatched is not None
    assert dispatched["jump_to"] == "tools"
    assert dispatched["messages"][0].tool_calls[0]["name"] == ("insurance_coverage_review_intake")

    payload = {
        "kind": "insurance_coverage_review",
        "status": "waiting_input",
        "message": "请补充缺口信息。",
    }
    finished = middleware._route(
        {
            "messages": [
                ToolMessage(
                    content=json.dumps(payload, ensure_ascii=False),
                    tool_call_id="insurance-1",
                    name="insurance_coverage_review_intake",
                )
            ]
        }
    )

    assert finished is not None
    assert finished["jump_to"] == "end"
    assert finished["messages"][0].content == ("保障检视流程已受理，请在任务卡中继续完成信息补录、代理人复核和报告生成。")
