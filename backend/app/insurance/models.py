"""保险客户档案领域模型。

当业务事实确实可能未知时，字段允许为空。空集合具有不同语义：用户或权威接口
已确认不存在相应记录。计算器必须保留这一区别。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Gender(StrEnum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    UNDISCLOSED = "undisclosed"


class Relationship(StrEnum):
    SELF = "self"
    SPOUSE = "spouse"
    CHILD = "child"
    PARENT = "parent"
    OTHER = "other"


class IncomeStability(StrEnum):
    STABLE = "stable"
    VARIABLE = "variable"
    SINGLE_INCOME = "single_income"
    BUSINESS_OWNER = "business_owner"


class HealthcarePreference(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    OVERSEAS = "overseas"


class PolicyCategory(StrEnum):
    LIFE_TERM = "life_term"
    LIFE_WHOLE = "life_whole"
    CRITICAL_ILLNESS = "critical_illness"
    MEDICAL = "medical"
    ACCIDENT = "accident"
    ANNUITY = "annuity"
    INCREMENTAL_LIFE = "incremental_life"


class PolicyStatus(StrEnum):
    ACTIVE = "active"
    LAPSED = "lapsed"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class MedicalResponsibility(StrEnum):
    SOCIAL_BASIC = "social_basic"
    LARGE_HOSPITALIZATION = "large_hospitalization"
    SPECIAL_OUTPATIENT = "special_outpatient"
    PRIVATE_HOSPITAL = "private_hospital"
    INTERNATIONAL = "international"
    GENERAL_OUTPATIENT = "general_outpatient"


class FactSource(StrEnum):
    USER_EXPLICIT = "user_explicit"
    AUTHORITATIVE_API = "authoritative_api"
    MODEL_INFERENCE_CONFIRMED = "model_inference_confirmed"


class FieldEvidence(BaseModel):
    """展示在内部诊断报告中的字段级来源信息。"""

    source: FactSource
    source_reference: str | None = None
    confirmed: bool = True
    confidence: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FamilyMember(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    relationship: Relationship
    age: int | None = Field(default=None, ge=0, le=120)
    gender: Gender | None = None
    occupation: str | None = None
    occupation_risk_class: int | None = Field(default=None, ge=1, le=6)
    city: str | None = None
    economic_pillar: bool = False
    income_share: Decimal | None = Field(default=None, ge=0, le=1)
    social_insurance_type: str | None = None
    health_summary: str | None = None
    family_medical_history: str | None = None
    healthcare_preference: HealthcarePreference | None = None
    desired_retirement_age: int | None = Field(default=None, ge=40, le=80)
    desired_retirement_monthly_spending: Decimal | None = Field(default=None, ge=0)
    estimated_social_pension_monthly: Decimal | None = Field(default=None, ge=0)
    existing_commercial_retirement_monthly: Decimal | None = Field(default=None, ge=0)


class Liability(BaseModel):
    id: str
    kind: str
    balance: Decimal = Field(ge=0)


class EducationPlan(BaseModel):
    child_member_id: str
    target_amount: Decimal = Field(ge=0)


class ParentSupportPlan(BaseModel):
    parent_member_id: str
    target_amount: Decimal = Field(ge=0)


class FinancialProfile(BaseModel):
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    annual_income: Decimal | None = Field(default=None, ge=0)
    monthly_expenses: Decimal | None = Field(default=None, ge=0)
    # `None` 表示未知，`[]` 表示已经明确确认没有负债。
    liabilities: list[Liability] | None = None
    liquid_assets: Decimal | None = Field(default=None, ge=0)
    non_liquid_assets: Decimal | None = Field(default=None, ge=0)
    annual_premium_budget: Decimal | None = Field(default=None, ge=0)
    income_stability: IncomeStability | None = None
    income_structure: str | None = None


class InsurancePolicy(BaseModel):
    id: str
    insured_member_id: str
    category: PolicyCategory
    status: PolicyStatus = PolicyStatus.UNKNOWN
    sum_assured: Decimal | None = Field(default=None, ge=0)
    accident_medical_limit: Decimal | None = Field(default=None, ge=0)
    annual_retirement_cashflow: Decimal | None = Field(default=None, ge=0)
    medical_responsibilities: set[MedicalResponsibility] = Field(default_factory=set)
    effective_date: date | None = None
    expiry_date: date | None = None
    annual_premium: Decimal | None = Field(default=None, ge=0)
    payment_end_date: date | None = None
    beneficiary: str | None = None

    def is_effective(self, as_of: date) -> bool:
        """以保守规则判断保单是否应计入测算基准日的现有保障。"""

        if self.status is not PolicyStatus.ACTIVE:
            return False
        if self.effective_date is not None and self.effective_date > as_of:
            return False
        return self.expiry_date is None or self.expiry_date >= as_of


class CustomerProfile(BaseModel):
    """作为单份主体记忆快照存储的权威家庭档案。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    customer_id: str = Field(min_length=1)
    household_name: str = Field(min_length=1)
    marital_status: str | None = None
    education_planning_intent: bool | None = None
    members: list[FamilyMember] = Field(default_factory=list)
    financial: FinancialProfile = Field(default_factory=FinancialProfile)
    # `None` 表示尚未收集现有保障，绝不能强制转换为空列表，否则会制造出
    # 虚假的“完全没有保障”缺口。
    policies: list[InsurancePolicy] | None = None
    education_plans: list[EducationPlan] | None = None
    parent_support_plans: list[ParentSupportPlan] | None = None
    field_evidence: dict[str, FieldEvidence] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> CustomerProfile:
        member_ids = [member.id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("family member ids must be unique")
        known = set(member_ids)
        for policy in self.policies or []:
            if policy.insured_member_id not in known:
                raise ValueError(f"policy {policy.id!r} references unknown member {policy.insured_member_id!r}")
        for plan in self.education_plans or []:
            if plan.child_member_id not in known:
                raise ValueError(f"education plan references unknown member {plan.child_member_id!r}")
        for plan in self.parent_support_plans or []:
            if plan.parent_member_id not in known:
                raise ValueError(f"parent support plan references unknown member {plan.parent_member_id!r}")
        return self

    def member(self, member_id: str) -> FamilyMember:
        try:
            return next(member for member in self.members if member.id == member_id)
        except StopIteration as exc:
            raise KeyError(f"unknown family member: {member_id}") from exc

    def effective_policies(self, as_of: date) -> list[InsurancePolicy]:
        return [policy for policy in self.policies or [] if policy.is_effective(as_of)]
