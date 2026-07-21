"""保障检视企业级算法域的强类型契约。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    """为诊断事实和投影提供不可变的 Pydantic 基类。"""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)


class DimensionCode(StrEnum):
    """八类保障缺口的规范维度码。"""

    DISEASE = "D1"
    MEDICAL = "D2"
    DISABILITY = "D3"
    LONG_TERM_CARE = "D5"
    DEATH = "D4"
    WEALTH = "A1"
    RETIREMENT = "B1"
    LEGACY = "C1"


DIMENSION_ORDER: tuple[DimensionCode, ...] = (
    DimensionCode.DISEASE,
    DimensionCode.MEDICAL,
    DimensionCode.DISABILITY,
    DimensionCode.LONG_TERM_CARE,
    DimensionCode.DEATH,
    DimensionCode.WEALTH,
    DimensionCode.RETIREMENT,
    DimensionCode.LEGACY,
)


class DimensionState(StrEnum):
    """诊断内核允许的缺口状态。"""

    SUFFICIENT = "sufficient"
    MILD_GAP = "mild_gap"
    SIGNIFICANT_GAP = "significant_gap"
    SEVERE_GAP = "severe_gap"
    NO_NEED = "no_need"
    UNKNOWN = "unknown"


class MeasurementType(StrEnum):
    """维度缺口的量纲类型。"""

    AMOUNT_RATIO = "amount_ratio"
    LIABILITY_TIER = "liability_tier"
    CASHFLOW_RATIO = "cashflow_ratio"


class ConfidenceLevel(StrEnum):
    """诊断事实的数据置信度。"""

    MEASURED = "measured"
    INFERRED = "inferred"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"


class EvidenceSource(StrEnum):
    """字段证据的规范来源。"""

    CUSTOMER_CENTER = "customer_center"
    CUSTOMER_PROFILE = "customer_profile"
    POLICY_REPORT = "policy_report"
    AGENT_CONFIRMATION = "agent_confirmation"
    CALCULATOR = "calculator"
    DERIVED = "derived"


class VerificationStatus(StrEnum):
    """字段证据的校验状态。"""

    RAW = "raw"
    EXTRACTED = "extracted"
    CONFIRMED = "confirmed"
    CONFLICTED = "conflicted"


class TemperatureType(StrEnum):
    """触发场景对应的情绪温度。"""

    OPEN = "open"
    FEAR = "fear"
    DEFENSIVE = "defensive"
    COLD_START = "cold_start"
    NEUTRAL = "neutral"


class CustomerRoute(StrEnum):
    """客户诊断叙事分线。"""

    STANDARD = "standard"
    HIGH_NET_WORTH = "high_net_worth"
    NEEDS_CONFIRMATION = "needs_confirmation"


class RouteConfidence(StrEnum):
    """客户分线结论置信度。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ActionType(StrEnum):
    """5.3 允许的六种保障动作。"""

    NEW_POLICY = "new_policy"
    ADD_COVERAGE = "add_coverage"
    ADJUST = "adjust"
    REDUCED_PAID_UP = "reduced_paid_up"
    SURRENDER = "surrender"
    MAINTAIN = "maintain"


class ActionWave(StrEnum):
    """5.3 行动次序的波次。"""

    FIRST = "first"
    SECOND = "second"
    THIRD = "third"
    MAINTAIN = "maintain"


class ReviewAction(StrEnum):
    """代理人复核闸门允许的动作。"""

    APPROVE = "approve"
    REVISE_FACTS = "revise_facts"
    REVISE_TRIGGERS = "revise_triggers"
    REVISE_NARRATIVE = "revise_narrative"
    REJECT = "reject"


class GenerationMode(StrEnum):
    """语言资产的实际生成方式。"""

    MODEL = "model"
    HYBRID = "hybrid"
    RULE_BOUND = "rule_bound"
    FALLBACK = "fallback"


class EvidenceItem(FrozenModel):
    """单个字段的值、来源、时点和校验信息。"""

    field_key: str
    value: Any = None
    source_type: EvidenceSource
    source_ref: str
    as_of: datetime
    confidence: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    verification_status: VerificationStatus = VerificationStatus.RAW
    sensitivity: str = "internal"


class PolicyFact(FrozenModel):
    """从中保信报告抽取的一条保单或聚合保障事实。"""

    fact_id: str
    category: str
    value: Decimal | str | int | None = None
    unit: str | None = None
    source_excerpt: str
    confidence: ConfidenceLevel = ConfidenceLevel.MEASURED


