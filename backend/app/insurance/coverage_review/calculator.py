"""纯函数、确定性的六维保障检视计算。"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.insurance.coverage_review.models import (
    CoverageDimension,
    CoverageReviewResult,
    DimensionAssessment,
    DimensionStatus,
    ScenarioAssessment,
)
from app.insurance.coverage_review.parameters import CoverageReviewParameters
from app.insurance.models import CustomerProfile, PolicyCategory

_MONEY = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    """只在结果边界舍入，中间公式保留完整精度。"""

    return max(value, Decimal("0")).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _money_scenario(
    *,
    target: Decimal,
    current: Decimal,
    explanation: str,
    assumptions: tuple[str, ...],
) -> ScenarioAssessment:
    raw_gap = target - current
    return ScenarioAssessment(
        target=_money(target),
        current=_money(current),
        gap=_money(raw_gap),
        surplus=_money(-raw_gap),
        explanation=explanation,
        assumptions=assumptions,
    )


def _unavailable(dimension: CoverageDimension, missing: list[str], *notes: str) -> DimensionAssessment:
    return DimensionAssessment(
        dimension=dimension,
        status=DimensionStatus.UNAVAILABLE,
        missing_facts=tuple(sorted(set(missing))),
        notes=tuple(notes),
    )


def _common_missing(profile: CustomerProfile) -> list[str]:
    missing: list[str] = []
    if not profile.members:
        return ["/members"]
    for index, member in enumerate(profile.members):
        prefix = f"/members/{index}"
        for field in ("age", "gender", "occupation", "city"):
            if getattr(member, field) is None:
                missing.append(f"{prefix}/{field}")
    if not any(member.economic_pillar for member in profile.members):
        missing.append("/members/economic_pillar")
    return missing


def _income_share_missing(profile: CustomerProfile) -> list[str]:
    return [f"/members/{index}/income_share" for index, member in enumerate(profile.members) if member.economic_pillar and member.income_share is None]


def _active_policies(profile: CustomerProfile, as_of: date):
    return profile.effective_policies(as_of)


def _life(profile: CustomerProfile, parameters: CoverageReviewParameters, as_of: date) -> DimensionAssessment:
    missing = _common_missing(profile) + _income_share_missing(profile)
    financial = profile.financial
    if financial.monthly_expenses is None:
        missing.append("/financial/monthly_expenses")
    if financial.liabilities is None:
        missing.append("/financial/liabilities")
    if financial.liquid_assets is None:
        missing.append("/financial/liquid_assets")
    if profile.policies is None:
        missing.append("/policies")
    if any(member.relationship.value == "child" for member in profile.members) and profile.education_plans is None:
        missing.append("/education_plans")
    if missing:
        return _unavailable(CoverageDimension.LIFE, missing, "客户事实缺失，未将未知值视为零。")

    debt = sum((liability.balance for liability in financial.liabilities or []), Decimal("0"))
    education = sum((plan.target_amount for plan in profile.education_plans or []), Decimal("0"))
    parent_support = sum((plan.target_amount for plan in profile.parent_support_plans or []), Decimal("0"))
    annual_expense = financial.monthly_expenses * Decimal("12")
    liquid_assets = financial.liquid_assets
    policies = _active_policies(profile, as_of)
    direct_life = sum((policy.sum_assured or Decimal("0")) for policy in policies if policy.category in {PolicyCategory.LIFE_TERM, PolicyCategory.LIFE_WHOLE})
    accident_death = sum((policy.sum_assured or Decimal("0")) for policy in policies if policy.category is PolicyCategory.ACCIDENT)

    scenarios: dict[str, ScenarioAssessment] = {}
    for name, scenario in parameters.scenarios.items():
        # 负债、教育、赡养和流动资产都是家庭级数值。通过经济支柱收入占比之和
        # 进行分配，可以避免双经济支柱家庭重复计算。
        pillar_share = sum((member.income_share or Decimal("0")) for member in profile.members if member.economic_pillar)
        target = (debt + education + parent_support) * pillar_share + annual_expense * pillar_share * Decimal(scenario.life_replacement_years) - liquid_assets * scenario.liquid_asset_deduction_ratio
        current = direct_life + accident_death * scenario.accident_death_credit_ratio
        scenarios[name] = _money_scenario(
            target=target,
            current=current,
            explanation="目标由未偿负债、教育/赡养责任及家庭生活费替代需求组成，并扣除可动用流动资产。",
            assumptions=(
                f"收入/支出替代年限：{scenario.life_replacement_years} 年（DRAFT）",
                f"流动资产扣减比例：{scenario.liquid_asset_deduction_ratio:.0%}（DRAFT）",
                f"意外身故责任计入比例：{scenario.accident_death_credit_ratio:.0%}（DRAFT）",
            ),
        )
    return DimensionAssessment(dimension=CoverageDimension.LIFE, status=DimensionStatus.COMPLETE, scenarios=scenarios)


def _critical_illness(profile: CustomerProfile, parameters: CoverageReviewParameters, as_of: date) -> DimensionAssessment:
    missing = _common_missing(profile) + _income_share_missing(profile)
    if profile.financial.annual_income is None:
        missing.append("/financial/annual_income")
    if profile.policies is None:
        missing.append("/policies")
    if missing:
        return _unavailable(CoverageDimension.CRITICAL_ILLNESS, missing)

    pillars = [member for member in profile.members if member.economic_pillar]
    current = sum((policy.sum_assured or Decimal("0")) for policy in _active_policies(profile, as_of) if policy.category is PolicyCategory.CRITICAL_ILLNESS)
    scenarios = {}
    for name, scenario in parameters.scenarios.items():
        income_replacement = sum(profile.financial.annual_income * (member.income_share or Decimal("0")) * Decimal(scenario.critical_income_loss_years) for member in pillars)
        target = scenario.critical_treatment_cost * Decimal(len(pillars)) + income_replacement
        scenarios[name] = _money_scenario(
            target=target,
            current=current,
            explanation="目标同时覆盖一次性治疗/康复支出和重疾导致的收入中断，不只计算医疗费用。",
            assumptions=(
                f"每位经济支柱治疗康复费：{scenario.critical_treatment_cost}（DRAFT）",
                f"收入损失补偿：{scenario.critical_income_loss_years} 年（DRAFT）",
            ),
        )
    return DimensionAssessment(dimension=CoverageDimension.CRITICAL_ILLNESS, status=DimensionStatus.COMPLETE, scenarios=scenarios)


def _medical(profile: CustomerProfile, parameters: CoverageReviewParameters, as_of: date) -> DimensionAssessment:
    missing = _common_missing(profile)
    for index, member in enumerate(profile.members):
        if member.social_insurance_type is None:
            missing.append(f"/members/{index}/social_insurance_type")
    if profile.policies is None:
        missing.append("/policies")
    if missing:
        return _unavailable(CoverageDimension.MEDICAL, missing)

    policies = _active_policies(profile, as_of)
    scenarios: dict[str, ScenarioAssessment] = {}
    for name, scenario in parameters.scenarios.items():
        target: set[str] = set()
        current: set[str] = set()
        for member in profile.members:
            for responsibility in scenario.medical_target_responsibilities:
                target.add(f"{member.id}:{responsibility.value}")
            # 存在社保类型即视为具备社保基础责任；具体给付金额不属于当前责任分类。
            current.add(f"{member.id}:social_basic")
        for policy in policies:
            if policy.category is PolicyCategory.MEDICAL:
                current.update(f"{policy.insured_member_id}:{item.value}" for item in policy.medical_responsibilities)
        missing_responsibilities = target - current
        scenarios[name] = ScenarioAssessment(
            target_responsibilities=tuple(sorted(target)),
            current_responsibilities=tuple(sorted(current)),
            missing_responsibilities=tuple(sorted(missing_responsibilities)),
            explanation="医疗维度按成员和责任类型检查覆盖空白，不把责任差异压缩成一个保额数字。",
            assumptions=(f"目标责任组合采用 {name} DRAFT 场景。",),
        )
    return DimensionAssessment(dimension=CoverageDimension.MEDICAL, status=DimensionStatus.COMPLETE, scenarios=scenarios)


def _risk_coefficient(risk_class: int) -> Decimal:
    # 占位系数表保持显式且单调。在核保或业务负责人提供正式数值前，它始终是
    # DRAFT 参数。
    return {
        1: Decimal("1.0"),
        2: Decimal("1.0"),
        3: Decimal("1.2"),
        4: Decimal("1.5"),
        5: Decimal("1.8"),
        6: Decimal("2.0"),
    }[risk_class]


def _accident(profile: CustomerProfile, parameters: CoverageReviewParameters, as_of: date) -> DimensionAssessment:
    missing = _common_missing(profile) + _income_share_missing(profile)
    if profile.financial.annual_income is None:
        missing.append("/financial/annual_income")
    for index, member in enumerate(profile.members):
        if member.economic_pillar and member.occupation_risk_class is None:
            missing.append(f"/members/{index}/occupation_risk_class")
    if profile.policies is None:
        missing.append("/policies")
    if missing:
        return _unavailable(CoverageDimension.ACCIDENT, missing)

    policies = [policy for policy in _active_policies(profile, as_of) if policy.category is PolicyCategory.ACCIDENT]
    current_death = sum((policy.sum_assured or Decimal("0")) for policy in policies)
    current_medical = sum((policy.accident_medical_limit or Decimal("0")) for policy in policies)
    pillars = [member for member in profile.members if member.economic_pillar]
    scenarios = {}
    for name, scenario in parameters.scenarios.items():
        death_target = sum(profile.financial.annual_income * (member.income_share or Decimal("0")) * scenario.accident_income_multiple * _risk_coefficient(member.occupation_risk_class or 1) for member in pillars)
        medical_target = scenario.accident_medical_target * Decimal(len(pillars))
        scenarios[name] = _money_scenario(
            target=death_target + medical_target,
            current=current_death + current_medical,
            explanation="目标由经济支柱收入倍数、职业风险系数和意外医疗额度组成。",
            assumptions=(
                f"收入倍数：{scenario.accident_income_multiple}（DRAFT）",
                f"每位经济支柱意外医疗目标：{scenario.accident_medical_target}（DRAFT）",
                "职业类别系数为 DRAFT 占位表。",
            ),
        )
    return DimensionAssessment(dimension=CoverageDimension.ACCIDENT, status=DimensionStatus.COMPLETE, scenarios=scenarios)


def _retirement(profile: CustomerProfile, parameters: CoverageReviewParameters) -> DimensionAssessment:
    missing = _common_missing(profile) + _income_share_missing(profile)
    if profile.financial.annual_income is None:
        missing.append("/financial/annual_income")
    for index, member in enumerate(profile.members):
        if not member.economic_pillar:
            continue
        for field in (
            "desired_retirement_age",
            "desired_retirement_monthly_spending",
            "estimated_social_pension_monthly",
            "existing_commercial_retirement_monthly",
        ):
            if getattr(member, field) is None:
                missing.append(f"/members/{index}/{field}")
    if missing:
        return _unavailable(CoverageDimension.RETIREMENT, missing)

    pillars = [member for member in profile.members if member.economic_pillar]
    current = sum((member.estimated_social_pension_monthly or Decimal("0")) + (member.existing_commercial_retirement_monthly or Decimal("0")) for member in pillars)
    scenarios = {}
    for name, scenario in parameters.scenarios.items():
        target = sum(
            max(
                member.desired_retirement_monthly_spending or Decimal("0"),
                profile.financial.annual_income * (member.income_share or Decimal("0")) / Decimal("12") * scenario.retirement_replacement_rate,
            )
            for member in pillars
        )
        scenarios[name] = _money_scenario(
            target=target,
            current=current,
            explanation="目标月现金流取客户期望退休支出与收入替代率结果中的较高值。",
            assumptions=(
                f"退休收入替代率：{scenario.retirement_replacement_rate:.0%}（DRAFT）",
                "仅报告月现金流缺口；未调用正式利益演示接口，不反推产品保额或保费。",
            ),
        )
    return DimensionAssessment(dimension=CoverageDimension.RETIREMENT, status=DimensionStatus.COMPLETE, scenarios=scenarios)


def _emergency(profile: CustomerProfile, parameters: CoverageReviewParameters) -> DimensionAssessment:
    missing: list[str] = []
    if profile.financial.monthly_expenses is None:
        missing.append("/financial/monthly_expenses")
    if profile.financial.liquid_assets is None:
        missing.append("/financial/liquid_assets")
    if missing:
        return _unavailable(CoverageDimension.EMERGENCY_RESERVE, missing)
    scenarios = {}
    for name, scenario in parameters.scenarios.items():
        target = profile.financial.monthly_expenses * Decimal(scenario.emergency_months)
        scenarios[name] = _money_scenario(
            target=target,
            current=profile.financial.liquid_assets,
            explanation="应急储备以家庭月支出乘以目标覆盖月数衡量流动性。",
            assumptions=(f"目标覆盖月数：{scenario.emergency_months} 个月（DRAFT）",),
        )
    return DimensionAssessment(dimension=CoverageDimension.EMERGENCY_RESERVE, status=DimensionStatus.COMPLETE, scenarios=scenarios)


def calculate_coverage_review(
    profile: CustomerProfile,
    parameters: CoverageReviewParameters,
    *,
    as_of: date | None = None,
    profile_version: int | None = None,
) -> CoverageReviewResult:
    """基于同一份不可变客户档案和参数快照计算全部维度。"""

    review_date = as_of or date.today()
    dimensions = {
        CoverageDimension.LIFE: _life(profile, parameters, review_date),
        CoverageDimension.CRITICAL_ILLNESS: _critical_illness(profile, parameters, review_date),
        CoverageDimension.MEDICAL: _medical(profile, parameters, review_date),
        CoverageDimension.ACCIDENT: _accident(profile, parameters, review_date),
        CoverageDimension.RETIREMENT: _retirement(profile, parameters),
        CoverageDimension.EMERGENCY_RESERVE: _emergency(profile, parameters),
    }
    missing_evidence = [path for path, evidence in profile.field_evidence.items() if not evidence.confirmed or evidence.confidence < Decimal("1")]
    confidence_notes = tuple(f"字段 {path} 尚未完全确认" for path in sorted(missing_evidence)) or ("Mock 档案未附逐字段来源；正式接入时必须补充来源与确认状态。",)
    return CoverageReviewResult(
        customer_id=profile.customer_id,
        household_name=profile.household_name,
        as_of_date=review_date,
        profile_version=profile_version,
        parameter_version=parameters.version,
        parameter_status=parameters.status.value,
        dimensions=dimensions,
        confidence_notes=confidence_notes,
        draft_parameter_notes=parameters.notes,
    )
