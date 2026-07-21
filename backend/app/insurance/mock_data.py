"""仅用于测试和本地演示的合成客户档案。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.insurance.intake import CoverageReviewIntake, customer_id_for_name
from app.insurance.models import (
    CustomerProfile,
    EducationPlan,
    FamilyMember,
    FinancialProfile,
    Gender,
    HealthcarePreference,
    IncomeStability,
    InsurancePolicy,
    InvestmentRiskTolerance,
    Liability,
    MedicalResponsibility,
    PolicyCategory,
    PolicyStatus,
    Relationship,
)

REGISTERED_MOCK_CUSTOMER_NAME = "演示甲"


def hydrate_registered_mock_intake(
    intake: CoverageReviewIntake,
) -> CoverageReviewIntake:
    """为已登记的本地 Mock 客户补齐客户中心身份字段。

    Args:
        intake: 从对话或表单解析得到的保障检视入口数据。

    Returns:
        命中 ``演示甲`` 时补齐年龄、性别和职业，否则原样返回。
    """

    if (intake.customer_name or "").strip() != REGISTERED_MOCK_CUSTOMER_NAME:
        return intake
    return intake.model_copy(
        update={
            "age": intake.age if intake.age is not None else 40,
            "gender": intake.gender or Gender.MALE,
            "occupation": intake.occupation or "企业管理",
            "marital_status": intake.marital_status or "married",
        }
    )


def build_registered_mock_profile(customer_name: str | None) -> CustomerProfile | None:
    """按姓名返回可供聊天入口查询的完整合成客户档案。

    Args:
        customer_name: 代理人请求检视的客户姓名。

    Returns:
        ``演示甲`` 对应的完整档案；其他姓名返回 ``None``。
    """

    normalized_name = (customer_name or "").strip()
    if normalized_name != REGISTERED_MOCK_CUSTOMER_NAME:
        return None
    profile = build_complete_mock_profile()
    profile.customer_id = customer_id_for_name(normalized_name)
    return profile


def build_complete_mock_profile() -> CustomerProfile:
    """返回一份信息完整但特意设置为保障不足的合成家庭档案。"""

    return CustomerProfile(
        customer_id="mock-customer-complete",
        household_name="演示家庭",
        marital_status="married",
        education_planning_intent=True,
        members=[
            FamilyMember(
                id="adult-1",
                name="演示甲",
                relationship=Relationship.SELF,
                age=40,
                gender=Gender.MALE,
                occupation="企业管理",
                occupation_risk_class=2,
                city="上海",
                economic_pillar=True,
                income_share=Decimal("0.7"),
                social_insurance_type="城镇职工社保",
                healthcare_preference=HealthcarePreference.PUBLIC,
                desired_retirement_age=60,
                desired_retirement_monthly_spending=Decimal("18000"),
                estimated_social_pension_monthly=Decimal("5000"),
                existing_commercial_retirement_monthly=Decimal("1000"),
            ),
            FamilyMember(
                id="adult-2",
                name="演示乙",
                relationship=Relationship.SPOUSE,
                age=38,
                gender=Gender.FEMALE,
                occupation="教师",
                occupation_risk_class=1,
                city="上海",
                economic_pillar=True,
                income_share=Decimal("0.3"),
                social_insurance_type="事业单位社保",
                healthcare_preference=HealthcarePreference.PRIVATE,
                desired_retirement_age=55,
                desired_retirement_monthly_spending=Decimal("12000"),
                estimated_social_pension_monthly=Decimal("4000"),
                existing_commercial_retirement_monthly=Decimal("0"),
            ),
            FamilyMember(
                id="child-1",
                name="演示子女",
                relationship=Relationship.CHILD,
                age=8,
                gender=Gender.FEMALE,
                occupation="学生",
                occupation_risk_class=1,
                city="上海",
                economic_pillar=False,
                income_share=Decimal("0"),
                social_insurance_type="城乡居民医保",
                healthcare_preference=HealthcarePreference.PUBLIC,
            ),
        ],
        financial=FinancialProfile(
            annual_income=Decimal("600000"),
            primary_annual_income=Decimal("450000"),
            spouse_annual_income=Decimal("150000"),
            monthly_expenses=Decimal("25000"),
            liabilities=[Liability(id="mortgage", kind="房贷", balance=Decimal("1800000"))],
            liquid_assets=Decimal("500000"),
            non_liquid_assets=Decimal("3500000"),
            annual_premium_budget=Decimal("80000"),
            income_stability=IncomeStability.STABLE,
            investment_risk_tolerance=InvestmentRiskTolerance.BALANCED,
        ),
        policies=[
            InsurancePolicy(
                id="life-1",
                insured_member_id="adult-1",
                category=PolicyCategory.LIFE_TERM,
                status=PolicyStatus.ACTIVE,
                sum_assured=Decimal("1000000"),
                effective_date=date(2023, 1, 1),
                expiry_date=date(2043, 1, 1),
            ),
            InsurancePolicy(
                id="ci-1",
                insured_member_id="adult-1",
                category=PolicyCategory.CRITICAL_ILLNESS,
                status=PolicyStatus.ACTIVE,
                sum_assured=Decimal("300000"),
            ),
            InsurancePolicy(
                id="medical-1",
                insured_member_id="adult-1",
                category=PolicyCategory.MEDICAL,
                status=PolicyStatus.ACTIVE,
                medical_responsibilities={MedicalResponsibility.LARGE_HOSPITALIZATION},
            ),
            InsurancePolicy(
                id="accident-1",
                insured_member_id="adult-1",
                category=PolicyCategory.ACCIDENT,
                status=PolicyStatus.ACTIVE,
                sum_assured=Decimal("500000"),
                accident_medical_limit=Decimal("30000"),
            ),
        ],
        education_plans=[EducationPlan(child_member_id="child-1", target_amount=Decimal("800000"))],
        parent_support_plans=[],
    )


def build_incomplete_mock_profile() -> CustomerProfile:
    """返回负债信息未知（而非确认无负债）的客户档案。"""

    profile = build_complete_mock_profile()
    profile.customer_id = "mock-customer-incomplete"
    profile.financial.liabilities = None
    return profile