class PolicyReportExtraction(FrozenModel):
    """中保信原始报告的结构化抽取结果。"""

    report_id: str
    total_active_policies: int | None = None
    policy_facts: tuple[PolicyFact, ...] = ()
    conflicts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    source_hash: str


class TriggerBinding(FrozenModel):
    """一次检视绑定的主触发、复合触发和调性契约。"""

    primary_trigger_id: str
    trigger_ids: tuple[str, ...]
    temperature: TemperatureType
    focus_dimensions: tuple[DimensionCode, ...]
    auxiliary_focus: bool = False
    tone_contract: tuple[str, ...] = ()
    guardrails: tuple[str, ...] = ()
    scenario_materials: tuple[str, ...] = ()


class QuestionItem(FrozenModel):
    """字段追问计划中的单个问题。"""

    field_key: str
    prompt: str
    reason: str
    priority: str
    blocking: bool
    sensitive: bool = False


class EvidenceBundle(FrozenModel):
    """步骤 1 固化的客户、档案、保单和触发证据。"""

    customer_id: str
    customer_name: str
    as_of_date: date
    profile_version: int
    fields: tuple[EvidenceItem, ...]
    policy_report: PolicyReportExtraction
    trigger_binding: TriggerBinding
    assumptions: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    enhancement_questions: tuple[QuestionItem, ...] = ()

    def field_value(self, field_key: str, default: Any = None) -> Any:
        """读取指定规范字段的最新证据值。

        Args:
            field_key: 规范字段名。
            default: 字段不存在时返回的默认值。

        Returns:
            对应字段值，或调用方提供的默认值。
        """

        matches = [item for item in self.fields if item.field_key == field_key]
        return matches[-1].value if matches else default


class CalculationRequest(FrozenModel):
    """步骤 2 发送给精确测算 Tool 的规范请求。"""

    request_id: str
    customer_id: str
    member_id: str
    annual_income_wan: Decimal
    spouse_annual_income_wan: Decimal
    family_expense_yuan_month: Decimal
    large_loan_wan: Decimal
    investment_risk_tolerance: str
    existing_disease_coverage_wan: Decimal
    existing_medical_responsibility_tier: Decimal
    existing_disability_coverage_wan: Decimal
    existing_care_coverage_wan: Decimal
    existing_death_coverage_wan: Decimal
    existing_wealth_reserve_wan: Decimal
    existing_retirement_cashflow_yuan_year: Decimal
    existing_legacy_reserve_wan: Decimal
    d3_semantic_version: str = "disability-v2.1"
    evidence_refs: tuple[str, ...] = ()


class DerivationComponent(FrozenModel):
    """精确测算 Tool 返回的单个推导分项。"""

    key: str
    label: str
    value: Decimal | None = None
    unit: str
    role: str = "factor"
    note: str | None = None


class CalculatorDimension(FrozenModel):
    """精确测算 Tool 返回的单维权威数字。"""

    dimension_code: DimensionCode
    source_code: str
    source_name: str
    semantic_version: str
    existing_value: Decimal | None = Field(default=None, ge=0)
    ideal_value: Decimal | None = Field(default=None, ge=0)
    gap_value: Decimal | None = Field(default=None, ge=0)
    unit: str
    is_full: bool
    recommend_first: bool = False
    measurement_type: MeasurementType
    liability_tier: str | None = None
    formula_version: str | None = None
    derivation_components: tuple[DerivationComponent, ...] = ()
    calculation_refs: tuple[str, ...] = ()


class AuxiliaryMetric(FrozenModel):
    """独立于八维缺口的辅助财务指标。"""

    metric_code: str
    status: str
    value: Decimal | None = None
    unit: str | None = None
    calculation_refs: tuple[str, ...] = ()


class CalculationResult(FrozenModel):
    """步骤 2 的八维权威测算结果。"""

    call_id: str
    tool_name: str
    tool_version: str
    calculation_type: str
    dimensions: tuple[CalculatorDimension, ...]
    auxiliary_metrics: tuple[AuxiliaryMetric, ...] = ()
    raw_response_hash: str

    @model_validator(mode="after")
    def validate_dimensions(self) -> CalculationResult:
        """校验八维完整性、顺序和 D3 伤残语义。

        Returns:
            校验通过的当前结果。

        Raises:
            ValueError: 维度不完整、顺序错误或 D3 语义错误时抛出。
        """

        codes = tuple(item.dimension_code for item in self.dimensions)
        if codes != DIMENSION_ORDER:
            raise ValueError(f"calculator dimensions must follow canonical order: {DIMENSION_ORDER}")
        disability = self.dimensions[DIMENSION_ORDER.index(DimensionCode.DISABILITY)]
        if disability.source_code != "D3" or not disability.semantic_version.startswith("disability-"):
            raise ValueError("DIMENSION_SEMANTIC_VERSION_MISMATCH: D3 must declare disability semantics")
        if any(metric.metric_code in {code.value for code in DIMENSION_ORDER} for metric in self.auxiliary_metrics):
            raise ValueError("auxiliary metrics must not use dimension codes")
        return self


