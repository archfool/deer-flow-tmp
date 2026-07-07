"""首版保障检视使用的版本化 DRAFT 参数。"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.insurance.models import MedicalResponsibility


class ParameterStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


class ScenarioParameters(BaseModel):
    model_config = ConfigDict(frozen=True)

    life_replacement_years: int
    liquid_asset_deduction_ratio: Decimal
    accident_death_credit_ratio: Decimal
    critical_treatment_cost: Decimal
    critical_income_loss_years: int
    accident_income_multiple: Decimal
    accident_medical_target: Decimal
    retirement_replacement_rate: Decimal
    emergency_months: int
    medical_target_responsibilities: frozenset[MedicalResponsibility]


class CoverageReviewParameters(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    status: ParameterStatus
    scenarios: dict[str, ScenarioParameters]
    notes: tuple[str, ...]


def build_draft_parameters() -> CoverageReviewParameters:
    """返回等待业务审核的明确软件默认参数。

    这些值被刻意集中管理并带版本。公式代码不会嵌入所谓“行业数值”，因此后续
    替换只属于数据变更，同时历史任务结果仍然可以复现。
    """

    return CoverageReviewParameters(
        version="draft-2026-07-07",
        status=ParameterStatus.DRAFT,
        scenarios={
            "conservative": ScenarioParameters(
                life_replacement_years=10,
                liquid_asset_deduction_ratio=Decimal("0.50"),
                accident_death_credit_ratio=Decimal("0.50"),
                critical_treatment_cost=Decimal("300000"),
                critical_income_loss_years=3,
                accident_income_multiple=Decimal("5"),
                accident_medical_target=Decimal("30000"),
                retirement_replacement_rate=Decimal("0.60"),
                emergency_months=3,
                medical_target_responsibilities=frozenset({MedicalResponsibility.SOCIAL_BASIC, MedicalResponsibility.LARGE_HOSPITALIZATION}),
            ),
            "baseline": ScenarioParameters(
                life_replacement_years=15,
                liquid_asset_deduction_ratio=Decimal("0.70"),
                accident_death_credit_ratio=Decimal("0.75"),
                critical_treatment_cost=Decimal("400000"),
                critical_income_loss_years=4,
                accident_income_multiple=Decimal("8"),
                accident_medical_target=Decimal("50000"),
                retirement_replacement_rate=Decimal("0.70"),
                emergency_months=6,
                medical_target_responsibilities=frozenset(
                    {
                        MedicalResponsibility.SOCIAL_BASIC,
                        MedicalResponsibility.LARGE_HOSPITALIZATION,
                        MedicalResponsibility.SPECIAL_OUTPATIENT,
                    }
                ),
            ),
            "comprehensive": ScenarioParameters(
                life_replacement_years=20,
                liquid_asset_deduction_ratio=Decimal("1.00"),
                accident_death_credit_ratio=Decimal("1.00"),
                critical_treatment_cost=Decimal("500000"),
                critical_income_loss_years=5,
                accident_income_multiple=Decimal("10"),
                accident_medical_target=Decimal("100000"),
                retirement_replacement_rate=Decimal("0.80"),
                emergency_months=9,
                medical_target_responsibilities=frozenset(
                    {
                        MedicalResponsibility.SOCIAL_BASIC,
                        MedicalResponsibility.LARGE_HOSPITALIZATION,
                        MedicalResponsibility.SPECIAL_OUTPATIENT,
                        MedicalResponsibility.PRIVATE_HOSPITAL,
                        MedicalResponsibility.GENERAL_OUTPATIENT,
                    }
                ),
            ),
        },
        notes=(
            "全部参数为待业务审核的 DRAFT 软件占位值，不代表行业标准。",
            "客户事实不会由这些参数填充；事实缺失时对应维度不可计算并触发追问。",
        ),
    )
