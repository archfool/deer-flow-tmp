"""保险保障检视任务的公开 API。"""

from app.insurance.coverage_review.calculator import calculate_coverage_review
from app.insurance.coverage_review.models import (
    CoverageDimension,
    CoverageReviewResult,
    CustomerCoverageReport,
    DimensionAssessment,
    DimensionStatus,
    InternalCoverageReport,
    ScenarioAssessment,
)
from app.insurance.coverage_review.parameters import (
    CoverageReviewParameters,
    ParameterStatus,
    ScenarioParameters,
    build_draft_parameters,
)
from app.insurance.coverage_review.reports import render_customer_report, render_internal_report
from app.insurance.coverage_review.workflow import (
    COVERAGE_REVIEW_TASK_NAME,
    COVERAGE_REVIEW_TASK_VERSION,
    build_coverage_review_skill_registry,
    build_coverage_review_task_definition,
)

__all__ = [
    "CoverageDimension",
    "COVERAGE_REVIEW_TASK_NAME",
    "COVERAGE_REVIEW_TASK_VERSION",
    "CoverageReviewParameters",
    "CoverageReviewResult",
    "CustomerCoverageReport",
    "DimensionAssessment",
    "DimensionStatus",
    "InternalCoverageReport",
    "ParameterStatus",
    "ScenarioAssessment",
    "ScenarioParameters",
    "build_draft_parameters",
    "build_coverage_review_skill_registry",
    "build_coverage_review_task_definition",
    "calculate_coverage_review",
    "render_customer_report",
    "render_internal_report",
]