class DimensionFact(FrozenModel):
    """诊断内核中的单维事实。"""

    dimension_code: DimensionCode
    dimension_name: str
    state: DimensionState
    measurement_type: MeasurementType
    existing_value: Decimal | None = None
    ideal_value: Decimal | None = None
    gap_value: Decimal | None = None
    gap_ratio: Decimal | None = Field(default=None, ge=0)
    unit: str
    reason_codes: tuple[str, ...]
    formula_version: str | None = None
    derivation_components: tuple[DerivationComponent, ...] = ()
    priority_rank: int | None = Field(default=None, ge=1)
    urgency_score: Decimal = Field(default=Decimal("0"), ge=0)
    urgency_reason_codes: tuple[str, ...] = ()
    confidence: ConfidenceLevel = ConfidenceLevel.MEASURED
    requires_agent_review: bool = False
    tool_priority_hint: bool = False
    fact_refs: tuple[str, ...] = ()
    calculation_refs: tuple[str, ...] = ()


class PreserveItem(FrozenModel):
    """诊断内核中的保留项。"""

    item_id: str
    dimension_code: DimensionCode | None = None
    object_ref: str
    reason_code: str
    confidence: ConfidenceLevel
    capability_available: bool
    fact_refs: tuple[str, ...] = ()


class CorrectionItem(FrozenModel):
    """诊断内核中的纠错项。"""

    item_id: str
    correction_type: str
    dimension_codes: tuple[DimensionCode, ...]
    reason_code: str
    confidence: ConfidenceLevel
    capability_available: bool
    fact_refs: tuple[str, ...] = ()


class AssumptionItem(FrozenModel):
    """诊断内核中必须显式复核的假设。"""

    field_key: str
    source: str
    affected_dimensions: tuple[DimensionCode, ...] = ()


class CustomerRouteDecision(FrozenModel):
    """标准客户或高客叙事线的确定性判定。"""

    route: CustomerRoute
    confidence: RouteConfidence
    basis_codes: tuple[str, ...]


class DiagnosisKernel(FrozenModel):
    """步骤 3 固化的不可变诊断内核。"""

    review_id: str
    revision: int = Field(ge=1)
    customer_id: str
    as_of_date: date
    evidence_hash: str
    calculation_hash: str
    calculator_tool_version: str
    rule_bundle_hash: str
    policy_inventory_confirmed: bool
    annual_premium_budget_yuan: Decimal | None = Field(default=None, ge=0)
    trigger_binding: TriggerBinding
    customer_route: CustomerRouteDecision
    dimension_facts: tuple[DimensionFact, ...]
    auxiliary_diagnostics: tuple[AuxiliaryMetric, ...] = ()
    preserve_items: tuple[PreserveItem, ...] = ()
    correction_items: tuple[CorrectionItem, ...] = ()
    priority_order: tuple[DimensionCode, ...] = ()
    assumptions: tuple[AssumptionItem, ...] = ()
    conflicts: tuple[str, ...] = ()
    capabilities: dict[str, bool] = Field(default_factory=dict)
    kernel_hash: str

    @model_validator(mode="after")
    def validate_kernel(self) -> DiagnosisKernel:
        """校验内核八维、辅助域和优先级不变量。

        Returns:
            校验通过的当前内核。

        Raises:
            ValueError: 内核违反八维或辅助域边界时抛出。
        """

        codes = tuple(item.dimension_code for item in self.dimension_facts)
        if codes != DIMENSION_ORDER:
            raise ValueError("diagnosis kernel must contain eight canonical dimensions")
        if any(item.metric_code != "EMERGENCY_LIQUIDITY_ALERT" for item in self.auxiliary_diagnostics):
            raise ValueError("unsupported auxiliary diagnostic")
        if set(self.priority_order) - set(DIMENSION_ORDER):
            raise ValueError("priority order contains non-canonical dimensions")
        return self


