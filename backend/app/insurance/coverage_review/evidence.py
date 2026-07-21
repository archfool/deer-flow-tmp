"""步骤 1 的证据采集、报告抽取、触发合成与追问计划。"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.insurance.coverage_review.models import (
    DIMENSION_ORDER,
    ConfidenceLevel,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSource,
    PolicyFact,
    PolicyReportExtraction,
    QuestionItem,
    TemperatureType,
    TriggerBinding,
    VerificationStatus,
)
from app.insurance.coverage_review.rules import FieldRule, RuleBundle
from app.insurance.coverage_review.tools import (
    CustomerCenterRecord,
    CustomerProfileSnapshot,
    RawPolicyReport,
)
from app.insurance.models import (
    CustomerProfile,
    MedicalResponsibility,
    PolicyCategory,
)

_TEMPERATURE_RANK = {
    TemperatureType.FEAR: 0,
    TemperatureType.DEFENSIVE: 1,
    TemperatureType.COLD_START: 2,
    TemperatureType.NEUTRAL: 3,
    TemperatureType.OPEN: 4,
}

_COVERAGE_FIELD_KEYS = (
    "existing_disease_coverage_wan",
    "existing_medical_responsibility_tier",
    "existing_disability_coverage_wan",
    "existing_care_coverage_wan",
    "existing_death_coverage_wan",
    "existing_wealth_reserve_wan",
    "existing_retirement_cashflow_yuan_year",
    "existing_legacy_reserve_wan",
)


def extract_policy_report(report: RawPolicyReport) -> PolicyReportExtraction:
    """从中保信原始文本提取可引用事实并识别冲突。

    Args:
        report: 中保信 Tool 返回的原始报告。

    Returns:
        结构化保单报告抽取结果。
    """

    text = report.content
    total_match = re.search(r"有效保单总数[：:]?\s*\*{0,2}(\d+)", text)
    facts: list[PolicyFact] = []
    patterns = (
        ("paid_premium", "累计已交保费", r"累计已交保费[：:]?\s*\*{0,2}([\d.]+)", "万元"),
        ("death_total", "身故保障", r"身故总保额[：:]?\s*\*{0,2}([\d.]+)", "万元"),
        ("critical_illness_total", "疾病保障", r"合计重疾[：:]?\s*\*{0,2}([\d.]+)", "万元"),
        ("medical_total", "医疗保障", r"医疗(?:赔付|报销)[：:]?\s*\*{0,2}([\d.]+)", "万元"),
        ("annuity_total", "养老储蓄", r"(?:未来生存金总额|生存金/生存类领取总额)[：:]?\s*\*{0,2}([\d.]+)", "万元"),
    )
    for fact_id, category, pattern, unit in patterns:
        match = re.search(pattern, text)
        if match is None:
            continue
        facts.append(
            PolicyFact(
                fact_id=fact_id,
                category=category,
                value=_to_decimal(match.group(1)),
                unit=unit,
                source_excerpt=_excerpt(text, match.start(), match.end()),
            )
        )

    care_values = [_to_decimal(value) for value in re.findall(r"(?:护理责任保障|长期护理保险仅)[：:]?\s*\*{0,2}([\d.]+)", text)]
    conflicts: list[str] = []
    if care_values:
        for index, value in enumerate(care_values, start=1):
            facts.append(
                PolicyFact(
                    fact_id=f"long_term_care_{index}",
                    category="护理保障",
                    value=value,
                    unit="万元",
                    source_excerpt=f"护理责任候选值 {value} 万元",
                    confidence=(ConfidenceLevel.UNKNOWN if len(set(care_values)) > 1 else ConfidenceLevel.MEASURED),
                )
            )
        if len(set(care_values)) > 1:
            conflicts.append("护理责任存在互相冲突的金额，不能直接进入诊断内核：" + " / ".join(str(value) for value in care_values) + " 万元")

    warnings: list[str] = []
    if "被保人视图" in text or "投保人视图" in text:
        warnings.append("成员保单数来自不同视图，禁止简单求和")
    if "建议" in text:
        warnings.append("原报告建议只作为线索，不是诊断事实")
    return PolicyReportExtraction(
        report_id=report.report_id,
        total_active_policies=int(total_match.group(1)) if total_match else None,
        policy_facts=tuple(facts),
        conflicts=tuple(conflicts),
        warnings=tuple(warnings),
        source_hash=report.content_hash,
    )


def resolve_trigger_binding(
    trigger_ids: list[str] | tuple[str, ...],
    bundle: RuleBundle,
) -> TriggerBinding:
    """把一个或多个触发场景合成为唯一调性契约。

    Args:
        trigger_ids: 代理人确认的触发场景编号。
        bundle: 当前签名规则包。

    Returns:
        主触发、维度并集、情绪温度和护栏。

    Raises:
        ValueError: 未提供触发或编号不存在时抛出。
    """

    normalized = tuple(dict.fromkeys(value.strip().upper() for value in trigger_ids if value.strip()))
    if not normalized:
        raise ValueError("at least one trigger is required")
    rules = tuple(bundle.trigger(trigger_id) for trigger_id in normalized)
    primary = min(
        rules,
        key=lambda item: (
            _TEMPERATURE_RANK[item.temperature],
            0 if item.id == "B2" else 1 if item.id == "B1" else 2,
            normalized.index(item.id),
        ),
    )
    focus_set = {code for item in rules for code in item.focus_dimensions}
    focus = tuple(code for code in DIMENSION_ORDER if code in focus_set)
    style_contracts = bundle.style_contracts["contracts"].get(primary.temperature.value, [])
    guardrails = tuple(
        dict.fromkeys(
            [
                *bundle.style_contracts.get("universal_guardrails", []),
                *(guardrail for item in rules for guardrail in item.guardrails),
            ]
        )
    )
    return TriggerBinding(
        primary_trigger_id=primary.id,
        trigger_ids=normalized,
        temperature=primary.temperature,
        focus_dimensions=focus,
        auxiliary_focus=any(item.auxiliary_focus for item in rules),
        tone_contract=tuple([primary.tone, *style_contracts]),
        guardrails=guardrails,
        scenario_materials=tuple(item.scenario for item in rules),
    )


def collect_field_evidence(
    *,
    profile: CustomerProfile,
    center: CustomerCenterRecord,
    profile_snapshot: CustomerProfileSnapshot,
    answers: dict[str, Any],
    bundle: RuleBundle,
) -> tuple[tuple[EvidenceItem, ...], tuple[FieldRule, ...], tuple[str, ...]]:
    """收集精算字段并返回缺失项和已批准假设。

    Args:
        profile: 当前客户档案。
        center: 客户中心五字段记录。
        profile_snapshot: 内部档案版本快照。
        answers: 代理人在步骤 1.4 确认的补充值。
        bundle: 当前规则包。

    Returns:
        字段证据、阻塞缺失规则和假设说明。
    """

    now = datetime.now(UTC)
    self_member = next((item for item in profile.members if item.relationship.value == "self"), None)
    fields: list[EvidenceItem] = [
        _evidence("customer_name", center.customer_name, EvidenceSource.CUSTOMER_CENTER, center.source_ref, now),
        _evidence("age", center.age, EvidenceSource.CUSTOMER_CENTER, center.source_ref, now),
        _evidence("gender", center.gender, EvidenceSource.CUSTOMER_CENTER, center.source_ref, now),
        _evidence("life_stage_name", center.life_stage_name, EvidenceSource.CUSTOMER_CENTER, center.source_ref, now),
        _evidence("wealth_level_name", center.wealth_level_name, EvidenceSource.CUSTOMER_CENTER, center.source_ref, now),
        _evidence(
            "occupation",
            self_member.occupation if self_member is not None else None,
            EvidenceSource.CUSTOMER_PROFILE,
            profile_snapshot.source_ref,
            now,
        ),
        _evidence(
            "has_dependants",
            any(item.relationship.value in {"child", "parent"} for item in profile.members),
            EvidenceSource.DERIVED,
            profile_snapshot.source_ref,
            now,
        ),
        _evidence(
            "business_owner",
            financial_is_business_owner(profile),
            EvidenceSource.DERIVED,
            profile_snapshot.source_ref,
            now,
        ),
    ]
    financial = profile.financial
    profile_values: dict[str, Any] = {
        "annual_income_wan": _yuan_to_wan(financial.primary_annual_income),
        "spouse_annual_income_wan": _spouse_income(profile),
        "family_expense_yuan_month": financial.monthly_expenses,
        "large_loan_wan": _loan_wan(profile),
        **_coverage_profile_values(profile),
        "annual_premium_budget_yuan": financial.annual_premium_budget,
        "investment_risk_tolerance": (financial.investment_risk_tolerance.value if financial.investment_risk_tolerance is not None else None),
        "review_scope": None,
        "customer_goal": None,
        "recent_concern": None,
        "risk_attitude": None,
    }
    assumptions: list[str] = []
    missing: list[FieldRule] = []
    source_ref = profile_snapshot.source_ref

    for rule in bundle.fields:
        if rule.key == "trigger_ids":
            continue
        answer_supplied = rule.key in answers
        value = answers.get(rule.key) if answer_supplied else profile_values.get(rule.key)
        source = EvidenceSource.AGENT_CONFIRMATION if answer_supplied else EvidenceSource.CUSTOMER_PROFILE
        evidence_ref = f"agent-answer:{rule.key}" if answer_supplied else source_ref
        if value is None and rule.key in bundle.approved_defaults:
            default = bundle.approved_defaults[rule.key]
            value = default.get("value")
            assumptions.append(f"{rule.key} 使用批准默认值 {value}")
            source = EvidenceSource.DERIVED
            evidence_ref = f"approved-default:{rule.key}"
        if value is None:
            if rule.blocking:
                missing.append(rule)
            continue
        normalized = _normalize_field_value(rule.key, value)
        fields.append(_evidence(rule.key, normalized, source, evidence_ref, now))

    collected_keys = {item.field_key for item in fields}
    if all(key in collected_keys for key in _COVERAGE_FIELD_KEYS):
        answer_confirmed = any(key in answers for key in _COVERAGE_FIELD_KEYS)
        fields.append(
            _evidence(
                "policy_inventory_confirmed",
                True,
                EvidenceSource.AGENT_CONFIRMATION if answer_confirmed else EvidenceSource.CUSTOMER_PROFILE,
                "agent-answer:coverage-inventory" if answer_confirmed else source_ref,
                now,
            )
        )

    return tuple(fields), tuple(missing), tuple(assumptions)


def build_question_items(
    missing: tuple[FieldRule, ...],
    *,
    bundle: RuleBundle,
) -> tuple[QuestionItem, ...]:
    """按 P0/P1/P2 和表格顺序生成一轮追问。

    Args:
        missing: 当前缺失的字段规则。
        bundle: 当前规则包。

    Returns:
        最多五个结构化追问。
    """

    max_fields = 16
    priority = {"P0": 0, "P1": 1, "P2": 2}
    ordered = sorted(missing, key=lambda item: (priority.get(item.priority, 9), item.key))
    return tuple(
        QuestionItem(
            field_key=item.key,
            prompt=item.prompt,
            reason=item.reason,
            priority=item.priority,
            blocking=item.blocking,
            sensitive=item.sensitive,
        )
        for item in ordered[:max_fields]
    )


def build_enhancement_questions(
    existing_keys: set[str],
    bundle: RuleBundle,
) -> tuple[QuestionItem, ...]:
    """生成不阻塞测算的面谈增益问题。

    Args:
        existing_keys: 已有字段名集合。
        bundle: 当前规则包。

    Returns:
        对内报告中的待确认问题。
    """

    return tuple(
        QuestionItem(
            field_key=item.key,
            prompt=item.prompt,
            reason=item.reason,
            priority=item.priority,
            blocking=False,
            sensitive=item.sensitive,
        )
        for item in bundle.fields
        if not item.blocking and item.key not in existing_keys
    )


def build_evidence_bundle(
    *,
    profile: CustomerProfile,
    profile_version: int,
    center: CustomerCenterRecord,
    fields: tuple[EvidenceItem, ...],
    policy_report: PolicyReportExtraction,
    trigger_binding: TriggerBinding,
    assumptions: tuple[str, ...],
    bundle: RuleBundle,
) -> EvidenceBundle:
    """固化步骤 1 的完整证据包。

    Args:
        profile: 当前客户档案。
        profile_version: 档案版本。
        center: 客户中心记录。
        fields: 已完成的字段证据。
        policy_report: 中保信结构化抽取。
        trigger_binding: 触发合成结果。
        assumptions: 已批准默认值说明。
        bundle: 当前规则包。

    Returns:
        可供精算和内核只读使用的 EvidenceBundle。
    """

    existing_keys = {item.field_key for item in fields}
    return EvidenceBundle(
        customer_id=profile.customer_id,
        customer_name=center.customer_name,
        as_of_date=date.today(),
        profile_version=profile_version,
        fields=fields,
        policy_report=policy_report,
        trigger_binding=trigger_binding,
        assumptions=assumptions,
        conflicts=policy_report.conflicts,
        enhancement_questions=build_enhancement_questions(existing_keys, bundle),
    )


def _evidence(
    key: str,
    value: Any,
    source: EvidenceSource,
    source_ref: str,
    as_of: datetime,
) -> EvidenceItem:
    """构造统一字段证据。"""

    return EvidenceItem(
        field_key=key,
        value=value,
        source_type=source,
        source_ref=source_ref,
        as_of=as_of,
        verification_status=(VerificationStatus.CONFIRMED if source in {EvidenceSource.AGENT_CONFIRMATION, EvidenceSource.CUSTOMER_PROFILE} else VerificationStatus.EXTRACTED),
    )


def _normalize_field_value(field_key: str, value: Any) -> Any:
    """按字段单位规范化代理人回答。"""

    numeric_fields = {
        "annual_income_wan",
        "spouse_annual_income_wan",
        "family_expense_yuan_month",
        "large_loan_wan",
        "existing_disease_coverage_wan",
        "existing_medical_responsibility_tier",
        "existing_disability_coverage_wan",
        "existing_care_coverage_wan",
        "existing_death_coverage_wan",
        "existing_wealth_reserve_wan",
        "existing_retirement_cashflow_yuan_year",
        "existing_legacy_reserve_wan",
        "annual_premium_budget_yuan",
    }
    if field_key not in numeric_fields:
        return value
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_key} must be a numeric value with the declared unit") from exc
    if result < 0:
        raise ValueError(f"{field_key} must not be negative")
    return result


def _yuan_to_wan(value: Decimal | None) -> Decimal | None:
    """把元转换为万元。"""

    return None if value is None else (value / Decimal("10000")).quantize(Decimal("0.01"))


def _spouse_income(profile: CustomerProfile) -> Decimal | None:
    """读取配偶收入并处理无配偶情形。"""

    if profile.marital_status is None:
        return None
    if profile.marital_status != "married":
        return Decimal("0")
    return _yuan_to_wan(profile.financial.spouse_annual_income)


def _loan_wan(profile: CustomerProfile) -> Decimal | None:
    """汇总已确认负债并转换为万元。"""

    if profile.financial.liabilities is None:
        return None
    total = sum((item.balance for item in profile.financial.liabilities), Decimal("0"))
    return _yuan_to_wan(total)


def _coverage_profile_values(profile: CustomerProfile) -> dict[str, Decimal | None]:
    """从已确认档案生成八维现有值。

    `policies=None` 表示清单未知，此时保障型维度全部保持未知；只有空列表才
    表示代理人或权威接口已确认没有商业保单。

    Args:
        profile: 当前客户档案。

    Returns:
        可直接进入字段证据的八维现有值，金额字段按声明单位返回。
    """

    self_member = next(
        (item for item in profile.members if item.relationship.value == "self"),
        None,
    )
    return {
        "existing_disease_coverage_wan": _policy_amount_wan(
            profile,
            {PolicyCategory.CRITICAL_ILLNESS},
        ),
        "existing_medical_responsibility_tier": _medical_tier(profile),
        "existing_disability_coverage_wan": _policy_amount_wan(
            profile,
            {PolicyCategory.ACCIDENT},
        ),
        "existing_care_coverage_wan": (None if profile.policies is None else Decimal("0")),
        "existing_death_coverage_wan": _policy_amount_wan(
            profile,
            {PolicyCategory.LIFE_TERM, PolicyCategory.LIFE_WHOLE},
        ),
        "existing_wealth_reserve_wan": _yuan_to_wan(profile.financial.liquid_assets),
        "existing_retirement_cashflow_yuan_year": (None if self_member is None or self_member.existing_commercial_retirement_monthly is None else self_member.existing_commercial_retirement_monthly * Decimal("12")),
        "existing_legacy_reserve_wan": _policy_amount_wan(
            profile,
            {PolicyCategory.LIFE_WHOLE},
        ),
    }


def _policy_amount_wan(
    profile: CustomerProfile,
    categories: set[PolicyCategory],
) -> Decimal | None:
    """汇总已确认有效保单金额并转换为万元。

    Args:
        profile: 当前客户档案。
        categories: 需要汇总的保单类别。

    Returns:
        已确认保额（万元）；清单或匹配保额未知时返回 None。
    """

    if profile.policies is None:
        return None
    matched = [item for item in profile.policies if item.status.value == "active" and item.category in categories]
    if not matched:
        return Decimal("0")
    if any(item.sum_assured is None for item in matched):
        return None
    return _yuan_to_wan(sum((item.sum_assured or Decimal("0") for item in matched), Decimal("0")))


def _medical_tier(profile: CustomerProfile) -> Decimal | None:
    """把已确认医疗责任映射为测算责任层级。

    Args:
        profile: 当前客户档案。

    Returns:
        0 表示确认无商业医疗，1 表示基础住院责任，2 表示扩展医疗责任；
        清单未知时返回 None。
    """

    if profile.policies is None:
        return None
    policies = [item for item in profile.policies if item.status.value == "active" and item.category is PolicyCategory.MEDICAL]
    if not policies:
        return Decimal("0")
    extended = {
        MedicalResponsibility.PRIVATE_HOSPITAL,
        MedicalResponsibility.INTERNATIONAL,
        MedicalResponsibility.GENERAL_OUTPATIENT,
    }
    responsibilities = {responsibility for policy in policies for responsibility in policy.medical_responsibilities}
    return Decimal("2" if responsibilities.intersection(extended) else "1")


def financial_is_business_owner(profile: CustomerProfile) -> bool:
    """判断档案是否明确标记企业主收入结构。

    Args:
        profile: 当前客户档案。

    Returns:
        收入稳定性或职业文本明确表示企业主时返回真。
    """

    if profile.financial.income_stability is not None:
        if profile.financial.income_stability.value == "business_owner":
            return True
    markers = ("企业主", "创业", "个体经营", "公司法人", "董事长", "实际控制人")
    return any(any(marker in (member.occupation or "") for marker in markers) for member in profile.members)


def _to_decimal(value: str) -> Decimal:
    """把报告数字转换为 Decimal。"""

    return Decimal(value)


def _excerpt(text: str, start: int, end: int, radius: int = 28) -> str:
    """截取报告事实附近的短引用。"""

    return text[max(0, start - radius) : min(len(text), end + radius)].replace("\n", " ")
