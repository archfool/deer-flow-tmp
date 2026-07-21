"""保障检视聊天任务卡与补录表格的稳定 UI 契约。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.insurance.coverage_review import (
    COVERAGE_REVIEW_LANGUAGE_STEP_IDS,
    COVERAGE_REVIEW_TASK_NAME,
    DimensionCode,
    DimensionState,
    ReviewPacket,
)
from app.insurance.coverage_review.display import format_value
from app.insurance.coverage_review.rules import load_default_rule_bundle
from app.insurance.intake import CoverageReviewIntake
from deerflow.workflows import StepStatus, TaskInstance, TaskStatus


class FormOption(BaseModel):
    """选择控件的值与显示名称。"""

    model_config = ConfigDict(frozen=True)

    value: str
    label: str


class FormField(BaseModel):
    """前端可以直接渲染的一行结构化补录字段。"""

    model_config = ConfigDict(frozen=True)

    path: str
    label: str
    reason: str = ""
    control: Literal["text", "number", "select", "multi_select"] = "text"
    unit: str | None = None
    required: bool = True
    sensitive: bool = False
    value: Any = None
    options: tuple[FormOption, ...] = ()


class WorkflowProgressStep(BaseModel):
    """前端任务卡展示的一项业务阶段进度。"""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    status: Literal[
        "pending",
        "running",
        "completed",
        "waiting",
        "failed",
    ]


class ReviewDimensionView(BaseModel):
    """代理人复核表中可见的单维事实，不携带规则码。"""

    model_config = ConfigDict(frozen=True)

    dimension_code: DimensionCode
    dimension_name: str
    state: DimensionState
    existing_value: Decimal | None = None
    ideal_value: Decimal | None = None
    gap_value: Decimal | None = None
    existing_display: str
    ideal_display: str
    gap_display: str
    unit: str
    priority_rank: int | None = None


class ReviewPacketView(BaseModel):
    """复核闸门的代理人视图，隔离内部报告和审计字段。"""

    model_config = ConfigDict(frozen=True)

    customer_identity: tuple[str, ...]
    trigger_context: tuple[str, ...]
    dimension_facts: tuple[ReviewDimensionView, ...]
    auxiliary_observations: tuple[str, ...]
    preserve_items: tuple[str, ...]
    correction_items: tuple[str, ...]
    warnings: tuple[str, ...]
    diff_summary: tuple[str, ...] = ()


class CoverageReviewUIEnvelope(BaseModel):
    """聊天 Tool、HTTP API 与前端任务卡共用的窄响应。"""

    kind: Literal["insurance_coverage_review"] = "insurance_coverage_review"
    status: Literal[
        "intake_required",
        "waiting_input",
        "waiting_confirmation",
        "completed",
        "suspended",
        "cancelled",
        "failed",
        "running",
    ]
    message: str
    task_id: str | None = None
    customer_id: str | None = None
    thread_id: str | None = None
    intake: CoverageReviewIntake | None = None
    form_fields: tuple[FormField, ...] = ()
    internal_report_url: str | None = None
    customer_report_url: str | None = None
    review_revision: int | None = None
    kernel_hash: str | None = None
    profile_version: int | None = None
    review_packet: ReviewPacketView | None = None
    progress_steps: tuple[WorkflowProgressStep, ...] = ()
    can_retry: bool = False


_IDENTITY_FIELDS: dict[str, FormField] = {
    "customer_name": FormField(
        path="/customer_name",
        label="客户姓名",
        reason="用于查询客户中心和绑定客户档案。",
    ),
    "age": FormField(
        path="/age",
        label="年龄",
        reason="用于识别人生阶段并完成保障测算。",
        control="number",
        unit="岁",
    ),
    "gender": FormField(
        path="/gender",
        label="性别",
        reason="用于客户中心基础信息核验。",
        control="select",
        options=(
            FormOption(value="male", label="男"),
            FormOption(value="female", label="女"),
            FormOption(value="other", label="其他"),
            FormOption(value="undisclosed", label="不披露"),
        ),
    ),
}

_FIELD_PRESENTATION: dict[str, tuple[str, str, str | None]] = {
    "annual_income_wan": ("本人年收入", "number", "万元/年"),
    "spouse_annual_income_wan": ("配偶年收入", "number", "万元/年"),
    "family_expense_yuan_month": ("家庭月支出", "number", "元/月"),
    "large_loan_wan": ("大额贷款余额", "number", "万元"),
    "existing_disease_coverage_wan": ("现有疾病保障", "number", "万元"),
    "existing_medical_responsibility_tier": (
        "现有商业医疗责任",
        "select",
        None,
    ),
    "existing_disability_coverage_wan": ("现有伤残保障", "number", "万元"),
    "existing_care_coverage_wan": ("现有护理保障", "number", "万元"),
    "existing_death_coverage_wan": ("现有身故保障", "number", "万元"),
    "existing_wealth_reserve_wan": ("现有长期财富储备", "number", "万元"),
    "existing_retirement_cashflow_yuan_year": (
        "现有商业养老现金流",
        "number",
        "元/年",
    ),
    "existing_legacy_reserve_wan": ("现有传承储备", "number", "万元"),
    "annual_premium_budget_yuan": ("年度保费预算", "number", "元/年"),
    "investment_risk_tolerance": ("投资风险偏好", "select", None),
    "review_scope": ("本次检视范围", "select", None),
    "trigger_ids": ("本次检视场景", "multi_select", None),
}

_PROGRESS_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "customer_data",
        "查询客户中心与客户档案",
        (
            "collect-customer-center",
            "collect-customer-profile",
        ),
    ),
    (
        "policy_data",
        "查询并解析中银保信保单报告",
        (
            "fetch-policy-report",
            "extract-policy-report",
        ),
    ),
    (
        "evidence_gate",
        "确认触发场景并完成信息闸门",
        (
            "resolve-triggers",
            "evidence-gate",
        ),
    ),
    (
        "calculation",
        "执行八维保障缺口精确测算",
        ("calculate-gaps",),
    ),
    (
        "kernel",
        "固化诊断内核与八维事实",
        ("freeze-kernel", "project-3-1"),
    ),
    (
        "diagnosis",
        "生成 5.1、5.2、5.3 诊断内容",
        (
            "project-5-2",
            "verbalize-5-2",
            "project-5-3",
            "verbalize-5-3",
            "customer-analysis",
            "meeting-support",
            "consistency-gate",
        ),
    ),
    (
        "internal_report",
        "校验并生成对内诊断报告",
        ("language-gate", "internal-report"),
    ),
    (
        "agent_review",
        "等待代理人复核确认",
        ("agent-review",),
    ),
    (
        "customer_report",
        "生成并校验对客 HTML 报告",
        (
            "customer-copy",
            "customer-view-model",
            "customer-report",
            "finalize",
        ),
    ),
)


def build_intake_ui(intake: CoverageReviewIntake) -> CoverageReviewUIEnvelope:
    """把自然语言入口尚缺的身份事实转成补录表格。

    Args:
        intake: 当前已机械提取的显式客户事实。

    Returns:
        不启动工作流的身份补录任务卡。
    """

    fields = tuple(_IDENTITY_FIELDS[key].model_copy(update={"value": getattr(intake, key)}) for key in intake.missing_identity_fields)
    return CoverageReviewUIEnvelope(
        status="intake_required",
        message="开始检视前，需要先确认少量客户身份信息，以便准确匹配客户中心、档案与保单资料。",
        form_fields=fields,
        thread_id=intake.thread_id,
        intake=intake,
        progress_steps=_intake_progress_steps(),
    )


def build_task_ui(task: TaskInstance) -> CoverageReviewUIEnvelope:
    """把持久化工作流状态投影为聊天任务卡。

    Args:
        task: 当前所有者名下的保障检视任务。

    Returns:
        只暴露 UI 所需字段的窄响应。

    Raises:
        ValueError: 收到非保障检视任务时抛出。
    """

    if task.task_name != COVERAGE_REVIEW_TASK_NAME:
        raise ValueError("task is not an insurance coverage review")
    report_base = f"/api/insurance/tasks/{task.id}/reports"
    kernel = task.steps.get("freeze-kernel")
    kernel_payload = kernel.output.get("kernel", {}) if kernel is not None else {}
    review_step = task.steps.get("agent-review")
    review_payload = review_step.output.get("review_packet") if review_step is not None else None
    review_packet = ReviewPacket.model_validate(review_payload) if review_payload is not None else None
    status = _ui_status(task.status)
    return CoverageReviewUIEnvelope(
        status=status,
        message=_status_message(task, status),
        task_id=task.id,
        customer_id=task.subject_id,
        thread_id=task.thread_id,
        form_fields=_task_form_fields(task) if task.status is TaskStatus.WAITING_INPUT else (),
        internal_report_url=(f"{report_base}/internal" if task.status in {TaskStatus.WAITING_CONFIRMATION, TaskStatus.COMPLETED} else None),
        customer_report_url=(f"{report_base}/customer" if task.status is TaskStatus.COMPLETED else None),
        review_revision=int(task.input_data.get("review_revision", 1)),
        kernel_hash=kernel_payload.get("kernel_hash"),
        profile_version=int(task.input_data.get("profile_version", 1)),
        review_packet=_review_packet_view(review_packet) if review_packet is not None and status == "waiting_confirmation" else None,
        progress_steps=_task_progress_steps(task),
        can_retry=_can_retry(task),
    )


def _review_packet_view(packet: ReviewPacket) -> ReviewPacketView:
    """把内部复核包投影为不含理由码和审计清单的代理人视图。

    Args:
        packet: 工作流内部保存的完整复核包。

    Returns:
        只包含复核表格所需业务字段的视图。
    """

    return ReviewPacketView(
        customer_identity=packet.customer_identity,
        trigger_context=packet.trigger_context,
        dimension_facts=tuple(
            ReviewDimensionView(
                dimension_code=item.dimension_code,
                dimension_name=item.dimension_name,
                state=item.state,
                existing_value=item.existing_value,
                ideal_value=item.ideal_value,
                gap_value=item.gap_value,
                existing_display=format_value(item.existing_value, item.unit, role="existing"),
                ideal_display=format_value(item.ideal_value, item.unit, role="target"),
                gap_display=format_value(item.gap_value, item.unit, role="gap"),
                unit=item.unit,
                priority_rank=item.priority_rank,
            )
            for item in packet.dimension_facts
        ),
        auxiliary_observations=packet.auxiliary_observations,
        preserve_items=packet.preserve_items,
        correction_items=packet.correction_items,
        warnings=packet.warnings,
        diff_summary=packet.diff_summary,
    )


def _task_form_fields(task: TaskInstance) -> tuple[FormField, ...]:
    """把工作流 InputRequest 映射为表格控件。"""

    bundle = load_default_rule_bundle()
    result: list[FormField] = []
    for request in task.pending_inputs:
        key = request.path.rsplit("/", maxsplit=1)[-1]
        label, control, unit = _FIELD_PRESENTATION.get(
            key,
            (request.prompt, "text", None),
        )
        options: tuple[FormOption, ...] = ()
        if key == "investment_risk_tolerance":
            options = tuple(FormOption(value=value, label=value) for value in ("保守型", "稳健型", "激进型"))
        elif key == "existing_medical_responsibility_tier":
            options = (
                FormOption(value="0", label="尚未配置商业医疗保障"),
                FormOption(value="1", label="基础住院医疗责任"),
                FormOption(value="2", label="基础及扩展医疗责任"),
            )
        elif key == "review_scope":
            options = (
                FormOption(value="个人", label="个人"),
                FormOption(value="家庭", label="家庭"),
            )
        elif key == "trigger_ids":
            options = tuple(FormOption(value=item.id, label=f"{item.id} {item.scene}") for item in bundle.triggers)
        result.append(
            FormField(
                path=request.path,
                label=label,
                reason=request.reason,
                control=control,
                unit=unit,
                sensitive=request.sensitive,
                options=options,
            )
        )
    return tuple(result)


def _ui_status(status: TaskStatus) -> str:
    """把通用工作流状态映射为保障检视任务卡状态。"""

    if status in {
        TaskStatus.WAITING_INPUT,
        TaskStatus.WAITING_CONFIRMATION,
        TaskStatus.COMPLETED,
        TaskStatus.SUSPENDED,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    }:
        return status.value
    return "running"


def _status_message(task: TaskInstance, status: str) -> str:
    """返回不允许模型改写的状态说明。"""

    messages = {
        "waiting_input": "资料汇集已经完成。仍有少量关键字段需要代理人确认，补齐后即可进入八维诊断。",
        "waiting_confirmation": "内部诊断已经就绪。请代理人完成最后复核，确认事实、数字与沟通表达后再生成客户版报告。",
        "completed": "本次保障检视已经完成。代理人诊断与客户版报告已锁定同一诊断内核，可分别查看和交付。",
        "suspended": "保障检视任务已暂停。",
        "cancelled": "保障检视任务已取消。",
        "failed": ("诊断事实与八维测算已安全保留。报告表达生成暂时中断，可直接从中断处继续，无需重新录入。" if _can_retry(task) else "保障检视任务执行失败，请查看服务日志。"),
        "running": "正在协调资料、测算与报告环节；完成当前阶段后会自动进入下一步。",
    }
    return messages[status]


def _intake_progress_steps() -> tuple[WorkflowProgressStep, ...]:
    """返回身份补录阶段的初始业务进度。"""

    return tuple(
        WorkflowProgressStep(
            key=key,
            label=label,
            status="waiting" if index == 0 else "pending",
        )
        for index, (key, label, _step_ids) in enumerate(_PROGRESS_GROUPS)
    )


def _task_progress_steps(
    task: TaskInstance,
) -> tuple[WorkflowProgressStep, ...]:
    """把底层 DAG 节点聚合为代理人可理解的业务阶段。

    Args:
        task: 当前保障检视任务。

    Returns:
        顺序固定的九阶段进度。
    """

    return tuple(
        WorkflowProgressStep(
            key=key,
            label=label,
            status=_progress_group_status(tuple(task.steps[step_id].status for step_id in step_ids if step_id in task.steps)),
        )
        for key, label, step_ids in _PROGRESS_GROUPS
    )


def _progress_group_status(
    statuses: tuple[StepStatus, ...],
) -> Literal["pending", "running", "completed", "waiting", "failed"]:
    """汇总一组工作流节点状态。"""

    if any(status is StepStatus.FAILED for status in statuses):
        return "failed"
    if any(status in {StepStatus.WAITING_INPUT, StepStatus.WAITING_CONFIRMATION} for status in statuses):
        return "waiting"
    if any(status is StepStatus.RUNNING for status in statuses):
        return "running"
    if statuses and all(status in {StepStatus.COMPLETED, StepStatus.SKIPPED} for status in statuses):
        return "completed"
    if any(status in {StepStatus.COMPLETED, StepStatus.SKIPPED} for status in statuses):
        return "running"
    return "pending"


def _can_retry(task: TaskInstance) -> bool:
    """判断失败任务是否可通过重新生成受控语言资产恢复。"""

    failed = {step_id for step_id, state in task.steps.items() if state.status is StepStatus.FAILED}
    if bool(failed) and failed.issubset(COVERAGE_REVIEW_LANGUAGE_STEP_IDS):
        return True
    if failed != {"customer-report"}:
        return False
    error = task.steps["customer-report"].error or ""
    return any(
        marker in error
        for marker in (
            "customer report contains forbidden pattern:",
            "customer calculation explanation misses derivation component:",
        )
    )