class WhyBlock(FrozenModel):
    """5.2 从内核投影出的解释块。"""

    kernel_hash: str
    rule_version: str
    dimension_code: DimensionCode
    reason_code: str
    framework_code: str
    framework_selection_basis: tuple[str, ...]
    derivation_chain: tuple[str, ...]
    narrative_skeleton: tuple[str, ...]
    benefit_language: tuple[str, ...]
    confidence: ConfidenceLevel
    fact_refs: tuple[str, ...]


class AuxiliaryInsight(FrozenModel):
    """5.2 对辅助财务指标的独立说明。"""

    kernel_hash: str
    metric_code: str
    summary: str
    fact_refs: tuple[str, ...]


class ActionItem(FrozenModel):
    """5.3 行动块中的单个维度级动作。"""

    action_id: str
    wave: ActionWave
    dimension_code: DimensionCode | None = None
    action_type: ActionType
    reason_code: str
    sequence_reason_codes: tuple[str, ...]
    background_gap_value: Decimal | None = None
    unit: str | None = None
    requires_human_review: bool = False
    fact_refs: tuple[str, ...] = ()


class ActionBlock(FrozenModel):
    """5.3 从内核投影出的封闭行动集合。"""

    kernel_hash: str
    rule_version: str
    actions: tuple[ActionItem, ...]
    maintain_items: tuple[ActionItem, ...]
    budget_tradeoff: str | None = None
    human_review_flags: tuple[str, ...] = ()
    handoff_statement: str
    boundary_hits: tuple[str, ...] = ()


class ConfirmationQuestion(FrozenModel):
    """代理人在面谈中需要确认的自然语言问题。"""

    item: str
    importance: str
    natural_question: str
    fact_refs: tuple[str, ...] = ()


class CustomerAnalysis(FrozenModel):
    """5.1 客户分析的结构化结果。"""

    one_line_profile: str
    customer_segment: str
    strongest_hooks: tuple[str, ...]
    conversion_assessment: str
    differentiation_notes: tuple[str, ...]
    summary_points: tuple[str, ...]
    core_focus: tuple[str, ...]
    confirmation_questions: tuple[ConfirmationQuestion, ...]
    fact_refs: tuple[str, ...] = ()
    generation_mode: GenerationMode = GenerationMode.MODEL


class DimensionNarrative(FrozenModel):
    """5.2 受约束语言节点输出的维度解释。"""

    dimension_code: DimensionCode
    heading: str
    calculation_explanation: tuple[str, ...]
    why_it_matters: str
    agent_guidance: str
    fact_refs: tuple[str, ...] = ()
    generation_mode: GenerationMode = GenerationMode.MODEL


class ActionNarrativeItem(FrozenModel):
    """5.3 受约束语言节点输出的单个行动表达。"""

    action_id: str
    title: str
    rationale: str
    agent_language: str


class ActionNarrative(FrozenModel):
    """5.3 行动次序的受约束文案。"""

    overview: str
    items: tuple[ActionNarrativeItem, ...]
    closing_boundary: str
    budget_tradeoff: str | None = None
    generation_mode: GenerationMode = GenerationMode.MODEL


class AgentScript(FrozenModel):
    """代理人面谈时可直接使用的话术块。"""

    topic: str
    script: str
    rationale: str
    fact_refs: tuple[str, ...] = ()


class ObjectionResponse(FrozenModel):
    """针对当前客户的异议处理块。"""

    objection: str
    strategy: str
    response: str


class MeetingPlan(FrozenModel):
    """5.4 至 5.6 的会谈支持材料。"""

    objectives: tuple[str, ...]
    agenda: tuple[str, ...]
    scripts: tuple[AgentScript, ...]
    objection_responses: tuple[ObjectionResponse, ...]
    red_lines: tuple[str, ...]
    next_actions: tuple[str, ...]
    knowledge_refs: tuple[str, ...] = ()
    generation_mode: GenerationMode = GenerationMode.MODEL


class InternalReport(FrozenModel):
    """供代理人复核的完整对内报告。"""

    report_type: str = "internal"
    kernel_hash: str
    markdown: str
    customer_analysis: CustomerAnalysis
    why_blocks: tuple[WhyBlock, ...]
    dimension_narratives: tuple[DimensionNarrative, ...]
    action_block: ActionBlock
    action_narrative: ActionNarrative
    meeting_plan: MeetingPlan
    validation_results: tuple[str, ...]
    audit_manifest: dict[str, Any] = Field(default_factory=dict)


