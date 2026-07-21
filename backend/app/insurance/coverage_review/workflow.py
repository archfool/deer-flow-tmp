"""基于 DeerFlow durable workflow 的保障检视企业级 DAG。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.insurance.coverage_review.evidence import (
    build_evidence_bundle,
    build_question_items,
    collect_field_evidence,
    extract_policy_report,
    resolve_trigger_binding,
)
from app.insurance.coverage_review.hashing import sha256_digest
from app.insurance.coverage_review.kernel import (
    build_calculation_request,
    build_diagnosis_kernel,
)
from app.insurance.coverage_review.knowledge import (
    KnowledgeSkillRegistry,
    load_default_knowledge_registry,
    load_knowledge_excerpt,
    select_knowledge_skills,
)
from app.insurance.coverage_review.models import (
    ActionBlock,
    ActionNarrative,
    CalculationResult,
    CustomerAnalysis,
    CustomerReportCopy,
    CustomerReportViewModel,
    DiagnosisKernel,
    DimensionNarrative,
    EvidenceBundle,
    GenerationMode,
    InternalReport,
    MeetingPlan,
    PolicyReportExtraction,
    ReviewPacket,
    TriggerBinding,
    WhyBlock,
)
from app.insurance.coverage_review.narrative import CoverageReviewNarrativeHarness, RuleBoundNarrativeHarness
from app.insurance.coverage_review.projections import (
    project_step_3_1,
    project_step_5_2,
    project_step_5_3,
    validate_projection_consistency,
)
from app.insurance.coverage_review.reports import (
    build_customer_view_model,
    build_review_packet,
    render_customer_report,
    render_internal_report,
)
from app.insurance.coverage_review.rules import (
    RuleBundle,
    load_default_rule_bundle,
)
from app.insurance.coverage_review.tools import (
    CoverageReviewTools,
    CustomerCenterRecord,
    CustomerProfileSnapshot,
    RawPolicyReport,
    build_mock_tools,
)
from app.insurance.models import CustomerProfile
from deerflow.workflows import (
    ConfirmationRequest,
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
COVERAGE_REVIEW_TASK_VERSION = "2.1"
SKILL_VERSION = "2.1"

COVERAGE_REVIEW_LANGUAGE_STEP_IDS = frozenset(
    {
        "verbalize-5-2",
        "verbalize-5-3",
        "customer-analysis",
        "meeting-support",
        "customer-copy",
    }
)

_SKILL_OUTPUT_KEYS = {
    "coverage-review-collect-customer-center": ("customer_center",),
    "coverage-review-collect-customer-profile": ("customer_profile",),
    "coverage-review-fetch-policy-report": ("raw_policy_report",),
    "coverage-review-extract-policy-report": ("policy_report",),
    "coverage-review-resolve-triggers": ("trigger_binding",),
    "coverage-review-evidence-gate": ("questions", "evidence"),
    "coverage-review-calculate": ("calculation_request", "calculation"),
    "coverage-review-freeze-kernel": ("kernel",),
    "coverage-review-project-3-1": ("projection_3_1",),
    "coverage-review-project-5-2": ("why_blocks", "auxiliary_insights"),
    "coverage-review-verbalize-5-2": ("dimension_narratives", "generation_audit"),
    "coverage-review-project-5-3": ("action_block",),
    "coverage-review-verbalize-5-3": ("action_narrative", "generation_audit"),
    "coverage-review-customer-analysis": ("customer_analysis", "generation_audit"),
    "coverage-review-meeting-support": ("meeting_plan", "generation_audit"),
    "coverage-review-consistency-gate": ("validation_results",),
    "coverage-review-language-gate": ("language_validation_results",),
    "coverage-review-internal-report": ("internal_report",),
    "coverage-review-agent-review": ("review_packet",),
    "coverage-review-customer-copy": ("customer_copy", "generation_audit"),
    "coverage-review-customer-view-model": ("view_model",),
    "coverage-review-customer-report": ("customer_report",),
    "coverage-review-finalize": (
        "review_id",
        "revision",
        "kernel_revision",
        "kernel_hash",
        "runtime_context",
        "internal_report",
        "customer_report",
    ),
}


def _profile(context: SkillExecutionContext) -> CustomerProfile:
    """从任务输入读取并校验客户档案。"""

    return CustomerProfile.model_validate(context.input_data["profile"])


def _runtime_context(context: SkillExecutionContext) -> dict[str, Any]:
    """读取并校验任务级签名运行上下文。

    Args:
        context: 当前 Workflow Skill 执行上下文。

    Returns:
        已通过内容哈希校验的运行上下文。

    Raises:
        ValueError: 上下文缺失、格式非法或内容哈希不一致时抛出。
    """

    value = context.input_data.get("runtime_context")
    if not isinstance(value, dict):
        raise ValueError("coverage review runtime context is missing")
    unsigned = dict(value)
    claimed_hash = str(unsigned.pop("context_hash", ""))
    actual_hash = sha256_digest(unsigned)
    if not claimed_hash or claimed_hash != actual_hash:
        raise ValueError("coverage review runtime context hash mismatch")
    return dict(value)


def _generation_audit(
    context: SkillExecutionContext,
    output: Any,
    *,
    knowledge_hashes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """为受约束模型节点生成可重放的输入输出指纹。

    Args:
        context: 当前模型 Skill 的只读上下文。
        output: 已通过 Pydantic 与业务边界校验的模型输出。
        knowledge_hashes: 本节点实际装载的 Knowledge Skill 内容哈希。

    Returns:
        含模型、提示词、输入、输出和知识引用哈希的审计记录。
    """

    runtime = _runtime_context(context)
    input_payload = {
        "step_id": context.step.id,
        "dependencies": context.dependency_outputs,
        "narrative_preferences": context.input_data.get(
            "narrative_preferences",
            {},
        ),
        "knowledge_hashes": knowledge_hashes,
    }
    return {
        "step_id": context.step.id,
        "model_name": runtime["model_name"],
        "provider_model": runtime["provider_model"],
        "harness_version": runtime["narrative_harness_version"],
        "prompt_versions": runtime["prompt_versions"],
        "runtime_context_hash": runtime["context_hash"],
        "input_hash": sha256_digest(input_payload),
        "output_hash": sha256_digest(output),
        "knowledge_hashes": list(knowledge_hashes),
    }


def _dependency(
    context: SkillExecutionContext,
    step_id: str,
    key: str,
) -> Any:
    """读取直接依赖步骤的指定输出。"""

    return context.dependency_outputs[step_id][key]


def _trigger_ids(context: SkillExecutionContext) -> list[str]:
    """读取启动参数或追问回答中的触发编号。"""

    values = context.input_data.get("trigger_ids")
    if not values:
        values = context.input_data.get("answers", {}).get("trigger_ids", [])
    if isinstance(values, str):
        return [item.strip() for item in values.split(",") if item.strip()]
    return list(values or [])


def _require_non_fallback(*assets: Any) -> None:
    """阻止模型静默降级后继续生成正式报告。

    Args:
        assets: 一个或多个带 ``generation_mode`` 的语言资产。

    Raises:
        RuntimeError: 任一正式语言资产使用 fallback 生成时抛出。
    """

    for asset in assets:
        if isinstance(asset, (tuple, list)):
            _require_non_fallback(*asset)
            continue
        if getattr(asset, "generation_mode", None) is GenerationMode.FALLBACK:
            raise RuntimeError("受约束模型文案生成失败，已阻止降级文案进入正式报告")


async def _collect_customer_center(
    context: SkillExecutionContext,
    tools: CoverageReviewTools,
) -> SkillExecutionResult:
    """执行步骤 1.1 客户中心查询。"""

    record = await tools.customer_center.lookup(_profile(context))
    return SkillExecutionResult(output={"customer_center": record.model_dump(mode="json")})


async def _collect_customer_profile(
    context: SkillExecutionContext,
    tools: CoverageReviewTools,
) -> SkillExecutionResult:
    """执行步骤 1.1 内部客户档案查询。"""

    snapshot = await tools.customer_profile.lookup(
        _profile(context),
        int(context.input_data["profile_version"]),
    )
    return SkillExecutionResult(output={"customer_profile": snapshot.model_dump(mode="json")})


async def _fetch_policy_report(
    context: SkillExecutionContext,
    tools: CoverageReviewTools,
) -> SkillExecutionResult:
    """执行步骤 1.2 中保信原始报告查询。"""

    report = await tools.policy_report.fetch(_profile(context))
    return SkillExecutionResult(output={"raw_policy_report": report.model_dump(mode="json")})


def _extract_policy_report(context: SkillExecutionContext) -> SkillExecutionResult:
    """执行步骤 1.2 中保信结构化抽取与冲突识别。"""

    raw = RawPolicyReport.model_validate(_dependency(context, "fetch-policy-report", "raw_policy_report"))
    extraction = extract_policy_report(raw)
    return SkillExecutionResult(output={"policy_report": extraction.model_dump(mode="json")})


def _resolve_triggers(
    context: SkillExecutionContext,
    bundle: RuleBundle,
) -> SkillExecutionResult:
    """执行步骤 1.3 触发场景闭集解析。"""

    trigger_ids = _trigger_ids(context)
    if not trigger_ids:
        field = bundle.field("trigger_ids")
        return SkillExecutionResult(
            output={},
            input_requests=(
                InputRequest(
                    path="/trigger_ids",
                    prompt=field.prompt,
                    reason=field.reason,
                    sensitive=field.sensitive,
                ),
            ),
        )
    binding = resolve_trigger_binding(trigger_ids, bundle)
    return SkillExecutionResult(output={"trigger_binding": binding.model_dump(mode="json")})


def _evidence_gate(
    context: SkillExecutionContext,
    bundle: RuleBundle,
) -> SkillExecutionResult:
    """执行步骤 1.4 证据合并、阻塞追问与完成闸门。"""

    profile = _profile(context)
    center = CustomerCenterRecord.model_validate(_dependency(context, "collect-customer-center", "customer_center"))
    snapshot = CustomerProfileSnapshot.model_validate(_dependency(context, "collect-customer-profile", "customer_profile"))
    policy_report = PolicyReportExtraction.model_validate(_dependency(context, "extract-policy-report", "policy_report"))
    trigger = TriggerBinding.model_validate(_dependency(context, "resolve-triggers", "trigger_binding"))
    answers = dict(context.input_data.get("answers", {}))
    fields, missing, assumptions = collect_field_evidence(
        profile=profile,
        center=center,
        profile_snapshot=snapshot,
        answers=answers,
        bundle=bundle,
    )
    if missing:
        questions = build_question_items(missing, bundle=bundle)
        return SkillExecutionResult(
            output={"questions": [item.model_dump(mode="json") for item in questions]},
            input_requests=tuple(
                InputRequest(
                    path=f"/answers/{item.field_key}",
                    prompt=item.prompt,
                    reason=item.reason,
                    sensitive=item.sensitive,
                )
                for item in questions
            ),
        )
    evidence = build_evidence_bundle(
        profile=profile,
        profile_version=int(context.input_data["profile_version"]),
        center=center,
        fields=fields,
        policy_report=policy_report,
        trigger_binding=trigger,
        assumptions=assumptions,
        bundle=bundle,
    )
    return SkillExecutionResult(output={"evidence": evidence.model_dump(mode="json")})


async def _calculate_gaps(
    context: SkillExecutionContext,
    tools: CoverageReviewTools,
) -> SkillExecutionResult:
    """执行步骤 2 精确测算 Tool 调用。"""

    evidence = EvidenceBundle.model_validate(_dependency(context, "evidence-gate", "evidence"))
    request = build_calculation_request(evidence)
    result = await tools.precise_calculator.calculate(request, _profile(context))
    return SkillExecutionResult(
        output={
            "calculation_request": request.model_dump(mode="json"),
            "calculation": result.model_dump(mode="json"),
        }
    )


def _freeze_kernel(
    context: SkillExecutionContext,
    bundle: RuleBundle,
) -> SkillExecutionResult:
    """执行步骤 3 诊断内核固化。"""

    evidence = EvidenceBundle.model_validate(_dependency(context, "evidence-gate", "evidence"))
    calculation = CalculationResult.model_validate(_dependency(context, "calculate-gaps", "calculation"))
    kernel = build_diagnosis_kernel(
        review_id=context.task.id,
        revision=int(context.input_data.get("coverage_review_revision", 1)),
        evidence=evidence,
        calculation=calculation,
        bundle=bundle,
    )
    return SkillExecutionResult(output={"kernel": kernel.model_dump(mode="json")})


def _project_3_1(context: SkillExecutionContext) -> SkillExecutionResult:
    """输出诊断内核的 3.1 事实投影。"""

    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    return SkillExecutionResult(output={"projection_3_1": project_step_3_1(kernel)})


def _project_5_2(
    context: SkillExecutionContext,
    bundle: RuleBundle,
) -> SkillExecutionResult:
    """执行 5.2 独立解释投影。"""

    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    why_blocks, auxiliary = project_step_5_2(kernel, bundle)
    return SkillExecutionResult(
        output={
            "why_blocks": [item.model_dump(mode="json") for item in why_blocks],
            "auxiliary_insights": [item.model_dump(mode="json") for item in auxiliary],
        }
    )


def _project_5_3(
    context: SkillExecutionContext,
    bundle: RuleBundle,
) -> SkillExecutionResult:
    """执行 5.3 独立行动投影。"""

    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    action = project_step_5_3(kernel, bundle)
    return SkillExecutionResult(output={"action_block": action.model_dump(mode="json")})


async def _verbalize_5_2(
    context: SkillExecutionContext,
    narrative: CoverageReviewNarrativeHarness,
) -> SkillExecutionResult:
    """执行 5.2 受约束解释表达。

    Args:
        context: 当前 Workflow Skill 执行上下文。
        narrative: 只能翻译已选资产的语言 Harness。

    Returns:
        与八维一一对应的代理人可读文案。
    """

    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    why_blocks = tuple(WhyBlock.model_validate(item) for item in _dependency(context, "project-5-2", "why_blocks"))
    preferences = dict(context.input_data.get("narrative_preferences", {}))
    items = await narrative.verbalize_explanations(kernel, why_blocks, preferences)
    _require_non_fallback(items)
    serialized = [item.model_dump(mode="json") for item in items]
    return SkillExecutionResult(
        output={
            "dimension_narratives": serialized,
            "generation_audit": _generation_audit(context, serialized),
        }
    )


async def _verbalize_5_3(
    context: SkillExecutionContext,
    narrative: CoverageReviewNarrativeHarness,
) -> SkillExecutionResult:
    """执行 5.3 受约束行动表达。

    Args:
        context: 当前 Workflow Skill 执行上下文。
        narrative: 只能表达封闭动作集的语言 Harness。

    Returns:
        与已批准动作 ID 绑定的文案。
    """

    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    action = ActionBlock.model_validate(_dependency(context, "project-5-3", "action_block"))
    preferences = dict(context.input_data.get("narrative_preferences", {}))
    result = await narrative.verbalize_actions(kernel, action, preferences)
    _require_non_fallback(result)
    serialized = result.model_dump(mode="json")
    return SkillExecutionResult(
        output={
            "action_narrative": serialized,
            "generation_audit": _generation_audit(context, serialized),
        }
    )


async def _customer_analysis(
    context: SkillExecutionContext,
    narrative: CoverageReviewNarrativeHarness,
) -> SkillExecutionResult:
    """执行步骤 5.1 受约束客户分析。

    Args:
        context: 当前 Workflow Skill 执行上下文。
        narrative: 只读白名单事实的语言 Harness。

    Returns:
        带事实引用的客户分析。
    """

    evidence = EvidenceBundle.model_validate(_dependency(context, "evidence-gate", "evidence"))
    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    analysis = await narrative.analyze_customer(evidence, kernel, dict(context.input_data.get("narrative_preferences", {})))
    _require_non_fallback(analysis)
    serialized = analysis.model_dump(mode="json")
    return SkillExecutionResult(
        output={
            "customer_analysis": serialized,
            "generation_audit": _generation_audit(context, serialized),
        }
    )


async def _meeting_support(
    context: SkillExecutionContext,
    knowledge_registry: KnowledgeSkillRegistry,
    narrative: CoverageReviewNarrativeHarness,
    knowledge_skills_root: Path | None,
) -> SkillExecutionResult:
    """执行步骤 5.4 至 5.6 面谈支持。

    Args:
        context: 当前 Workflow Skill 执行上下文。
        knowledge_registry: 书籍 Knowledge Skill 权限和路由注册表。
        narrative: 会谈计划语言 Harness。
        knowledge_skills_root: 部署层注入的书籍 Skill 根目录。

    Returns:
        个性化会谈计划、话术和异议处理。
    """

    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    analysis = CustomerAnalysis.model_validate(_dependency(context, "customer-analysis", "customer_analysis"))
    dimensions = tuple(DimensionNarrative.model_validate(item) for item in _dependency(context, "verbalize-5-2", "dimension_narratives"))
    action_narrative = ActionNarrative.model_validate(_dependency(context, "verbalize-5-3", "action_narrative"))
    topics = {
        "family_risk",
        "consultative",
        "meeting",
        "objection",
        "communication",
        "contract_boundary",
    }
    if kernel.customer_route.route.value == "high_net_worth":
        topics.update({"hnw", "structure"})
    if any(code.value == "C1" for code in kernel.trigger_binding.focus_dimensions):
        topics.update({"legacy", "family_boundary"})
    selected = select_knowledge_skills(
        knowledge_registry,
        node="meeting-support",
        topics=topics,
    )
    excerpts = ()
    if knowledge_skills_root is not None:
        excerpts = tuple(
            await asyncio.gather(
                *(
                    asyncio.to_thread(
                        load_knowledge_excerpt,
                        knowledge_registry,
                        skill_id=item.skill_id,
                        skills_root=knowledge_skills_root,
                        max_chars=4000,
                    )
                    for item in selected
                )
            )
        )
    meeting = await narrative.plan_meeting(
        kernel,
        analysis,
        dimensions,
        action_narrative,
        excerpts,
        dict(context.input_data.get("narrative_preferences", {})),
    )
    _require_non_fallback(meeting)
    serialized = meeting.model_dump(mode="json")
    return SkillExecutionResult(
        output={
            "meeting_plan": serialized,
            "generation_audit": _generation_audit(
                context,
                serialized,
                knowledge_hashes=tuple(item.source_hash for item in excerpts),
            ),
        }
    )


def _consistency_gate(
    context: SkillExecutionContext,
    bundle: RuleBundle,
) -> SkillExecutionResult:
    """执行 3.1、5.2、5.3 的机械一致性闸门。"""

    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    why_blocks = tuple(WhyBlock.model_validate(item) for item in _dependency(context, "project-5-2", "why_blocks"))
    auxiliary = tuple(_auxiliary_insight(item) for item in _dependency(context, "project-5-2", "auxiliary_insights"))
    action = ActionBlock.model_validate(_dependency(context, "project-5-3", "action_block"))
    results = validate_projection_consistency(
        kernel=kernel,
        why_blocks=why_blocks,
        auxiliary_insights=auxiliary,
        action_block=action,
        bundle=bundle,
    )
    return SkillExecutionResult(output={"validation_results": list(results)})


def _language_gate(
    context: SkillExecutionContext,
    bundle: RuleBundle,
) -> SkillExecutionResult:
    """校验语言节点未改变固定结论或泄漏开发字段。

    Args:
        context: 当前 Workflow Skill 执行上下文。
        bundle: 包含理由码、框架码和边界词的签名规则包。

    Returns:
        通过的语言一致性校验项。

    Raises:
        ValueError: 语言输出缺维、丢动作、伪造引用或泄漏开发字段时抛出。
    """

    evidence = EvidenceBundle.model_validate(_dependency(context, "evidence-gate", "evidence"))
    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    why_blocks = tuple(WhyBlock.model_validate(item) for item in _dependency(context, "project-5-2", "why_blocks"))
    action_block = ActionBlock.model_validate(_dependency(context, "project-5-3", "action_block"))
    analysis = CustomerAnalysis.model_validate(_dependency(context, "customer-analysis", "customer_analysis"))
    dimensions = tuple(DimensionNarrative.model_validate(item) for item in _dependency(context, "verbalize-5-2", "dimension_narratives"))
    action_narrative = ActionNarrative.model_validate(_dependency(context, "verbalize-5-3", "action_narrative"))
    meeting = MeetingPlan.model_validate(_dependency(context, "meeting-support", "meeting_plan"))

    if tuple(item.dimension_code for item in dimensions) != tuple(item.dimension_code for item in kernel.dimension_facts):
        raise ValueError("language dimensions drift from diagnosis kernel")
    expected_actions = {item.action_id for item in (*action_block.actions, *action_block.maintain_items)}
    if {item.action_id for item in action_narrative.items} != expected_actions:
        raise ValueError("language actions drift from closed action block")
    _require_non_fallback(
        analysis,
        dimensions,
        action_narrative,
        meeting,
    )
    refs_by_code = {item.dimension_code: set(item.fact_refs) for item in why_blocks}
    for item in dimensions:
        if not set(item.fact_refs).issubset(refs_by_code[item.dimension_code]):
            raise ValueError(f"dimension narrative invented fact refs: {item.dimension_code.value}")
    allowed_analysis_refs = {
        *(item.source_ref for item in evidence.fields if item.source_ref),
        *(item.fact_id for item in evidence.policy_report.policy_facts if item.fact_id),
    }
    if not set(analysis.fact_refs).issubset(allowed_analysis_refs):
        raise ValueError("customer analysis invented fact refs")
    allowed_refs = set().union(*refs_by_code.values(), set(analysis.fact_refs))
    if any(not set(item.fact_refs).issubset(allowed_refs) for item in meeting.scripts):
        raise ValueError("meeting script invented fact refs")

    public_language = json.dumps(
        {
            "analysis": analysis.model_dump(mode="json"),
            "dimensions": [item.model_dump(mode="json") for item in dimensions],
            "actions": action_narrative.model_dump(mode="json"),
            "meeting": meeting.model_dump(mode="json", exclude={"knowledge_refs"}),
        },
        ensure_ascii=False,
    )
    internal_tokens = {
        "reason_code",
        "framework_code",
        "kernel_hash",
        "rule_bundle",
        *(item.code for item in bundle.reasons),
        *(item.framework_code for item in bundle.reasons),
    }
    leaked = next((item for item in internal_tokens if item and item in public_language), None)
    if leaked is not None:
        raise ValueError(f"language output leaked internal token: {leaked}")
    return SkillExecutionResult(
        output={
            "language_validation_results": [
                "PASS: eight language dimensions remain kernel-bound",
                "PASS: every approved action has exactly one language projection",
                "PASS: fact refs stay inside selected assets",
                "PASS: internal tokens are isolated from agent-facing copy",
            ]
        }
    )


def _auxiliary_insight(value: dict[str, Any]) -> Any:
    """延迟导入并校验辅助说明，避免宽泛 Any 传播。"""

    from app.insurance.coverage_review.models import AuxiliaryInsight

    return AuxiliaryInsight.model_validate(value)


def _internal_report(context: SkillExecutionContext) -> SkillExecutionResult:
    """执行步骤 5 对内报告汇总。"""

    evidence = EvidenceBundle.model_validate(_dependency(context, "evidence-gate", "evidence"))
    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    why_blocks = tuple(WhyBlock.model_validate(item) for item in _dependency(context, "project-5-2", "why_blocks"))
    dimension_narratives = tuple(DimensionNarrative.model_validate(item) for item in _dependency(context, "verbalize-5-2", "dimension_narratives"))
    auxiliary = tuple(_auxiliary_insight(item) for item in _dependency(context, "project-5-2", "auxiliary_insights"))
    action = ActionBlock.model_validate(_dependency(context, "project-5-3", "action_block"))
    action_narrative = ActionNarrative.model_validate(_dependency(context, "verbalize-5-3", "action_narrative"))
    analysis = CustomerAnalysis.model_validate(_dependency(context, "customer-analysis", "customer_analysis"))
    meeting = MeetingPlan.model_validate(_dependency(context, "meeting-support", "meeting_plan"))
    results = tuple(
        [
            *_dependency(context, "consistency-gate", "validation_results"),
            *_dependency(context, "language-gate", "language_validation_results"),
        ]
    )
    report = render_internal_report(
        evidence=evidence,
        kernel=kernel,
        why_blocks=why_blocks,
        dimension_narratives=dimension_narratives,
        auxiliary_insights=auxiliary,
        action_block=action,
        action_narrative=action_narrative,
        validation_results=results,
        review_revision=int(context.input_data.get("review_revision", 1)),
        customer_analysis=analysis,
        meeting_plan=meeting,
    )
    report = report.model_copy(
        update={
            "audit_manifest": {
                **report.audit_manifest,
                "runtime_context": _runtime_context(context),
                "generation_audits": {
                    step_id: _dependency(
                        context,
                        step_id,
                        "generation_audit",
                    )
                    for step_id in (
                        "customer-analysis",
                        "verbalize-5-2",
                        "verbalize-5-3",
                        "meeting-support",
                    )
                },
            }
        }
    )
    return SkillExecutionResult(output={"internal_report": report.model_dump(mode="json")})


def _agent_review(context: SkillExecutionContext) -> SkillExecutionResult:
    """执行步骤 4 代理人复核中断。"""

    evidence = EvidenceBundle.model_validate(_dependency(context, "evidence-gate", "evidence"))
    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    report = InternalReport.model_validate(_dependency(context, "internal-report", "internal_report"))
    auxiliary = tuple(_auxiliary_insight(item) for item in _dependency(context, "project-5-2", "auxiliary_insights"))
    packet = build_review_packet(
        evidence,
        kernel,
        report,
        auxiliary,
        review_revision=int(context.input_data.get("review_revision", 1)),
    )
    return SkillExecutionResult(
        output={"review_packet": packet.model_dump(mode="json")},
        confirmation_request=ConfirmationRequest(
            prompt="请复核客户事实、八维数字、保留项、纠错项、优先级和对内报告，并确认是否生成对客报告。",
            risk="确认后将基于当前 kernel hash 生成客户可见 HTML；上游事实变化会使本次确认失效。",
        ),
    )


async def _customer_copy(
    context: SkillExecutionContext,
    knowledge_registry: KnowledgeSkillRegistry,
    narrative: CoverageReviewNarrativeHarness,
    knowledge_skills_root: Path | None,
) -> SkillExecutionResult:
    """在代理人批准后生成对客 ViewModel 受控文案。

    Args:
        context: 当前 Workflow Skill 执行上下文。
        knowledge_registry: 书籍 Knowledge Skill 权限和路由注册表。
        narrative: 对客文案语言 Harness。
        knowledge_skills_root: 部署层注入的书籍 Skill 根目录。

    Returns:
        不包含 HTML 和锁定数字的对客文案字段。
    """

    evidence = EvidenceBundle.model_validate(_dependency(context, "evidence-gate", "evidence"))
    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    why_blocks = tuple(WhyBlock.model_validate(item) for item in _dependency(context, "project-5-2", "why_blocks"))
    dimensions = tuple(
        DimensionNarrative.model_validate(item)
        for item in _dependency(
            context,
            "verbalize-5-2",
            "dimension_narratives",
        )
    )
    action_narrative = ActionNarrative.model_validate(_dependency(context, "verbalize-5-3", "action_narrative"))
    selected = select_knowledge_skills(
        knowledge_registry,
        node="customer-copy",
        topics={
            "family_risk",
            "consultative",
            "communication",
            "contract_boundary",
        },
        max_skills=3,
    )
    excerpts = ()
    if knowledge_skills_root is not None:
        excerpts = tuple(
            await asyncio.gather(
                *(
                    asyncio.to_thread(
                        load_knowledge_excerpt,
                        knowledge_registry,
                        skill_id=item.skill_id,
                        skills_root=knowledge_skills_root,
                        max_chars=3000,
                    )
                    for item in selected
                )
            )
        )
    copy = await narrative.write_customer_copy(
        evidence,
        kernel,
        why_blocks,
        dimensions,
        action_narrative,
        excerpts,
        dict(context.input_data.get("narrative_preferences", {})),
    )
    _require_non_fallback(copy)
    serialized = copy.model_dump(mode="json")
    return SkillExecutionResult(
        output={
            "customer_copy": serialized,
            "generation_audit": _generation_audit(
                context,
                serialized,
                knowledge_hashes=tuple(item.source_hash for item in excerpts),
            ),
        }
    )


def _customer_view_model(context: SkillExecutionContext) -> SkillExecutionResult:
    """执行步骤 6.1 至 6.3 对客 ViewModel 构建。"""

    profile = _profile(context)
    evidence = EvidenceBundle.model_validate(_dependency(context, "evidence-gate", "evidence"))
    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    why_blocks = tuple(WhyBlock.model_validate(item) for item in _dependency(context, "project-5-2", "why_blocks"))
    dimension_narratives = tuple(
        DimensionNarrative.model_validate(item)
        for item in _dependency(
            context,
            "verbalize-5-2",
            "dimension_narratives",
        )
    )
    auxiliary = tuple(
        _auxiliary_insight(item)
        for item in _dependency(
            context,
            "project-5-2",
            "auxiliary_insights",
        )
    )
    action = ActionBlock.model_validate(_dependency(context, "project-5-3", "action_block"))
    action_narrative = ActionNarrative.model_validate(_dependency(context, "verbalize-5-3", "action_narrative"))
    customer_copy = CustomerReportCopy.model_validate(_dependency(context, "customer-copy", "customer_copy"))
    packet = ReviewPacket.model_validate(_dependency(context, "agent-review", "review_packet"))
    view_model = build_customer_view_model(
        profile=profile,
        evidence=evidence,
        kernel=kernel,
        why_blocks=why_blocks,
        dimension_narratives=dimension_narratives,
        auxiliary_insights=auxiliary,
        action_block=action,
        action_narrative=action_narrative,
        customer_copy=customer_copy,
        approved_kernel_hash=packet.kernel_hash,
        review_revision=int(context.input_data.get("review_revision", 1)),
    )
    return SkillExecutionResult(output={"view_model": view_model.model_dump(mode="json")})


async def _customer_report(
    context: SkillExecutionContext,
    bundle: RuleBundle,
) -> SkillExecutionResult:
    """执行步骤 6.4 至 6.6 HTML 渲染与最终校验。"""

    view_model = CustomerReportViewModel.model_validate(_dependency(context, "customer-view-model", "view_model"))
    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    action = ActionBlock.model_validate(_dependency(context, "project-5-3", "action_block"))
    artifact = await render_customer_report(
        view_model=view_model,
        kernel=kernel,
        action_block=action,
        bundle=bundle,
    )
    approval_record = dict(context.input_data.get("approval_record", {}))
    artifact = artifact.model_copy(
        update={
            "manifest": {
                **artifact.manifest,
                "runtime_context": _runtime_context(context),
                "generation_audits": {
                    "customer-copy": _dependency(
                        context,
                        "customer-copy",
                        "generation_audit",
                    ),
                },
                "knowledge_refs": list(
                    dict.fromkeys(
                        [
                            *artifact.manifest.get("knowledge_refs", []),
                            *approval_record.get("knowledge_refs", []),
                        ]
                    )
                ),
                "approval_record": approval_record,
            }
        }
    )
    return SkillExecutionResult(output={"customer_report": artifact.model_dump(mode="json")})


def _finalize(context: SkillExecutionContext) -> SkillExecutionResult:
    """汇总内部、外部产物及核心审计引用。"""

    kernel = DiagnosisKernel.model_validate(_dependency(context, "freeze-kernel", "kernel"))
    internal = InternalReport.model_validate(_dependency(context, "internal-report", "internal_report"))
    customer = _dependency(context, "customer-report", "customer_report")
    return SkillExecutionResult(
        output={
            "review_id": kernel.review_id,
            "revision": int(context.input_data.get("review_revision", 1)),
            "kernel_revision": kernel.revision,
            "kernel_hash": kernel.kernel_hash,
            "runtime_context": _runtime_context(context),
            "internal_report": internal.model_dump(mode="json"),
            "customer_report": customer,
        }
    )


def _register(
    registry: SkillRegistry,
    *,
    name: str,
    description: str,
    handler: Callable[[SkillExecutionContext], Any],
    side_effect: SideEffectLevel = SideEffectLevel.NONE,
    allowed_tools: tuple[str, ...] = (),
) -> None:
    """注册一个带 Schema 和版本的 Executable Skill。

    Args:
        registry: DeerFlow Skill 注册表。
        name: 版本化 Skill 名称。
        description: 节点业务责任。
        handler: 执行 SkillExecutionContext 的处理器。
        side_effect: 机器可读的副作用级别。
        allowed_tools: 该 Skill 允许调用的 Tool 白名单。

    Raises:
        ValueError: Skill 未登记输出契约时抛出。
    """

    output_keys = _SKILL_OUTPUT_KEYS.get(name)
    if output_keys is None:
        raise ValueError(f"skill output schema is not registered: {name}")

    registry.register(
        SkillDefinition(
            name=name,
            version=SKILL_VERSION,
            description=description,
            input_schema={
                "type": "object",
                "description": "Task input and dependency outputs; handlers validate domain Pydantic contracts.",
                "additionalProperties": True,
            },
            output_schema={
                "type": "object",
                "properties": {key: {} for key in output_keys},
                "additionalProperties": False,
            },
            allowed_tools=allowed_tools,
            side_effect=side_effect,
            timeout_seconds=(
                110
                if name
                in {
                    "coverage-review-verbalize-5-2",
                    "coverage-review-verbalize-5-3",
                    "coverage-review-customer-analysis",
                    "coverage-review-meeting-support",
                    "coverage-review-customer-copy",
                }
                else 75
            ),
            max_attempts=(
                3
                if name
                in {
                    "coverage-review-verbalize-5-2",
                    "coverage-review-verbalize-5-3",
                    "coverage-review-customer-analysis",
                    "coverage-review-meeting-support",
                    "coverage-review-customer-copy",
                }
                else 2
            ),
        ),
        handler,
    )


def build_coverage_review_skill_registry(
    *,
    tools: CoverageReviewTools | None = None,
    bundle: RuleBundle | None = None,
    knowledge_registry: KnowledgeSkillRegistry | None = None,
    narrative_harness: CoverageReviewNarrativeHarness | None = None,
    knowledge_skills_root: Path | None = None,
) -> SkillRegistry:
    """创建无跨请求状态的保障检视 Skill 注册表。

    Args:
        tools: 四类领域 Tool；为空时使用可重复 Mock。
        bundle: 签名规则包；为空时加载默认包。
        knowledge_registry: 书籍 Knowledge Skill 权限和路由注册表。
        narrative_harness: 受约束语言节点；为空时使用离线规则降级实现。
        knowledge_skills_root: 部署层注入的书籍 Skill 根目录。

    Returns:
        已绑定全部 Workflow Handler 的 SkillRegistry。
    """

    runtime_tools = tools or build_mock_tools()
    runtime_bundle = bundle or load_default_rule_bundle()
    runtime_knowledge = knowledge_registry or load_default_knowledge_registry()
    runtime_narrative = narrative_harness or RuleBoundNarrativeHarness()
    registry = SkillRegistry()
    _register(
        registry,
        name="coverage-review-collect-customer-center",
        description="步骤1.1：查询客户中心五类基本信息。",
        handler=lambda context: _collect_customer_center(context, runtime_tools),
        side_effect=SideEffectLevel.READ,
        allowed_tools=("customer_center_lookup",),
    )
    _register(
        registry,
        name="coverage-review-collect-customer-profile",
        description="步骤1.1：查询内部客户档案快照。",
        handler=lambda context: _collect_customer_profile(context, runtime_tools),
        side_effect=SideEffectLevel.READ,
        allowed_tools=("customer_profile_lookup",),
    )
    _register(
        registry,
        name="coverage-review-fetch-policy-report",
        description="步骤1.2：获取中保信原始保单检视报告。",
        handler=lambda context: _fetch_policy_report(context, runtime_tools),
        side_effect=SideEffectLevel.READ,
        allowed_tools=("zhongbaoxin_report_fetch",),
    )
    _register(
        registry,
        name="coverage-review-extract-policy-report",
        description="步骤1.2：结构化抽取并识别报告冲突。",
        handler=_extract_policy_report,
    )
    _register(
        registry,
        name="coverage-review-resolve-triggers",
        description="步骤1.3：合成主触发、重心维度、温度与护栏。",
        handler=lambda context: _resolve_triggers(context, runtime_bundle),
    )
    _register(
        registry,
        name="coverage-review-evidence-gate",
        description="步骤1.4：合并证据并生成最多一轮阻塞追问。",
        handler=lambda context: _evidence_gate(context, runtime_bundle),
    )
    _register(
        registry,
        name="coverage-review-calculate",
        description="步骤2：调用唯一权威精确测算 Tool。",
        handler=lambda context: _calculate_gaps(context, runtime_tools),
        side_effect=SideEffectLevel.READ,
        allowed_tools=("precise_gap_calculator",),
    )
    _register(
        registry,
        name="coverage-review-freeze-kernel",
        description="步骤3：确定性固化八维诊断内核。",
        handler=lambda context: _freeze_kernel(context, runtime_bundle),
    )
    _register(
        registry,
        name="coverage-review-project-3-1",
        description="步骤3.1：生成纯事实投影。",
        handler=_project_3_1,
    )
    _register(
        registry,
        name="coverage-review-project-5-2",
        description="步骤5.2：从同一内核生成解释资产。",
        handler=lambda context: _project_5_2(context, runtime_bundle),
    )
    _register(
        registry,
        name="coverage-review-verbalize-5-2",
        description="步骤5.2b：用 Harness 表达已选中的解释资产。",
        handler=lambda context: _verbalize_5_2(context, runtime_narrative),
    )
    _register(
        registry,
        name="coverage-review-project-5-3",
        description="步骤5.3：从同一内核生成封闭行动资产。",
        handler=lambda context: _project_5_3(context, runtime_bundle),
    )
    _register(
        registry,
        name="coverage-review-verbalize-5-3",
        description="步骤5.3b：用 Harness 表达已选中的行动资产。",
        handler=lambda context: _verbalize_5_3(context, runtime_narrative),
    )
    _register(
        registry,
        name="coverage-review-customer-analysis",
        description="步骤5.1：生成带引用的客户分析。",
        handler=lambda context: _customer_analysis(context, runtime_narrative),
    )
    _register(
        registry,
        name="coverage-review-meeting-support",
        description="步骤5.4-5.6：生成会谈计划、话术锚点与异议处理。",
        handler=lambda context: _meeting_support(context, runtime_knowledge, runtime_narrative, knowledge_skills_root),
    )
    _register(
        registry,
        name="coverage-review-consistency-gate",
        description="校验3.1、5.2、5.3和辅助域的一致性。",
        handler=lambda context: _consistency_gate(context, runtime_bundle),
    )
    _register(
        registry,
        name="coverage-review-language-gate",
        description="校验语言节点的引用、封闭动作和内部字段隔离。",
        handler=lambda context: _language_gate(context, runtime_bundle),
    )
    _register(
        registry,
        name="coverage-review-internal-report",
        description="步骤5：组装对内报告，不重新判断。",
        handler=_internal_report,
    )
    _register(
        registry,
        name="coverage-review-agent-review",
        description="步骤4：形成复核包并等待代理人确认。",
        handler=_agent_review,
    )
    _register(
        registry,
        name="coverage-review-customer-copy",
        description="步骤6.2：仅基于获批内核生成 ViewModel 文案字段。",
        handler=lambda context: _customer_copy(
            context,
            runtime_knowledge,
            runtime_narrative,
            knowledge_skills_root,
        ),
    )
    _register(
        registry,
        name="coverage-review-customer-view-model",
        description="步骤6：基于批准内核构造对客ViewModel。",
        handler=_customer_view_model,
    )
    _register(
        registry,
        name="coverage-review-customer-report",
        description="步骤6：固定模板渲染和校验对客HTML。",
        handler=lambda context: _customer_report(context, runtime_bundle),
    )
    _register(
        registry,
        name="coverage-review-finalize",
        description="汇总最终产物与审计引用。",
        handler=_finalize,
    )
    return registry


def build_coverage_review_task_definition() -> TaskDefinition:
    """构造真实顺序为 1→2→3→5→4→6 的保障检视 DAG。

    Returns:
        DeerFlow 可持久化任务定义。
    """

    steps = (
        WorkflowStep(
            id="collect-customer-center",
            skill="coverage-review-collect-customer-center",
            skill_version=SKILL_VERSION,
        ),
        WorkflowStep(
            id="collect-customer-profile",
            skill="coverage-review-collect-customer-profile",
            skill_version=SKILL_VERSION,
        ),
        WorkflowStep(
            id="fetch-policy-report",
            skill="coverage-review-fetch-policy-report",
            skill_version=SKILL_VERSION,
        ),
        WorkflowStep(
            id="extract-policy-report",
            skill="coverage-review-extract-policy-report",
            skill_version=SKILL_VERSION,
            depends_on=("fetch-policy-report",),
        ),
        WorkflowStep(
            id="resolve-triggers",
            skill="coverage-review-resolve-triggers",
            skill_version=SKILL_VERSION,
        ),
        WorkflowStep(
            id="evidence-gate",
            skill="coverage-review-evidence-gate",
            skill_version=SKILL_VERSION,
            depends_on=(
                "collect-customer-center",
                "collect-customer-profile",
                "extract-policy-report",
                "resolve-triggers",
            ),
        ),
        WorkflowStep(
            id="calculate-gaps",
            skill="coverage-review-calculate",
            skill_version=SKILL_VERSION,
            depends_on=("evidence-gate",),
        ),
        WorkflowStep(
            id="freeze-kernel",
            skill="coverage-review-freeze-kernel",
            skill_version=SKILL_VERSION,
            depends_on=("evidence-gate", "calculate-gaps"),
        ),
        WorkflowStep(
            id="project-3-1",
            skill="coverage-review-project-3-1",
            skill_version=SKILL_VERSION,
            depends_on=("freeze-kernel",),
        ),
        WorkflowStep(
            id="project-5-2",
            skill="coverage-review-project-5-2",
            skill_version=SKILL_VERSION,
            depends_on=("freeze-kernel",),
        ),
        WorkflowStep(
            id="verbalize-5-2",
            skill="coverage-review-verbalize-5-2",
            skill_version=SKILL_VERSION,
            depends_on=("freeze-kernel", "project-5-2"),
        ),
        WorkflowStep(
            id="project-5-3",
            skill="coverage-review-project-5-3",
            skill_version=SKILL_VERSION,
            depends_on=("freeze-kernel",),
        ),
        WorkflowStep(
            id="verbalize-5-3",
            skill="coverage-review-verbalize-5-3",
            skill_version=SKILL_VERSION,
            depends_on=("freeze-kernel", "project-5-3"),
        ),
        WorkflowStep(
            id="customer-analysis",
            skill="coverage-review-customer-analysis",
            skill_version=SKILL_VERSION,
            depends_on=("evidence-gate", "freeze-kernel"),
        ),
        WorkflowStep(
            id="meeting-support",
            skill="coverage-review-meeting-support",
            skill_version=SKILL_VERSION,
            depends_on=("freeze-kernel", "customer-analysis", "verbalize-5-2", "verbalize-5-3"),
        ),
        WorkflowStep(
            id="consistency-gate",
            skill="coverage-review-consistency-gate",
            skill_version=SKILL_VERSION,
            depends_on=("freeze-kernel", "project-5-2", "project-5-3"),
        ),
        WorkflowStep(
            id="language-gate",
            skill="coverage-review-language-gate",
            skill_version=SKILL_VERSION,
            depends_on=(
                "evidence-gate",
                "freeze-kernel",
                "project-5-2",
                "project-5-3",
                "customer-analysis",
                "verbalize-5-2",
                "verbalize-5-3",
                "meeting-support",
            ),
        ),
        WorkflowStep(
            id="internal-report",
            skill="coverage-review-internal-report",
            skill_version=SKILL_VERSION,
            depends_on=(
                "evidence-gate",
                "freeze-kernel",
                "project-5-2",
                "verbalize-5-2",
                "project-5-3",
                "verbalize-5-3",
                "customer-analysis",
                "meeting-support",
                "consistency-gate",
                "language-gate",
            ),
        ),
        WorkflowStep(
            id="agent-review",
            skill="coverage-review-agent-review",
            skill_version=SKILL_VERSION,
            depends_on=("evidence-gate", "freeze-kernel", "project-5-2", "internal-report"),
        ),
        WorkflowStep(
            id="customer-copy",
            skill="coverage-review-customer-copy",
            skill_version=SKILL_VERSION,
            depends_on=(
                "evidence-gate",
                "freeze-kernel",
                "project-5-2",
                "verbalize-5-2",
                "verbalize-5-3",
                "agent-review",
            ),
        ),
        WorkflowStep(
            id="customer-view-model",
            skill="coverage-review-customer-view-model",
            skill_version=SKILL_VERSION,
            depends_on=(
                "evidence-gate",
                "freeze-kernel",
                "project-5-2",
                "verbalize-5-2",
                "project-5-3",
                "verbalize-5-3",
                "agent-review",
                "customer-copy",
            ),
        ),
        WorkflowStep(
            id="customer-report",
            skill="coverage-review-customer-report",
            skill_version=SKILL_VERSION,
            depends_on=(
                "freeze-kernel",
                "project-5-3",
                "customer-copy",
                "customer-view-model",
            ),
        ),
        WorkflowStep(
            id="finalize",
            skill="coverage-review-finalize",
            skill_version=SKILL_VERSION,
            depends_on=("freeze-kernel", "internal-report", "customer-report"),
        ),
    )
    return TaskDefinition(
        name=COVERAGE_REVIEW_TASK_NAME,
        version=COVERAGE_REVIEW_TASK_VERSION,
        description="企业级保障检视：证据→精算→内核→对内→复核→对客。",
        input_schema={
            "type": "object",
            "required": [
                "profile",
                "profile_version",
                "coverage_review_revision",
                "review_revision",
                "runtime_context",
            ],
            "properties": {
                "profile": {"type": "object"},
                "profile_version": {"type": "integer", "minimum": 1},
                "trigger_ids": {"type": "array", "items": {"type": "string"}},
                "answers": {"type": "object"},
                "coverage_review_revision": {"type": "integer", "minimum": 1},
                "review_revision": {"type": "integer", "minimum": 1},
                "runtime_context": {"type": "object"},
            },
        },
        steps=steps,
    )
