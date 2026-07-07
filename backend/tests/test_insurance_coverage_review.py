"""保险保障检视 DRAFT 纵向切片的业务契约测试。"""

from __future__ import annotations

from app.insurance.coverage_review import (
    CoverageDimension,
    DimensionStatus,
    build_draft_parameters,
    calculate_coverage_review,
    render_customer_report,
    render_internal_report,
)
from app.insurance.mock_data import build_complete_mock_profile, build_incomplete_mock_profile


def test_complete_profile_produces_three_scenarios_for_all_dimensions() -> None:
    profile = build_complete_mock_profile()
    result = calculate_coverage_review(profile, build_draft_parameters())

    assert set(result.dimensions) == set(CoverageDimension)
    for assessment in result.dimensions.values():
        assert assessment.status is DimensionStatus.COMPLETE
        assert set(assessment.scenarios) == {"conservative", "baseline", "comprehensive"}

    # Mock 家庭被特意设置为寿险和重疾保障不足。以下断言只保护公式方向，
    # 不会把某个 DRAFT 参数值固化为永久业务规则。
    assert result.dimensions[CoverageDimension.LIFE].scenarios["baseline"].gap > 0
    assert result.dimensions[CoverageDimension.CRITICAL_ILLNESS].scenarios["baseline"].gap > 0


def test_missing_customer_facts_are_not_replaced_with_defaults() -> None:
    profile = build_incomplete_mock_profile()
    result = calculate_coverage_review(profile, build_draft_parameters())

    life = result.dimensions[CoverageDimension.LIFE]
    assert life.status is DimensionStatus.UNAVAILABLE
    assert "/financial/liabilities" in life.missing_facts
    assert not life.scenarios


def test_customer_and_internal_reports_use_separate_schemas() -> None:
    result = calculate_coverage_review(build_complete_mock_profile(), build_draft_parameters())

    customer = render_customer_report(result)
    internal = render_internal_report(result)

    assert "演示测算，不可用于真实销售" in customer.markdown
    assert "客户顾虑与心理画像" not in customer.markdown
    assert not hasattr(customer, "confidence_notes")

    assert "内部诊断版" in internal.markdown
    assert "置信度" in internal.markdown
    assert internal.confidence_notes
    assert internal.draft_parameters
