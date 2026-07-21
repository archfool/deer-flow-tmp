"""保障检视四类领域 Tool 的协议、Normalizer 与可重复 Mock。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from app.insurance.coverage_review.hashing import sha256_digest, sha256_text
from app.insurance.coverage_review.models import (
    DIMENSION_ORDER,
    AuxiliaryMetric,
    CalculationRequest,
    CalculationResult,
    CalculatorDimension,
    DerivationComponent,
    DimensionCode,
    FrozenModel,
    MeasurementType,
)
from app.insurance.models import (
    CustomerProfile,
    PolicyCategory,
)

RESOURCE_DIR = Path(__file__).resolve().parent / "resources"


class CustomerCenterRecord(FrozenModel):
    """客户中心返回的五类基本客户信息。"""

    customer_id: str
    customer_name: str
    age: int
    gender: str
    life_stage_name: str
    wealth_level_name: str
    source_ref: str
    as_of: datetime


class CustomerProfileSnapshot(FrozenModel):
    """内部客户档案 Tool 的版本化快照。"""

    customer_id: str
    profile_version: int
    profile: dict[str, Any]
    source_ref: str
    as_of: datetime


class RawPolicyReport(FrozenModel):
    """中保信 Tool 返回的原始报告。"""

    report_id: str
    customer_id: str
    content: str
    generated_at: datetime
    source_ref: str
    content_hash: str


class CustomerCenterTool(Protocol):
    """客户中心 Tool 的领域协议。"""

    async def lookup(self, profile: CustomerProfile) -> CustomerCenterRecord:
        """根据客户档案引用查询五类基础信息。

        Args:
            profile: 当前任务绑定的客户档案。

        Returns:
            客户中心规范记录。
        """


class CustomerProfileTool(Protocol):
    """内部客户档案 Tool 的领域协议。"""

    async def lookup(
        self,
        profile: CustomerProfile,
        profile_version: int,
    ) -> CustomerProfileSnapshot:
        """查询并返回版本化客户档案。

        Args:
            profile: 当前客户档案。
            profile_version: 档案乐观锁版本。

        Returns:
            规范客户档案快照。
        """


class ZhongbaoxinReportTool(Protocol):
    """中保信保单检视报告 Tool 的领域协议。"""

    async def fetch(self, profile: CustomerProfile) -> RawPolicyReport:
        """获取客户的原始保单检视报告。

        Args:
            profile: 当前客户档案。

        Returns:
            原始报告及审计元数据。
        """


class PreciseGapCalculatorTool(Protocol):
    """公司精确测算 Tool 的领域协议。"""

    async def calculate(
        self,
        request: CalculationRequest,
        profile: CustomerProfile,
    ) -> CalculationResult:
        """执行八维权威缺口测算。

        Args:
            request: 已通过单位和字段校验的测算请求。
            profile: Mock 计算器构造响应所需的本地档案。

        Returns:
            八维权威测算结果。
        """


@dataclass(frozen=True)
class CoverageReviewTools:
    """保障检视 Workflow 依赖的四类 Tool 集合。"""

    customer_center: CustomerCenterTool
    customer_profile: CustomerProfileTool
    policy_report: ZhongbaoxinReportTool
    precise_calculator: PreciseGapCalculatorTool


class MockCustomerCenterTool:
    """根据合成档案模拟客户中心五字段查询。"""

    async def lookup(self, profile: CustomerProfile) -> CustomerCenterRecord:
        """构造稳定的客户中心查询结果。

        Args:
            profile: 合成或测试客户档案。

        Returns:
            客户中心规范记录。

        Raises:
            ValueError: 档案没有本人成员时抛出。
        """

        member = next(
            (item for item in profile.members if item.relationship.value == "self"),
            None,
        )
        if member is None or member.age is None:
            raise ValueError("customer center mock requires a self member with age")
        life_stage = _life_stage(profile, member.age)
        wealth_level = _wealth_level(profile)
        return CustomerCenterRecord(
            customer_id=profile.customer_id,
            customer_name=member.name,
            age=member.age,
            gender=member.gender.value if member.gender is not None else "undisclosed",
            life_stage_name=life_stage,
            wealth_level_name=wealth_level,
            source_ref=f"mock-customer-center:{profile.customer_id}",
            as_of=datetime.now(UTC),
        )


class MockCustomerProfileTool:
    """模拟项目内部客户档案查询。"""

    async def lookup(
        self,
        profile: CustomerProfile,
        profile_version: int,
    ) -> CustomerProfileSnapshot:
        """返回输入档案的不可变 JSON 快照。

        Args:
            profile: 当前客户档案。
            profile_version: 当前档案版本。

        Returns:
            内部档案 Tool 规范响应。
        """

        return CustomerProfileSnapshot(
            customer_id=profile.customer_id,
            profile_version=profile_version,
            profile=profile.model_dump(mode="json"),
            source_ref=f"mock-customer-profile:{profile.customer_id}:v{profile_version}",
            as_of=datetime.now(UTC),
        )


class MockZhongbaoxinReportTool:
    """按客户档案生成隔离的 Mock 中保信报告。"""

    def __init__(self, report_path: Path | None = None) -> None:
        """初始化 Mock 报告 Tool。

        Args:
            report_path: 自定义报告样例路径；仅用于抽取器夹具测试。
        """

        self._report_path = report_path

    async def fetch(self, profile: CustomerProfile) -> RawPolicyReport:
        """异步读取原始报告并附加哈希。

        Args:
            profile: 当前客户档案。

        Returns:
            中保信原始报告。

        Raises:
            FileNotFoundError: Mock 样例不存在时抛出。
        """

        if self._report_path is None:
            content = _mock_policy_report_for_profile(profile)
            source_name = profile.customer_id
        else:
            content = await asyncio.to_thread(
                self._report_path.read_text,
                encoding="utf-8",
            )
            source_name = self._report_path.name
        return RawPolicyReport(
            report_id=f"mock-zbx-{profile.customer_id}",
            customer_id=profile.customer_id,
            content=content,
            generated_at=datetime.now(UTC),
            source_ref=f"mock-zhongbaoxin:{source_name}",
            content_hash=sha256_text(content),
        )


def _mock_policy_report_for_profile(profile: CustomerProfile) -> str:
    """只使用当前客户已确认保单事实构造中保信 Mock 报告。"""

    policies = profile.policies
    if policies is None:
        return "\n".join(
            (
                "## 保单整体情况概览",
                "有效保单总数：未确认。",
                "客户尚未授权或补充完整保单清单，不提供任何金额推断。",
            )
        )
    active = [item for item in policies if item.status.value == "active"]
    category_labels = {
        PolicyCategory.LIFE_TERM: "定期寿险",
        PolicyCategory.LIFE_WHOLE: "终身寿险",
        PolicyCategory.CRITICAL_ILLNESS: "重疾险",
        PolicyCategory.MEDICAL: "医疗险",
        PolicyCategory.ACCIDENT: "意外险",
        PolicyCategory.ANNUITY: "年金险",
        PolicyCategory.INCREMENTAL_LIFE: "增额终身寿险",
    }
    categories = "、".join(dict.fromkeys(category_labels[item.category] for item in active)) or "无"
    lines = [
        "## 保单整体情况概览",
        f"有效保单总数：{len(active)}件。",
        f"已确认责任类别：{categories}。",
        "未提供的保额、保费和条款信息保持未知，不进行金额推断。",
    ]
    quantified = (
        ("身故总保额", sum((item.sum_assured or Decimal("0")) for item in active if item.category in {PolicyCategory.LIFE_TERM, PolicyCategory.LIFE_WHOLE})),
        ("合计重疾", sum((item.sum_assured or Decimal("0")) for item in active if item.category is PolicyCategory.CRITICAL_ILLNESS)),
        ("未来生存金总额", sum((item.annual_retirement_cashflow or Decimal("0")) for item in active if item.category is PolicyCategory.ANNUITY)),
    )
    for label, value in quantified:
        if value > 0:
            lines.append(f"{label}：{value / Decimal('10000')}万元。")
    medical_limits = [item.accident_medical_limit for item in active if item.category is PolicyCategory.MEDICAL and item.accident_medical_limit is not None]
    if medical_limits:
        lines.append(f"医疗报销：{sum(medical_limits, Decimal('0')) / Decimal('10000')}万元。")
    return "\n".join(lines)


class MockPreciseGapCalculatorTool:
    """用稳定公式模拟公司精确测算接口。

    公式只存在于 Mock Tool 内部，保障检视诊断算法只消费其返回值，
    因而不会形成第二套业务测算。
    """

    async def calculate(
        self,
        request: CalculationRequest,
        profile: CustomerProfile,
    ) -> CalculationResult:
        """生成八维权威 Mock 响应。

        Args:
            request: 标准化精确测算请求。
            profile: 用于模拟已有保障与家庭责任的档案。

        Returns:
            严格按八维顺序返回的测算结果。
        """

        values = _mock_dimension_values(request, profile)
        dimensions = tuple(
            CalculatorDimension(
                dimension_code=code,
                source_code=code.value,
                source_name=_dimension_name(code),
                semantic_version=("disability-v2.1" if code is DimensionCode.DISABILITY else "canonical-v2.1"),
                existing_value=values[code]["existing"],
                ideal_value=values[code]["ideal"],
                gap_value=values[code]["gap"],
                unit=values[code]["unit"],
                is_full=values[code]["gap"] <= 0,
                recommend_first=code in {DimensionCode.DEATH, DimensionCode.DISEASE},
                measurement_type=values[code]["measurement_type"],
                liability_tier=values[code].get("liability_tier"),
                formula_version=values[code]["formula_version"],
                derivation_components=values[code]["derivation_components"],
                calculation_refs=(f"mock-calculator:{request.request_id}:{code.value}",),
            )
            for code in DIMENSION_ORDER
        )
        liquidity_months = _safe_ratio(
            profile.financial.liquid_assets,
            profile.financial.monthly_expenses,
        )
        auxiliary = AuxiliaryMetric(
            metric_code="EMERGENCY_LIQUIDITY_ALERT",
            status=("unavailable" if liquidity_months is None else "alert" if liquidity_months < Decimal("6") else "normal"),
            value=liquidity_months,
            unit="months",
            calculation_refs=(f"mock-calculator:{request.request_id}:liquidity",),
        )
        raw = {
            "request": request.model_dump(mode="json"),
            "dimensions": [item.model_dump(mode="json") for item in dimensions],
            "auxiliary": auxiliary.model_dump(mode="json"),
        }
        raw_hash = sha256_digest(raw)
        return CalculationResult(
            call_id=f"calc-{raw_hash.removeprefix('sha256:')[:16]}",
            tool_name="mock-precise-gap-calculator",
            tool_version="mock-v2.1",
            calculation_type="precise",
            dimensions=dimensions,
            auxiliary_metrics=(auxiliary,),
            raw_response_hash=raw_hash,
        )


def build_mock_tools() -> CoverageReviewTools:
    """构造无外部依赖、可重复的本地 Tool 集。

    Returns:
        四类 Mock Tool 的组合。
    """

    return CoverageReviewTools(
        customer_center=MockCustomerCenterTool(),
        customer_profile=MockCustomerProfileTool(),
        policy_report=MockZhongbaoxinReportTool(),
        precise_calculator=MockPreciseGapCalculatorTool(),
    )


def normalize_calculator_payload(payload: dict[str, Any]) -> tuple[CalculatorDimension, ...]:
    """把公司工具原始 coverList 响应归一化为八维结果。

    Args:
        payload: 公司精确测算接口的原始 JSON。

    Returns:
        按规范顺序排列的八维结果。

    Raises:
        ValueError: 响应缺维、重复、单位或 D3 语义无法安全归一化时抛出。
    """

    cover_list = payload.get("coverList") or payload.get("cardData", {}).get("coverList")
    if not isinstance(cover_list, list) or not cover_list:
        raise ValueError("calculator payload has no coverList")
    snapshot = cover_list[0]
    items: list[dict[str, Any]] = []
    for group in snapshot.get("coverItemList", []):
        group_code = str(group.get("coverTypeGroupCode", ""))
        for item in group.get("coverTypeList", []):
            normalized = dict(item)
            normalized["_group_code"] = group_code
            items.append(normalized)

    by_code: dict[DimensionCode, CalculatorDimension] = {}
    for item in items:
        code = _normalize_dimension_code(
            source_code=str(item.get("coverTypeCode", "")),
            source_name=str(item.get("coverTypeName", "")),
            group_code=str(item.get("_group_code", "")),
        )
        if code in by_code:
            raise ValueError(f"calculator payload repeats dimension {code}")
        semantic_version = str(item.get("semanticVersion") or payload.get("dimensionSemanticVersions", {}).get(str(item.get("coverTypeCode", ""))) or "")
        if code is DimensionCode.DISABILITY and not semantic_version.startswith("disability-"):
            raise ValueError("DIMENSION_SEMANTIC_VERSION_MISMATCH: D3 has no disability semantic proof")
        measurement = MeasurementType.LIABILITY_TIER if code is DimensionCode.MEDICAL else MeasurementType.CASHFLOW_RATIO if code is DimensionCode.RETIREMENT else MeasurementType.AMOUNT_RATIO
        existing = _decimal_or_none(item.get("existingCoverage"))
        ideal = _decimal_or_none(item.get("idealData"))
        gap = _decimal_or_none(item.get("coverageGap"))
        by_code[code] = CalculatorDimension(
            dimension_code=code,
            source_code=str(item.get("coverTypeCode", "")),
            source_name=str(item.get("coverTypeName", "")),
            semantic_version=semantic_version or "unversioned",
            existing_value=existing,
            ideal_value=ideal,
            gap_value=gap,
            unit="tool_unit",
            is_full=bool(item.get("isFull")),
            recommend_first=bool(item.get("recommendFirst", item.get("recommendFifst", 0))),
            measurement_type=measurement,
            calculation_refs=(),
        )
    missing = set(DIMENSION_ORDER) - set(by_code)
    if missing:
        raise ValueError(f"calculator payload misses dimensions: {sorted(code.value for code in missing)}")
    return tuple(by_code[code] for code in DIMENSION_ORDER)


def _life_stage(profile: CustomerProfile, age: int) -> str:
    """根据档案构造 Mock 人生阶段。"""

    has_child = any(member.relationship.value == "child" for member in profile.members)
    if age >= 60:
        return "退休阶段"
    if has_child:
        return "育儿家庭"
    if profile.marital_status == "married":
        return "已婚家庭"
    if profile.marital_status is None:
        return "青年阶段"
    return "单身青年"


def _wealth_level(profile: CustomerProfile) -> str:
    """根据资产规模构造 Mock 财富分层。"""

    financial = profile.financial
    total_assets = (financial.liquid_assets or Decimal("0")) + (financial.non_liquid_assets or Decimal("0"))
    if total_assets >= Decimal("10000000"):
        return "高净值"
    if total_assets >= Decimal("3000000"):
        return "成熟中产"
    return "中产"


def _dimension_name(code: DimensionCode) -> str:
    """返回规范维度中文名。"""

    return {
        DimensionCode.DISEASE: "疾病保障",
        DimensionCode.MEDICAL: "医疗保障",
        DimensionCode.DISABILITY: "伤残保障",
        DimensionCode.LONG_TERM_CARE: "护理保障",
        DimensionCode.DEATH: "身故保障",
        DimensionCode.WEALTH: "财富管理",
        DimensionCode.RETIREMENT: "养老储蓄",
        DimensionCode.LEGACY: "传承储备",
    }[code]


def _safe_ratio(
    numerator: Decimal | None,
    denominator: Decimal | None,
) -> Decimal | None:
    """安全计算 Decimal 比例。"""

    if numerator is None or denominator in {None, Decimal("0")}:
        return None
    return (numerator / denominator).quantize(Decimal("0.01"))


def _mock_dimension_values(
    request: CalculationRequest,
    profile: CustomerProfile,
) -> dict[DimensionCode, dict[str, Any]]:
    """在 Mock Tool 内生成权威八维样例数字。"""

    primary_income_yuan = request.annual_income_wan * Decimal("10000")
    family_annual_expense = request.family_expense_yuan_month * Decimal("12")
    debt_yuan = request.large_loan_wan * Decimal("10000")
    education = sum(
        (plan.target_amount for plan in profile.education_plans or []),
        Decimal("0"),
    )
    self_member = next(
        (member for member in profile.members if member.relationship.value == "self"),
        profile.members[0],
    )
    disease_existing = request.existing_disease_coverage_wan * Decimal("10000")
    medical_existing = request.existing_medical_responsibility_tier
    disability_existing = request.existing_disability_coverage_wan * Decimal("10000")
    care_existing = request.existing_care_coverage_wan * Decimal("10000")
    death_existing = request.existing_death_coverage_wan * Decimal("10000")
    wealth_existing = request.existing_wealth_reserve_wan * Decimal("10000")
    retirement_existing = request.existing_retirement_cashflow_yuan_year
    legacy_existing = request.existing_legacy_reserve_wan * Decimal("10000")

    disease_ideal = primary_income_yuan * Decimal("3") + Decimal("300000")
    medical_ideal = Decimal("2")
    disability_ideal = primary_income_yuan * Decimal("3")
    care_ideal = Decimal("600000") if (self_member.age or 0) >= 50 else Decimal("0")
    death_ideal = debt_yuan + family_annual_expense * Decimal("10") + education
    wealth_ideal = (profile.financial.annual_income or primary_income_yuan) * Decimal("1.5")
    desired_retirement = (self_member.desired_retirement_monthly_spending or request.family_expense_yuan_month) * Decimal("12")
    social_pension = (self_member.estimated_social_pension_monthly or Decimal("0")) * Decimal("12")
    retirement_ideal = max(desired_retirement - social_pension, Decimal("0"))
    legacy_ideal = Decimal("2000000") if _wealth_level(profile) == "高净值" else Decimal("0")

    def amount(
        existing: Decimal,
        ideal: Decimal,
        measurement_type: MeasurementType = MeasurementType.AMOUNT_RATIO,
        unit: str = "CNY",
        *,
        formula_version: str,
        derivation_components: tuple[DerivationComponent, ...],
    ) -> dict[str, Any]:
        """构造单维 Mock 数字。

        Args:
            existing: 已确认现有值。
            ideal: Mock 权威目标值。
            measurement_type: 当前维度的量纲类型。
            unit: 当前维度标准单位。
            formula_version: 可追溯的 Mock 公式版本。
            derivation_components: 用于 5.2 解释的确定性分项。

        Returns:
            单维权威数字和推导分项。
        """

        return {
            "existing": existing,
            "ideal": ideal,
            "gap": max(ideal - existing, Decimal("0")),
            "unit": unit,
            "measurement_type": measurement_type,
            "formula_version": formula_version,
            "derivation_components": derivation_components,
        }

    values = {
        DimensionCode.DISEASE: amount(
            disease_existing,
            disease_ideal,
            formula_version="mock-disease-treatment-income-v2.1",
            derivation_components=(
                DerivationComponent(
                    key="treatment_cost",
                    label="治疗与康复费用基础",
                    value=Decimal("300000"),
                    unit="CNY",
                ),
                DerivationComponent(
                    key="income_interruption",
                    label="康复期收入中断准备",
                    value=primary_income_yuan * Decimal("3"),
                    unit="CNY",
                    note="按三年个人收入形成测算因子",
                ),
            ),
        ),
        DimensionCode.MEDICAL: amount(
            medical_existing,
            medical_ideal,
            MeasurementType.LIABILITY_TIER,
            "responsibility_tier",
            formula_version="mock-medical-liability-tier-v2.1",
            derivation_components=(
                DerivationComponent(
                    key="existing_tier",
                    label="当前商业医疗责任覆盖",
                    value=medical_existing,
                    unit="responsibility_tier",
                ),
                DerivationComponent(
                    key="target_tier",
                    label="建议商业医疗责任范围",
                    value=medical_ideal,
                    unit="responsibility_tier",
                ),
            ),
        ),
        DimensionCode.DISABILITY: amount(
            disability_existing,
            disability_ideal,
            formula_version="mock-disability-income-impact-v2.1",
            derivation_components=(
                DerivationComponent(
                    key="earning_capacity",
                    label="持续工作能力下降对应的收入影响",
                    value=primary_income_yuan * Decimal("3"),
                    unit="CNY",
                    note="康复与照护影响只作后果解释，不另行虚构金额",
                ),
            ),
        ),
        DimensionCode.LONG_TERM_CARE: amount(
            care_existing,
            care_ideal,
            formula_version="mock-care-cost-duration-v2.1",
            derivation_components=(
                DerivationComponent(
                    key="annual_care_cost",
                    label="年度护理成本",
                    value=Decimal("120000") if care_ideal > 0 else Decimal("0"),
                    unit="CNY_PER_YEAR",
                ),
                DerivationComponent(
                    key="care_duration",
                    label="预计护理年限",
                    value=Decimal("5") if care_ideal > 0 else Decimal("0"),
                    unit="years",
                ),
            ),
        ),
        DimensionCode.DEATH: amount(
            death_existing,
            death_ideal,
            formula_version="mock-family-need-hlv-v2.1",
            derivation_components=(
                DerivationComponent(
                    key="family_living_need",
                    label="家庭生活责任",
                    value=family_annual_expense * Decimal("10"),
                    unit="CNY",
                ),
                DerivationComponent(
                    key="outstanding_debt",
                    label="未偿债务责任",
                    value=debt_yuan,
                    unit="CNY",
                ),
                DerivationComponent(
                    key="education_need",
                    label="已确认教育责任",
                    value=education,
                    unit="CNY",
                ),
                DerivationComponent(
                    key="future_income_reference",
                    label="未来收入贡献参考",
                    value=primary_income_yuan * Decimal("10"),
                    unit="CNY",
                    role="upper_anchor",
                    note="仅作生命价值上限参考，不直接作为投保建议",
                ),
            ),
        ),
        DimensionCode.WEALTH: amount(
            wealth_existing,
            wealth_ideal,
            formula_version="mock-wealth-certainty-v2.1",
            derivation_components=(
                DerivationComponent(
                    key="stable_reserve_target",
                    label="长期稳定资金目标",
                    value=wealth_ideal,
                    unit="CNY",
                    note="按个人年收入的 1.5 倍形成测算目标",
                ),
            ),
        ),
        DimensionCode.RETIREMENT: amount(
            retirement_existing,
            retirement_ideal,
            MeasurementType.CASHFLOW_RATIO,
            "CNY_PER_YEAR",
            formula_version="mock-retirement-cashflow-v2.1",
            derivation_components=(
                DerivationComponent(
                    key="desired_retirement_cashflow",
                    label="目标退休年度现金流",
                    value=desired_retirement,
                    unit="CNY_PER_YEAR",
                ),
                DerivationComponent(
                    key="social_pension",
                    label="预计社保养老金",
                    value=social_pension,
                    unit="CNY_PER_YEAR",
                    role="deduction",
                ),
                DerivationComponent(
                    key="commercial_retirement",
                    label="现有商业养老现金流",
                    value=retirement_existing,
                    unit="CNY_PER_YEAR",
                    role="deduction",
                ),
            ),
        ),
        DimensionCode.LEGACY: amount(
            legacy_existing,
            legacy_ideal,
            formula_version="mock-legacy-arrangement-v2.1",
            derivation_components=(
                DerivationComponent(
                    key="legacy_target",
                    label="按客户分线确认的传承储备目标",
                    value=legacy_ideal,
                    unit="CNY",
                ),
            ),
        ),
    }
    values[DimensionCode.MEDICAL]["liability_tier"] = "complete" if medical_existing >= medical_ideal else "basic_only"
    return values


def _normalize_dimension_code(
    *,
    source_code: str,
    source_name: str,
    group_code: str,
) -> DimensionCode:
    """处理公司工具的历史编码与传承 B1 冲突。"""

    if source_name == "传承储备" or group_code == "C":
        return DimensionCode.LEGACY
    try:
        return DimensionCode(source_code)
    except ValueError as exc:
        raise ValueError(f"unknown calculator dimension: {source_code}/{source_name}") from exc


def _decimal_or_none(value: Any) -> Decimal | None:
    """把接口数字安全转换为 Decimal。"""

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f"invalid calculator decimal: {value}") from exc
