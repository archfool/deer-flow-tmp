"""步骤 2 请求构建与步骤 3 确定性诊断内核。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.insurance.coverage_review.hashing import sha256_digest
from app.insurance.coverage_review.models import (
    DIMENSION_ORDER,
    AssumptionItem,
    CalculationRequest,
    CalculationResult,
    ConfidenceLevel,
    CorrectionItem,
    CustomerRoute,
    CustomerRouteDecision,
    DiagnosisKernel,
    DimensionCode,
    DimensionFact,
    DimensionState,
    EvidenceBundle,
    MeasurementType,
    PreserveItem,
    RouteConfidence,
)
from app.insurance.coverage_review.rules import RuleBundle


def build_calculation_request(evidence: EvidenceBundle) -> CalculationRequest:
    """从步骤 1 证据包构造精确测算 Tool 请求。

    Args:
        evidence: 已完成字段闸门的证据包。

    Returns:
        单位明确且带证据引用的测算请求。

    Raises:
        ValueError: 必需字段仍缺失时抛出。
    """

    required = (
        "annual_income_wan",
        "spouse_annual_income_wan",
        "family_expense_yuan_month",
        "large_loan_wan",
        "investment_risk_tolerance",
        "existing_disease_coverage_wan",
        "existing_medical_responsibility_tier",
        "existing_disability_coverage_wan",
        "existing_care_coverage_wan",
        "existing_death_coverage_wan",
        "existing_wealth_reserve_wan",
        "existing_retirement_cashflow_yuan_year",
        "existing_legacy_reserve_wan",
    )
    missing = [key for key in required if evidence.field_value(key) is None]
    if missing:
        raise ValueError(f"calculation request misses required fields: {missing}")
    evidence_hash = sha256_digest(evidence)
    member_ref = evidence.customer_id
    return CalculationRequest(
        request_id=f"calc-request-{evidence_hash.removeprefix('sha256:')[:16]}",
        customer_id=evidence.customer_id,
        member_id=member_ref,
        annual_income_wan=Decimal(str(evidence.field_value("annual_income_wan"))),
        spouse_annual_income_wan=Decimal(str(evidence.field_value("spouse_annual_income_wan"))),
        family_expense_yuan_month=Decimal(str(evidence.field_value("family_expense_yuan_month"))),
        large_loan_wan=Decimal(str(evidence.field_value("large_loan_wan"))),
        investment_risk_tolerance=str(evidence.field_value("investment_risk_tolerance")),
        existing_disease_coverage_wan=Decimal(str(evidence.field_value("existing_disease_coverage_wan"))),
        existing_medical_responsibility_tier=Decimal(str(evidence.field_value("existing_medical_responsibility_tier"))),
        existing_disability_coverage_wan=Decimal(str(evidence.field_value("existing_disability_coverage_wan"))),
        existing_care_coverage_wan=Decimal(str(evidence.field_value("existing_care_coverage_wan"))),
        existing_death_coverage_wan=Decimal(str(evidence.field_value("existing_death_coverage_wan"))),
        existing_wealth_reserve_wan=Decimal(str(evidence.field_value("existing_wealth_reserve_wan"))),
        existing_retirement_cashflow_yuan_year=Decimal(str(evidence.field_value("existing_retirement_cashflow_yuan_year"))),
        existing_legacy_reserve_wan=Decimal(str(evidence.field_value("existing_legacy_reserve_wan"))),
        evidence_refs=tuple(item.source_ref for item in evidence.fields if item.field_key in required),
    )


def build_diagnosis_kernel(
    *,
    review_id: str,
    revision: int,
    evidence: EvidenceBundle,
    calculation: CalculationResult,
    bundle: RuleBundle,
) -> DiagnosisKernel:
    """由权威测算和规则包生成不可变诊断内核。

    Args:
        review_id: 当前保障检视业务 ID。
        revision: 业务修订号。
        evidence: 步骤 1 证据包。
        calculation: 步骤 2 权威测算结果。
        bundle: 当前签名规则包。

    Returns:
        不含销售文案的 DiagnosisKernel。
    """

    route = _resolve_customer_route(evidence, bundle)
    assumptions = _assumption_items(evidence.assumptions)
    facts = [
        _dimension_fact(
            item=item,
            evidence=evidence,
            route=route,
            assumptions=assumptions,
            bundle=bundle,
        )
        for item in calculation.dimensions
    ]
    ranked = _rank_dimension_facts(facts)
    preserve_items = _preserve_items(ranked)
    correction_items = _correction_items(ranked, evidence)
    priority_order = tuple(
        item.dimension_code
        for item in sorted(
            (fact for fact in ranked if fact.priority_rank is not None),
            key=lambda fact: fact.priority_rank or 99,
        )
    )
    evidence_hash = sha256_digest(evidence)
    calculation_hash = sha256_digest(
        {
            "dimensions": [item.model_dump(mode="json") for item in calculation.dimensions],
            "auxiliary_metrics": [item.model_dump(mode="json") for item in calculation.auxiliary_metrics],
            "tool_version": calculation.tool_version,
        }
    )
    payload: dict[str, Any] = {
        "review_id": review_id,
        "revision": revision,
        "customer_id": evidence.customer_id,
        "as_of_date": evidence.as_of_date.isoformat(),
        "evidence_hash": evidence_hash,
        "calculation_hash": calculation_hash,
        "calculator_tool_version": calculation.tool_version,
        "rule_bundle_hash": bundle.bundle_hash,
        "policy_inventory_confirmed": bool(evidence.field_value("policy_inventory_confirmed", False)),
        "annual_premium_budget_yuan": evidence.field_value("annual_premium_budget_yuan"),
        "trigger_binding": evidence.trigger_binding.model_dump(mode="json"),
        "customer_route": route.model_dump(mode="json"),
        "dimension_facts": [item.model_dump(mode="json") for item in ranked],
        "auxiliary_diagnostics": [item.model_dump(mode="json") for item in calculation.auxiliary_metrics],
        "preserve_items": [item.model_dump(mode="json") for item in preserve_items],
        "correction_items": [item.model_dump(mode="json") for item in correction_items],
        "priority_order": [item.value for item in priority_order],
        "assumptions": [item.model_dump(mode="json") for item in assumptions],
        "conflicts": list(evidence.conflicts),
        "capabilities": bundle.diagnosis_rules["capabilities"],
    }
    return DiagnosisKernel(
        **payload,
        kernel_hash=sha256_digest(payload),
    )


def _dimension_fact(
    *,
    item: Any,
    evidence: EvidenceBundle,
    route: CustomerRouteDecision,
    assumptions: tuple[AssumptionItem, ...],
    bundle: RuleBundle,
) -> DimensionFact:
    """生成单维诊断事实。"""

    rule = bundle.dimension(item.dimension_code)
    no_need = _is_no_need(item.dimension_code, evidence, route)
    gap_ratio = _gap_ratio(item)
    state = _dimension_state(item, gap_ratio, no_need, bundle)
    if state is DimensionState.SUFFICIENT:
        reason_codes = ("COVER_SUFFICIENT",)
    elif state is DimensionState.NO_NEED:
        reason_codes = ("NO_NEED_LIFESTAGE",)
    elif state is DimensionState.UNKNOWN:
        reason_codes = (rule.default_reason_code,)
    else:
        codes = [rule.default_reason_code]
        if item.dimension_code is DimensionCode.DEATH and Decimal(str(evidence.field_value("large_loan_wan", 0))) > 0 and (item.existing_value or Decimal("0")) < Decimal(str(evidence.field_value("large_loan_wan", 0))) * Decimal("10000"):
            codes.append("DEBT_UNCOVERED")
        reason_codes = tuple(codes)

    confidence = _dimension_confidence(item.dimension_code, assumptions)
    severity_weights = bundle.diagnosis_rules["severity_weights"]
    severity = Decimal(str(severity_weights[state.value]))
    exposure = Decimal(str(rule.risk_exposure))
    if item.dimension_code is DimensionCode.DEATH:
        exposure = Decimal(str(bundle.diagnosis_rules["conditional_exposure"]["D4_with_dependants" if evidence.field_value("has_dependants", False) else "D4_without_dependants"]))
    trigger_weight = Decimal(str(bundle.diagnosis_rules["trigger_weights"]["direct"])) if item.dimension_code in evidence.trigger_binding.focus_dimensions else Decimal("0")
    urgency = severity * exposure + trigger_weight
    urgency_reasons: list[str] = []
    if severity >= 3:
        urgency_reasons.append("SEVERITY_HIGH")
    elif severity >= 2:
        urgency_reasons.append("SEVERITY_MEDIUM")
    if exposure >= 3:
        urgency_reasons.append("EXPOSURE_FAMILY")
    elif exposure >= 2:
        urgency_reasons.append("EXPOSURE_MAJOR")
    if trigger_weight > 0:
        urgency_reasons.append("TRIGGER_MATCH")
    if item.recommend_first:
        urgency_reasons.append("TOOL_PRIORITY_HINT")

    return DimensionFact(
        dimension_code=item.dimension_code,
        dimension_name=rule.name,
        state=state,
        measurement_type=item.measurement_type,
        existing_value=item.existing_value,
        ideal_value=item.ideal_value,
        gap_value=item.gap_value,
        gap_ratio=gap_ratio,
        unit=item.unit,
        reason_codes=reason_codes,
        formula_version=item.formula_version,
        derivation_components=item.derivation_components,
        urgency_score=urgency,
        urgency_reason_codes=tuple(urgency_reasons),
        confidence=confidence,
        requires_agent_review=(confidence in {ConfidenceLevel.ASSUMED, ConfidenceLevel.UNKNOWN} or state is DimensionState.UNKNOWN),
        tool_priority_hint=item.recommend_first,
        fact_refs=tuple(
            dict.fromkeys(
                [
                    *item.calculation_refs,
                    *(field.source_ref for field in evidence.fields),
                ]
            )
        ),
        calculation_refs=item.calculation_refs,
    )


def _gap_ratio(item: Any) -> Decimal | None:
    """仅对同量纲金额或现金流计算缺口率。"""

    if item.measurement_type is MeasurementType.LIABILITY_TIER:
        return None
    if item.ideal_value is None or item.ideal_value <= 0 or item.gap_value is None:
        return None
    return (item.gap_value / item.ideal_value).quantize(Decimal("0.0001"))


def _dimension_state(
    item: Any,
    gap_ratio: Decimal | None,
    no_need: bool,
    bundle: RuleBundle,
) -> DimensionState:
    """根据各维量纲和阈值生成缺口档位。"""

    if no_need:
        return DimensionState.NO_NEED
    if item.measurement_type is MeasurementType.LIABILITY_TIER:
        if item.is_full:
            return DimensionState.SUFFICIENT
        if (item.existing_value or Decimal("0")) <= 0:
            return DimensionState.SEVERE_GAP
        return DimensionState.SIGNIFICANT_GAP
    if item.ideal_value in {None, Decimal("0")}:
        return DimensionState.SUFFICIENT if item.is_full else DimensionState.UNKNOWN
    if gap_ratio is None:
        return DimensionState.UNKNOWN

    thresholds = bundle.diagnosis_rules["severity_thresholds"]
    if gap_ratio < Decimal(str(thresholds["sufficient_upper_exclusive"])):
        return DimensionState.SUFFICIENT
    if gap_ratio < Decimal(str(thresholds["mild_upper_exclusive"])):
        return DimensionState.MILD_GAP
    if gap_ratio <= Decimal(str(thresholds["significant_upper_inclusive"])):
        return DimensionState.SIGNIFICANT_GAP
    return DimensionState.SEVERE_GAP


def _rank_dimension_facts(facts: list[DimensionFact]) -> tuple[DimensionFact, ...]:
    """按紧急度、工具提示和固定维度顺序生成最终序位。"""

    candidates = [
        fact
        for fact in facts
        if fact.state
        in {
            DimensionState.MILD_GAP,
            DimensionState.SIGNIFICANT_GAP,
            DimensionState.SEVERE_GAP,
        }
    ]
    sorted_candidates = sorted(
        candidates,
        key=lambda fact: (
            -fact.urgency_score,
            -int(fact.tool_priority_hint),
            DIMENSION_ORDER.index(fact.dimension_code),
        ),
    )
    ranks = {fact.dimension_code: index for index, fact in enumerate(sorted_candidates, start=1)}
    return tuple(fact.model_copy(update={"priority_rank": ranks.get(fact.dimension_code)}) for fact in facts)


def _preserve_items(
    facts: tuple[DimensionFact, ...],
) -> tuple[PreserveItem, ...]:
    """从充足维度或已有有效基础生成一等保留项。"""

    items: list[PreserveItem] = []
    for fact in facts:
        has_existing_value = fact.existing_value is not None and fact.existing_value > 0
        if fact.state is not DimensionState.SUFFICIENT and not has_existing_value:
            continue
        redundant = fact.ideal_value is not None and fact.ideal_value > 0 and fact.existing_value is not None and fact.existing_value >= fact.ideal_value * Decimal("1.5")
        reason = "REDUNDANT_COVERAGE" if redundant else "COVER_SUFFICIENT"
        items.append(
            PreserveItem(
                item_id=f"preserve-{fact.dimension_code.value}",
                dimension_code=fact.dimension_code,
                object_ref=(fact.dimension_name if fact.state is DimensionState.SUFFICIENT else f"{fact.dimension_name}已有部分"),
                reason_code=reason,
                confidence=fact.confidence,
                capability_available=True,
                fact_refs=fact.fact_refs,
            )
        )
    return tuple(items)


def _correction_items(
    facts: tuple[DimensionFact, ...],
    evidence: EvidenceBundle,
) -> tuple[CorrectionItem, ...]:
    """根据跨维事实生成第一阶段可用的纠错项。"""

    by_code = {fact.dimension_code: fact for fact in facts}
    wealth = by_code[DimensionCode.WEALTH]
    death = by_code[DimensionCode.DEATH]
    if evidence.field_value("has_dependants", False) and (wealth.existing_value or Decimal("0")) > 0 and death.state is DimensionState.SEVERE_GAP:
        return (
            CorrectionItem(
                item_id="correction-structure-wealth-death",
                correction_type="structure_misallocation",
                dimension_codes=(DimensionCode.WEALTH, DimensionCode.DEATH),
                reason_code="STRUCTURE_MISALLOC",
                confidence=ConfidenceLevel.MEASURED,
                capability_available=True,
                fact_refs=tuple(dict.fromkeys([*wealth.fact_refs, *death.fact_refs])),
            ),
        )
    return ()


def _resolve_customer_route(
    evidence: EvidenceBundle,
    bundle: RuleBundle,
) -> CustomerRouteDecision:
    """按财富层级和触发场景确定标准或高客分线。"""

    wealth_level = str(evidence.field_value("wealth_level_name", ""))
    trigger_ids = set(evidence.trigger_binding.trigger_ids)
    route_rules = bundle.diagnosis_rules["route_rules"]
    if wealth_level in route_rules["high_net_worth_wealth_levels"] or route_rules["legacy_trigger"] in trigger_ids or (route_rules["business_owner_trigger"] in trigger_ids and evidence.field_value("business_owner", False)):
        return CustomerRouteDecision(
            route=CustomerRoute.HIGH_NET_WORTH,
            confidence=RouteConfidence.HIGH,
            basis_codes=("HIGH_NET_WORTH_SIGNAL",),
        )
    if wealth_level in route_rules["middle_wealth_levels"] and (evidence.field_value("business_owner", False) or "C3" in trigger_ids):
        return CustomerRouteDecision(
            route=CustomerRoute.NEEDS_CONFIRMATION,
            confidence=RouteConfidence.LOW,
            basis_codes=("MIDDLE_WEALTH_MIXED_SIGNAL",),
        )
    return CustomerRouteDecision(
        route=CustomerRoute.STANDARD,
        confidence=RouteConfidence.HIGH,
        basis_codes=("STANDARD_WEALTH_ROUTE",),
    )


def _is_no_need(
    code: DimensionCode,
    evidence: EvidenceBundle,
    route: CustomerRouteDecision,
) -> bool:
    """执行护理和传承的阶段性无需求排除。"""

    age = int(evidence.field_value("age", 0))
    high_route = route.route is CustomerRoute.HIGH_NET_WORTH
    if code is DimensionCode.LONG_TERM_CARE and age < 50 and not high_route:
        return True
    if code is DimensionCode.LEGACY:
        business_owner = bool(evidence.field_value("business_owner", False))
        return not high_route and not business_owner
    return False


def _assumption_items(assumptions: tuple[str, ...]) -> tuple[AssumptionItem, ...]:
    """把证据包的默认值说明转换为结构化假设。"""

    mapping = {
        "annual_income_wan": (DimensionCode.DISEASE, DimensionCode.DISABILITY, DimensionCode.DEATH),
        "spouse_annual_income_wan": (DimensionCode.DEATH,),
        "family_expense_yuan_month": (DimensionCode.DEATH, DimensionCode.RETIREMENT),
        "large_loan_wan": (DimensionCode.DEATH,),
        "investment_risk_tolerance": (DimensionCode.WEALTH, DimensionCode.RETIREMENT),
        "review_scope": (),
    }
    items: list[AssumptionItem] = []
    for description in assumptions:
        field_key = description.split(" ", 1)[0]
        items.append(
            AssumptionItem(
                field_key=field_key,
                source=description,
                affected_dimensions=mapping.get(field_key, ()),
            )
        )
    return tuple(items)


def _dimension_confidence(
    code: DimensionCode,
    assumptions: tuple[AssumptionItem, ...],
) -> ConfidenceLevel:
    """按假设影响范围确定单维置信度。"""

    if any(code in item.affected_dimensions for item in assumptions):
        return ConfidenceLevel.ASSUMED
    return ConfidenceLevel.MEASURED
