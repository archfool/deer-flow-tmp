"""保障检视自然语言入口与客户档案构造。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from pydantic import BaseModel, Field

from app.insurance.models import (
    CustomerProfile,
    FamilyMember,
    FinancialProfile,
    Gender,
    InsurancePolicy,
    PolicyCategory,
    PolicyStatus,
    Relationship,
)


class CoverageReviewIntake(BaseModel):
    """代理人发起保障检视时提交的显式客户事实。"""

    source_text: str = ""
    thread_id: str | None = None
    customer_name: str | None = None
    age: int | None = Field(default=None, ge=0, le=120)
    gender: Gender | None = None
    occupation: str | None = None
    marital_status: str | None = None
    annual_income_wan: Decimal | None = Field(default=None, ge=0)
    known_policy_categories: tuple[PolicyCategory, ...] = ()
    policies_complete: bool = False
    trigger_ids: tuple[str, ...] = ("D2",)
    answers: dict[str, object] = Field(default_factory=dict)

    @property
    def missing_identity_fields(self) -> tuple[str, ...]:
        """返回客户中心 Mock 查询前必须补齐的身份字段。

        Returns:
            尚未提供的姓名、年龄或性别字段名。
        """

        missing: list[str] = []
        if not self.customer_name:
            missing.append("customer_name")
        if self.age is None:
            missing.append("age")
        if self.gender is None:
            missing.append("gender")
        return tuple(missing)


def parse_coverage_review_intake(
    source_text: str,
    *,
    thread_id: str | None = None,
) -> CoverageReviewIntake:
    """从代理人原话中提取明确陈述的保障检视字段。

    解析器只接收可机械识别的显式事实。无法确认的字段保持为空，交由后续
    结构化表格补录，不使用模型推断客户事实。

    Args:
        source_text: 当前会话中与保障检视有关的代理人原话。
        thread_id: 当前 DeerFlow 会话 ID。

    Returns:
        仅包含显式事实的 Intake 对象。
    """

    text = _normalize_text(source_text)
    customer_name = _extract_customer_name(text)
    age = _extract_int(text, r"(?<!\d)(\d{1,3})\s*岁")
    gender = _extract_gender(text)
    occupation = _extract_occupation(text)
    marital_status = _extract_marital_status(text)
    annual_income_wan = _extract_decimal(
        text,
        r"(?<!配偶)(?<!妻子)(?<!丈夫)(?<!爱人)(?:个人)?年收入\s*(\d+(?:\.\d+)?)\s*万",
    )
    categories, policies_complete = _extract_policies(text)
    return CoverageReviewIntake(
        source_text=source_text,
        thread_id=thread_id,
        customer_name=customer_name,
        age=age,
        gender=gender,
        occupation=occupation,
        marital_status=marital_status,
        annual_income_wan=annual_income_wan,
        known_policy_categories=categories,
        policies_complete=policies_complete,
        trigger_ids=_extract_triggers(text),
        answers=_extract_answers(text),
    )


def merge_intake_answers(
    intake: CoverageReviewIntake,
    source_text: str,
) -> CoverageReviewIntake:
    """把新一轮代理人原话中的精算答案合并到 Intake。

    Args:
        intake: 已有 Intake。
        source_text: 新一轮代理人补充文本。

    Returns:
        合并显式答案后的 Intake 副本。
    """

    parsed = parse_coverage_review_intake(source_text, thread_id=intake.thread_id)
    return intake.model_copy(
        update={
            "source_text": "\n".join(item for item in (intake.source_text, source_text) if item),
            "customer_name": parsed.customer_name or intake.customer_name,
            "age": parsed.age if parsed.age is not None else intake.age,
            "gender": parsed.gender or intake.gender,
            "occupation": parsed.occupation or intake.occupation,
            "marital_status": parsed.marital_status or intake.marital_status,
            "annual_income_wan": (parsed.annual_income_wan if parsed.annual_income_wan is not None else intake.annual_income_wan),
            "known_policy_categories": (parsed.known_policy_categories if parsed.known_policy_categories else intake.known_policy_categories),
            "policies_complete": parsed.policies_complete or intake.policies_complete,
            "trigger_ids": (parsed.trigger_ids if _has_explicit_trigger(source_text) else intake.trigger_ids),
            "answers": {**intake.answers, **parsed.answers},
        }
    )


def build_customer_profile_from_intake(intake: CoverageReviewIntake) -> CustomerProfile:
    """把已满足身份闸门的 Intake 转为客户档案。

    Args:
        intake: 已完成姓名、年龄和性别补录的 Intake。

    Returns:
        未知值保持为空的客户档案。

    Raises:
        ValueError: 身份闸门仍有缺失字段时抛出。
    """

    if intake.missing_identity_fields:
        raise ValueError("coverage review intake is missing identity fields: " + ", ".join(intake.missing_identity_fields))
    assert intake.customer_name is not None
    assert intake.age is not None
    assert intake.gender is not None
    customer_id = customer_id_for_name(intake.customer_name)
    annual_income = intake.annual_income_wan * Decimal("10000") if intake.annual_income_wan is not None else None
    policies = None
    if intake.policies_complete:
        policies = [
            InsurancePolicy(
                id=f"intake-{category.value}-{index}",
                insured_member_id="self",
                category=category,
                status=PolicyStatus.ACTIVE,
            )
            for index, category in enumerate(intake.known_policy_categories, start=1)
        ]
    return CustomerProfile(
        customer_id=customer_id,
        household_name=f"{intake.customer_name}家庭",
        marital_status=intake.marital_status,
        members=[
            FamilyMember(
                id="self",
                name=intake.customer_name,
                relationship=Relationship.SELF,
                age=intake.age,
                gender=intake.gender,
                occupation=intake.occupation,
                economic_pillar=annual_income is not None,
                income_share=Decimal("1") if annual_income is not None else None,
            )
        ],
        financial=FinancialProfile(
            annual_income=annual_income,
            primary_annual_income=annual_income,
        ),
        policies=policies,
    )


def customer_id_for_name(customer_name: str) -> str:
    """生成不暴露客户姓名的稳定 Mock 客户 ID。

    Args:
        customer_name: 已去除首尾空白的客户姓名。

    Returns:
        可安全用于 URL 和数据库主键的客户 ID。
    """

    digest = sha256(customer_name.strip().encode("utf-8")).hexdigest()[:16]
    return f"customer-{digest}"


def _normalize_text(value: str) -> str:
    """移除 DeerFlow 用户输入包装并统一空白。"""

    value = value.replace("--- BEGIN USER INPUT ---", " ")
    value = value.replace("--- END USER INPUT ---", " ")
    return re.sub(r"\s+", " ", value).strip()


def _extract_customer_name(text: str) -> str | None:
    """从常见保障检视表达中提取客户姓名。"""

    patterns = (
        r"(?:帮我)?(?:给|为)\s*([\u4e00-\u9fff]{2,4})\s*(?:做|进行)",
        r"(?:客户(?:姓名)?[：:]?\s*)([\u4e00-\u9fff]{2,4})",
        r"^\s*([\u4e00-\u9fff]{2,4})\s*[，,]\s*\d{1,3}\s*岁",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _extract_gender(text: str) -> Gender | None:
    """提取明确陈述的性别。"""

    if re.search(r"(?:性别[：:]?\s*)?(?:男性|男)(?:[，,。；;\s]|$)", text):
        return Gender.MALE
    if re.search(r"(?:性别[：:]?\s*)?(?:女性|女)(?:[，,。；;\s]|$)", text):
        return Gender.FEMALE
    return None


def _extract_occupation(text: str) -> str | None:
    """提取性别后或职业标签后的职业文本。"""

    patterns = (
        r"职业[：:]?\s*([^，,。；;]{1,30})",
        r"(?:男性|女性|男|女)\s*[，,]\s*([^，,。；;]{1,30})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip()
            if value and not re.search(r"已婚|未婚|收入", value):
                return value
    return None


def _extract_marital_status(text: str) -> str | None:
    """提取明确陈述的婚姻状态。"""

    if "已婚" in text:
        return "married"
    if "未婚" in text or "单身" in text:
        return "single"
    return None


def _extract_policies(text: str) -> tuple[tuple[PolicyCategory, ...], bool]:
    """提取已明确确认的现有保单类别及清单完整性。"""

    if re.search(r"(?:没有|无)\s*(?:任何)?(?:商业)?保险", text):
        return (), True
    mapping = (
        ("医疗", PolicyCategory.MEDICAL),
        ("重疾", PolicyCategory.CRITICAL_ILLNESS),
        ("定期寿", PolicyCategory.LIFE_TERM),
        ("终身寿", PolicyCategory.LIFE_WHOLE),
        ("意外", PolicyCategory.ACCIDENT),
        ("年金", PolicyCategory.ANNUITY),
        ("增额寿", PolicyCategory.INCREMENTAL_LIFE),
    )
    categories = tuple(dict.fromkeys(category for keyword, category in mapping if keyword in text))
    complete = bool(re.search(r"(?:只有|仅有|目前只(?:有|买了))", text))
    return categories, complete


def _extract_triggers(text: str) -> tuple[str, ...]:
    """从明确场景词映射闭集触发编号，通用检视使用年度例检。"""

    mapping = (
        (r"刚结婚|新婚", "A1"),
        (r"头胎|怀孕|预产|刚生", "A2"),
        (r"二胎|多孩", "A3"),
        (r"买房|房贷", "A4"),
        (r"换工作|收入跃迁|涨薪", "A5"),
        (r"创业|企业主", "A6"),
        (r"临近退休|快退休", "A8"),
        (r"体检异常|小病", "B3"),
        (r"主动.*养老|咨询养老", "C1"),
        (r"主动.*重疾|咨询重疾", "C6"),
    )
    result = tuple(code for pattern, code in mapping if re.search(pattern, text))
    return tuple(dict.fromkeys(result)) or ("D2",)


def _has_explicit_trigger(text: str) -> bool:
    """判断补充文本是否明确改变了触发场景。"""

    return _extract_triggers(_normalize_text(text)) != ("D2",) or bool(re.search(r"年度例检|年度检视|例行检视", text))


def _extract_answers(text: str) -> dict[str, object]:
    """提取可直接进入步骤 1.4 的精算补录答案。"""

    answers: dict[str, object] = {}
    spouse_income = _extract_decimal(
        text,
        r"(?:配偶|妻子|丈夫|爱人)(?:个人)?年收入\s*(\d+(?:\.\d+)?)\s*万",
    )
    if spouse_income is not None:
        answers["spouse_annual_income_wan"] = spouse_income
    elif re.search(r"(?:配偶|妻子|丈夫|爱人)(?:没有|无)收入", text):
        answers["spouse_annual_income_wan"] = Decimal("0")

    monthly_expense_wan = _extract_decimal(
        text,
        r"(?:家庭)?(?:每月|月)(?:支出|开支|花费)\s*(\d+(?:\.\d+)?)\s*万",
    )
    if monthly_expense_wan is not None:
        answers["family_expense_yuan_month"] = monthly_expense_wan * Decimal("10000")
    else:
        monthly_expense_yuan = _extract_decimal(
            text,
            r"(?:家庭)?(?:每月|月)(?:支出|开支|花费)\s*(\d+(?:\.\d+)?)\s*(?:元|块)",
        )
        if monthly_expense_yuan is not None:
            answers["family_expense_yuan_month"] = monthly_expense_yuan

    loan = _extract_decimal(
        text,
        r"(?:房贷|贷款|负债)(?:余额)?\s*(\d+(?:\.\d+)?)\s*万",
    )
    if loan is not None:
        answers["large_loan_wan"] = loan
    elif re.search(r"(?:没有|无)(?:房贷|贷款|负债)", text):
        answers["large_loan_wan"] = Decimal("0")

    for value in ("保守型", "稳健型", "激进型"):
        if value in text:
            answers["investment_risk_tolerance"] = value
            break
    return answers


def _extract_decimal(text: str, pattern: str) -> Decimal | None:
    """按正则读取非负十进制数。"""

    match = re.search(pattern, text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except (InvalidOperation, ValueError):
        return None


def _extract_int(text: str, pattern: str) -> int | None:
    """按正则读取整数。"""

    match = re.search(pattern, text)
    return int(match.group(1)) if match else None