class ReviewPacket(FrozenModel):
    """步骤 4 提交给代理人的复核包。"""

    review_id: str
    revision: int
    kernel_hash: str
    internal_report: InternalReport
    customer_identity: tuple[str, ...]
    trigger_context: tuple[str, ...]
    dimension_facts: tuple[DimensionFact, ...]
    auxiliary_observations: tuple[str, ...]
    preserve_items: tuple[str, ...]
    correction_items: tuple[str, ...]
    warnings: tuple[str, ...]
    diff_summary: tuple[str, ...] = ()


class ReviewDecision(FrozenModel):
    """代理人对当前复核包的结构化决定。"""

    action: ReviewAction
    reviewer_id: str
    expected_review_revision: int = Field(ge=1)
    expected_kernel_hash: str = Field(min_length=8)
    note: str = ""
    expected_profile_version: int | None = Field(default=None, ge=1)
    fact_patches: dict[str, Any] = Field(default_factory=dict)
    trigger_ids: tuple[str, ...] = ()
    narrative_preferences: dict[str, Any] = Field(default_factory=dict)


class ReviewPatchExtraction(FrozenModel):
    """代理人自然语言复核意见的候选变更。"""

    fact_patches: dict[str, Any] = Field(default_factory=dict)
    trigger_ids: tuple[str, ...] = ()
    narrative_preferences: dict[str, Any] = Field(default_factory=dict)
    unresolved: tuple[str, ...] = ()


class ScenarioSimulation(FrozenModel):
    """对客报告中带事实锚点的情境模拟。"""

    title: str
    trigger_condition: str
    reasoning_steps: tuple[str, ...]
    conclusion: str
    fact_refs: tuple[str, ...]
    dimension_refs: tuple[DimensionCode, ...] = ()
    assumptions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    action_or_preserve: str = ""


class CustomerFamilyMember(FrozenModel):
    """对客报告中的家庭成员摘要。"""

    role: str
    name: str
    summary: str
    is_core: bool = False


class CustomerDimensionNarrative(FrozenModel):
    """对客报告单个维度的受控文案。"""

    dimension_code: DimensionCode
    headline: str
    calculation_explanation: tuple[str, ...]
    why_it_matters: str
    suggestion: str
    fact_refs: tuple[str, ...] = ()


class CustomerReportMetric(FrozenModel):
    """对客报告中的家庭财务摘要指标。"""

    label: str
    value: str
    note: str = ""


class FutureOutlookItem(FrozenModel):
    """对客报告中的阶段性未来检视安排。"""

    phase: str
    title: str
    summary: str


class CustomerReportCopy(FrozenModel):
    """对客 HTML Harness 允许模型填写的文案字段。"""

    salutation: str
    opening_paragraphs: tuple[str, ...]
    family_summary: str
    status_summary: str
    dimension_narratives: tuple[CustomerDimensionNarrative, ...]
    roadmap_intro: str
    preserve_intro: str
    closing_paragraphs: tuple[str, ...]
    knowledge_refs: tuple[str, ...] = ()
    generation_mode: GenerationMode = GenerationMode.MODEL


class CustomerReportViewModel(FrozenModel):
    """对客 HTML 模板的受控输入。"""

    report_id: str
    customer_name: str
    approved_kernel_hash: str
    revision: int
    generated_at: datetime
    summary: str
    salutation: str
    opening_paragraphs: tuple[str, ...]
    family_members: tuple[CustomerFamilyMember, ...]
    family_summary: str
    financial_snapshot: tuple[CustomerReportMetric, ...]
    status_summary: str
    diagnosis_dimensions: tuple[DimensionFact, ...]
    dimension_narratives: tuple[CustomerDimensionNarrative, ...]
    auxiliary_observations: tuple[str, ...]
    preserve_items: tuple[str, ...]
    preserve_intro: str
    priority_actions: tuple[ActionItem, ...]
    priority_action_copy: tuple[ActionNarrativeItem, ...]
    roadmap_intro: str
    budget_tradeoff: str | None = None
    scenario_simulations: tuple[ScenarioSimulation, ...]
    future_outlook: tuple[FutureOutlookItem, ...]
    assumptions_and_limits: tuple[str, ...]
    closing_paragraphs: tuple[str, ...]
    disclaimer: str
    generation_modes: dict[str, GenerationMode] = Field(default_factory=dict)
    knowledge_refs: tuple[str, ...] = ()


class CustomerReportArtifact(FrozenModel):
    """最终对客 HTML 与审计清单。"""

    report_type: str = "customer"
    kernel_hash: str
    html: str
    view_model: CustomerReportViewModel
    manifest: dict[str, Any]
    validation_results: tuple[str, ...]
