"""保障检视业务展示格式测试。"""

from decimal import Decimal

from app.insurance.coverage_review.display import format_value


def test_medical_responsibility_hides_internal_tier_codes() -> None:
    """医疗责任值应按语义角色展示，不向代理人暴露层级编码。"""

    assert format_value(Decimal("0"), "responsibility_tier", role="existing") == "尚未配置商业医疗保障"
    assert format_value(Decimal("2"), "responsibility_tier", role="target") == "建议覆盖基础及扩展医疗责任"
    assert format_value(Decimal("2"), "responsibility_tier", role="gap") == "基础及扩展医疗责任待补足"


def test_medical_responsibility_generic_value_is_business_readable() -> None:
    """通用医疗责任展示也不得出现内部层级术语。"""

    display = format_value(Decimal("1"), "responsibility_tier")

    assert display == "基础住院医疗责任"
    assert "层责任" not in display
