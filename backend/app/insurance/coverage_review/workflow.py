"""保障检视任务 DAG 和可执行 Skill 注册表。"""

from __future__ import annotations

from collections.abc import Callable

from app.insurance.coverage_review.calculator import calculate_coverage_review
from app.insurance.coverage_review.models import CoverageDimension, DimensionStatus
from app.insurance.coverage_review.parameters import CoverageReviewParameters
from app.insurance.coverage_review.reports import render_customer_report, render_internal_report
from app.insurance.models import CustomerProfile
from deerflow.workflows import (
    InputRequest,
    SideEffectLevel,
    SkillDefinition,
    SkillExecutionContext,
    SkillExecutionResult,
    SkillRegistry,
    TaskDefinition,
    WorkflowStep,
)

COVERAGE_REVIEW_TASK_NAME = "insurance-coverage-review"
COVERAGE_REVIEW_TASK_VERSION = "1"

_DIMENSION_LABELS = {
    CoverageDimension.LIFE: "寿险",
    CoverageDimension.CRITICAL_ILLNESS: "重疾",
    CoverageDimension.MEDICAL: "医疗",
    CoverageDimension.ACCIDENT: "意外",
    CoverageDimension.RETIREMENT: "养老",
    CoverageDimension.EMERGENCY_RESERVE: "应急储备",
}


def _profile(context: SkillExecutionContext) -> CustomerProfile:
    return CustomerProfile.model_validate(context.input_data["profile"])


def _parameters(context: SkillExecutionContext) -> CoverageReviewParameters:
    return CoverageReviewParameters.model_validate(context.input_data["parameters"])


def _collect_general(context: SkillExecutionContext) -> SkillExecutionResult:
    profile = _profile(context)
    return SkillExecutionResult(
        output={
            "household_name": profile.household_name,
            "members": [member.model_dump(mode="json") for member in profile.members],
        }
    )


def _collect_financial(context: SkillExecutionContext) -> SkillExecutionResult:
    return SkillExecutionResult(output=_profile(context).financial.model_dump(mode="json"))


def _collect_policies(context: SkillExecutionContext) -> SkillExecutionResult:
    profile = _profile(context)
    return SkillExecutionResult(
        output={
            "known": profile.policies is not None,
            "policies": [policy.model_dump(mode="json") for policy in profile.policies or []],
        }
    )


def _question_for_path(path: str, label: str) -> str:
    readable = path.rsplit("/", 1)[-1].replace("_", " ")
    return f"为了继续{label}检视，请补充：{readable}（字段 {path}）。"


def _dimension_handler(dimension: CoverageDimension) -> Callable[[SkillExecutionContext], SkillExecutionResult]:
    def handler(context: SkillExecutionContext) -> SkillExecutionResult:
        result = calculate_coverage_review(
            _profile(context),
            _parameters(context),
            profile_version=context.input_data.get("profile_version"),
        )
        assessment = result.dimensions[dimension]
        if assessment.status is DimensionStatus.UNAVAILABLE:
            requests = tuple(
                InputRequest(
                    path=path,
                    prompt=_question_for_path(path, _DIMENSION_LABELS[dimension]),
                    reason=f"{_DIMENSION_LABELS[dimension]}维度缺少不可由参数替代的客户事实",
                    sensitive="health" in path or "medical" in path,
                )
                for path in assessment.missing_facts
            )
            return SkillExecutionResult(
                output={"assessment": assessment.model_dump(mode="json")},
                input_requests=requests,
            )
        return SkillExecutionResult(output={"assessment": assessment.model_dump(mode="json")})

    return handler


def _customer_report(context: SkillExecutionContext) -> SkillExecutionResult:
    result = calculate_coverage_review(
        _profile(context),
        _parameters(context),
        profile_version=context.input_data.get("profile_version"),
    )
    report = render_customer_report(result)
    return SkillExecutionResult(output=report.model_dump(mode="json"))


def _internal_report(context: SkillExecutionContext) -> SkillExecutionResult:
    result = calculate_coverage_review(
        _profile(context),
        _parameters(context),
        profile_version=context.input_data.get("profile_version"),
    )
    report = render_internal_report(result)
    return SkillExecutionResult(output=report.model_dump(mode="json"))


def build_coverage_review_skill_registry() -> SkillRegistry:
    """创建全新注册表，确保 Handler 在不同请求之间不保留状态。"""

    registry = SkillRegistry()
    registry.register(
        SkillDefinition(
            name="insurance-collect-general",
            version="1",
            description="从绑定的档案快照中读取家庭成员与客户通用事实。",
            side_effect=SideEffectLevel.READ,
        ),
        _collect_general,
    )
    registry.register(
        SkillDefinition(
            name="insurance-collect-financial",
            version="1",
            description="读取家庭收入、支出、负债、资产与保费预算事实。",
            side_effect=SideEffectLevel.READ,
        ),
        _collect_financial,
    )
    registry.register(
        SkillDefinition(
            name="insurance-collect-policies",
            version="1",
            description="读取现有保单责任，不把未知保障误判为空。",
            side_effect=SideEffectLevel.READ,
        ),
        _collect_policies,
    )

    for dimension in CoverageDimension:
        registry.register(
            SkillDefinition(
                name=f"insurance-analyze-{dimension.value}",
                version="1",
                description=f"Calculate the {_DIMENSION_LABELS[dimension]} coverage dimension in three DRAFT scenarios.",
                side_effect=SideEffectLevel.NONE,
            ),
            _dimension_handler(dimension),
        )

    registry.register(
        SkillDefinition(
            name="insurance-generate-customer-report",
            version="1",
            description="根据已完成的维度生成客户可见的 Markdown/JSON 报告。",
            side_effect=SideEffectLevel.NONE,
        ),
        _customer_report,
    )
    registry.register(
        SkillDefinition(
            name="insurance-generate-internal-report",
            version="1",
            description="使用独立响应 Schema 生成仅供代理人查看的诊断报告。",
            side_effect=SideEffectLevel.NONE,
        ),
        _internal_report,
    )
    return registry


def build_coverage_review_task_definition() -> TaskDefinition:
    collection_steps = (
        WorkflowStep(id="collect-general", skill="insurance-collect-general"),
        WorkflowStep(id="collect-financial", skill="insurance-collect-financial"),
        WorkflowStep(id="collect-policies", skill="insurance-collect-policies"),
    )
    collection_ids = tuple(step.id for step in collection_steps)
    dimension_steps = tuple(
        WorkflowStep(
            id=f"analyze-{dimension.value}",
            skill=f"insurance-analyze-{dimension.value}",
            depends_on=collection_ids,
        )
        for dimension in CoverageDimension
    )
    dimension_ids = tuple(step.id for step in dimension_steps)
    return TaskDefinition(
        name=COVERAGE_REVIEW_TASK_NAME,
        version=COVERAGE_REVIEW_TASK_VERSION,
        description="使用成员级输入并行分析六个维度的家庭保障检视。",
        steps=(
            *collection_steps,
            *dimension_steps,
            WorkflowStep(
                id="customer-report",
                skill="insurance-generate-customer-report",
                depends_on=dimension_ids,
            ),
            WorkflowStep(
                id="internal-report",
                skill="insurance-generate-internal-report",
                depends_on=dimension_ids,
            ),
        ),
    )
