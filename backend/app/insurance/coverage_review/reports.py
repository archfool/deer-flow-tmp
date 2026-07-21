"""步骤 5 对内报告与步骤 6 对客 HTML Harness。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from html import escape
from pathlib import Path
from string import Template

from app.insurance.coverage_review.display import format_datetime, format_value
from app.insurance.coverage_review.hashing import sha256_digest
from app.insurance.coverage_review.models import (
    ActionBlock,
    ActionItem,
    ActionNarrative,
    ActionNarrativeItem,
    ActionWave,
    AuxiliaryInsight,
    CustomerAnalysis,
    CustomerDimensionNarrative,
    CustomerFamilyMember,
    CustomerReportArtifact,
    CustomerReportCopy,
    CustomerReportMetric,
    CustomerReportViewModel,
    DiagnosisKernel,
    DimensionFact,
    DimensionNarrative,
    DimensionState,
    EvidenceBundle,
    FutureOutlookItem,
    GenerationMode,
    InternalReport,
    MeetingPlan,
    ReviewPacket,
    ScenarioSimulation,
    WhyBlock,
)
from app.insurance.coverage_review.rules import RuleBundle
from app.insurance.models import CustomerProfile

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
CUSTOMER_REPORT_TEMPLATE_VERSION = "customer-report-v2.3"

_STATE_LABELS = {
    DimensionState.SUFFICIENT: "充足",
    DimensionState.MILD_GAP: "轻度不足",
    DimensionState.SIGNIFICANT_GAP: "显著不足",
    DimensionState.SEVERE_GAP: "严重缺失",
    DimensionState.NO_NEED: "当前阶段无需求",
    DimensionState.UNKNOWN: "待核验",
}

_ROUTE_LABELS = {
    "standard": "标准家庭保障检视",
    "high_net_worth": "高净值家庭综合检视",
    "needs_confirmation": "信息待确认",
}

_TEMPERATURE_LABELS = {
    "open": "开放沟通",
    "fear": "先安抚再核对",
    "defensive": "克制沟通",
    "cold_start": "从事实建立共识",
    "neutral": "中性沟通",
}

_ASSUMPTION_FIELD_LABELS = {
    "investment_risk_tolerance": "投资风险偏好",
    "review_scope": "本次检视范围",
}

_CORRECTION_LABELS = {
    "structure_misallocation": "现有安排的保障方向与家庭责任可能不完全匹配",
    "duplicate_policy": "可能存在重复安排",
    "coverage_mismatch": "成员责任与保障归属可能不匹配",
}

_WAVE_LABELS = {
    ActionWave.FIRST: "第一波",
    ActionWave.SECOND: "第二波",
    ActionWave.THIRD: "第三波",
    ActionWave.MAINTAIN: "维持不动",
}


def render_internal_report(
    *,
    evidence: EvidenceBundle,
    kernel: DiagnosisKernel,
    why_blocks: tuple[WhyBlock, ...],
    dimension_narratives: tuple[DimensionNarrative, ...],
    auxiliary_insights: tuple[AuxiliaryInsight, ...],
    action_block: ActionBlock,
    action_narrative: ActionNarrative,
    validation_results: tuple[str, ...],
    review_revision: int,
    customer_analysis: CustomerAnalysis,
    meeting_plan: MeetingPlan,
) -> InternalReport:
    """按代理人模板组装已校验的 5.1 至 5.6 语言模块。

    Args:
        evidence: 步骤 1 证据包。
        kernel: 不可变诊断内核。
        why_blocks: 5.2 解释块。
        dimension_narratives: 5.2 受约束语言输出。
        auxiliary_insights: 辅助财务说明。
        action_block: 5.3 行动块。
        action_narrative: 5.3 受约束语言输出。
        validation_results: 同源一致性检查结果。
        review_revision: 当前报告复核修订号。
        customer_analysis: 5.1 客户分析。
        meeting_plan: 5.4 至 5.6 沟通支持。

    Returns:
        代理人可读正文与隔离的审计清单。
    """

    lines = [
        f"# {evidence.customer_name} · 保障检视 · 内部诊断（代理人专用 · 请勿外发）",
        "",
        "> 配套客户版报告使用。含测算假设、客户沟通策略、话术与异议处理，仅供代理人使用。",
        "",
        "## 一、客户速读与核心抓手",
        "",
        f"**一句话画像：** {customer_analysis.one_line_profile}",
        "",
        f"**客群定位：** {customer_analysis.customer_segment}",
        "",
        "**最强抓手（按强度）：**",
        *(f"{index}. {item}" for index, item in enumerate(customer_analysis.strongest_hooks, start=1)),
        "",
        f"**促成与沟通判断：** {customer_analysis.conversion_assessment}",
        "",
        "**差异化提醒：**",
        *(f"- {item}" for item in customer_analysis.differentiation_notes),
        "",
        "## 二、测算明细与关键假设",
        "",
        ("**测算来源：** 公司保障检视精确测算工具；工具版本、公式版本与计算引用已留存在审计清单。"),
        "",
        ("**保单现状口径：** 已由代理人或权威档案确认八维现有值。" if kernel.policy_inventory_confirmed else "**保单现状口径：** 尚未完成八维现有值确认，不得生成正式结论。"),
        "",
        "| 维度 | 目标测算 | 当前 | 缺口 | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    for fact in kernel.dimension_facts:
        lines.append(
            "| "
            + " | ".join(
                (
                    fact.dimension_name,
                    format_value(fact.ideal_value, fact.unit, role="target"),
                    format_value(fact.existing_value, fact.unit, role="existing"),
                    format_value(fact.gap_value, fact.unit, role="gap"),
                    _STATE_LABELS[fact.state],
                )
            )
            + " |"
        )

    lines.extend(["", "### 关键假设与提醒", ""])
    lines.extend(f"- {_assumption_text(item.field_key, item.source)}" for item in kernel.assumptions)
    if not kernel.assumptions:
        lines.append("- 本次没有额外测算假设。")
    lines.extend(["", "### 家庭财务观察（不属于保障第九维）", ""])
    if auxiliary_insights:
        lines.extend(f"- {item.summary}" for item in auxiliary_insights)
    else:
        lines.append("- 本次无额外辅助财务观察。")

    lines.extend(["", "### 八维测算解读：为什么是这个结果", ""])
    for narrative in dimension_narratives:
        lines.extend(
            [
                f"#### {narrative.heading}",
                *(f"- 测算口径：{item}" for item in narrative.calculation_explanation),
                f"- 对客意义：{narrative.why_it_matters}",
                f"- 代理人提醒：{narrative.agent_guidance}",
                "",
            ]
        )

    lines.extend(["## 三、必须补问的信息", ""])
    if customer_analysis.confirmation_questions:
        lines.extend(["| 待确认 | 为什么重要 | 自然问法 |", "|---|---|---|"])
        lines.extend(f"| {_cell(item.item)} | {_cell(item.importance)} | {_cell(item.natural_question)} |" for item in customer_analysis.confirmation_questions)
    else:
        lines.append("- 当前无待确认项。")

    lines.extend(["", "## 四、优先级与配置策略", "", action_narrative.overview, ""])
    if action_narrative.budget_tradeoff:
        lines.extend(
            [
                f"**预算与阶段取舍：** {action_narrative.budget_tradeoff}",
                "",
            ]
        )
    for index, item in enumerate(action_narrative.items, start=1):
        lines.extend([f"{index}. **{item.title}**", f"   - 为什么：{item.rationale}", f"   - 怎么说：{item.agent_language}"])
    lines.extend(["", f"> {action_narrative.closing_boundary}", "", "### 建议明确保留的安排", ""])
    if kernel.preserve_items:
        lines.extend(f"- {item.object_ref}已经形成有效基础，建议肯定并保留，不重复增加。" for item in kernel.preserve_items)
    else:
        lines.append("- 当前没有需要单独标注的保留项。")
    if kernel.correction_items:
        lines.extend(f"- 需要先核对的安排：{_correction_text(item.correction_type)}。" for item in kernel.correction_items)
    if not kernel.capabilities.get("policy_level", False):
        lines.append("- 当前数据不足以逐单判断旧条款、重复投保、到期和受益人问题。")

    lines.extend(["", "## 五、面访话术（为什么这么聊 + 怎么聊）", ""])
    for index, script in enumerate(meeting_plan.scripts, start=1):
        lines.extend([f"**{index}. {script.topic}**", f"> {script.script}", f"> *为什么：{script.rationale}*", ""])
    lines.extend(["", "## 六、异议处理", ""])
    lines.extend(["| 客户可能说 | 应对方向 + 话术 |", "|---|---|"])
    lines.extend(f"| {_cell(item.objection)} | {_cell(item.strategy)}。{_cell(item.response)} |" for item in meeting_plan.objection_responses)
    lines.extend(["", "## 七、红线与注意", ""])
    lines.extend(f"- {item}" for item in meeting_plan.red_lines)
    lines.extend(["", "## 八、下一步动作", ""])
    lines.extend(f"{index}. {item}" for index, item in enumerate(meeting_plan.next_actions, start=1))

    return InternalReport(
        kernel_hash=kernel.kernel_hash,
        markdown="\n".join(lines),
        customer_analysis=customer_analysis,
        why_blocks=why_blocks,
        dimension_narratives=dimension_narratives,
        action_block=action_block,
        action_narrative=action_narrative,
        meeting_plan=meeting_plan,
        validation_results=validation_results,
        audit_manifest={
            "review_id": kernel.review_id,
            "review_revision": review_revision,
            "kernel_revision": kernel.revision,
            "kernel_hash": kernel.kernel_hash,
            "rule_bundle_hash": kernel.rule_bundle_hash,
            "calculator_tool_version": kernel.calculator_tool_version,
            "policy_inventory_confirmed": kernel.policy_inventory_confirmed,
            "why_blocks": [item.model_dump(mode="json") for item in why_blocks],
            "action_block": action_block.model_dump(mode="json"),
            "knowledge_refs": list(meeting_plan.knowledge_refs),
            "generation_modes": {
                "customer_analysis": customer_analysis.generation_mode.value,
                "dimension_narratives": [item.generation_mode.value for item in dimension_narratives],
                "action_narrative": action_narrative.generation_mode.value,
                "meeting_plan": meeting_plan.generation_mode.value,
            },
            "validation_results": list(validation_results),
        },
    )


def build_review_packet(
    evidence: EvidenceBundle,
    kernel: DiagnosisKernel,
    report: InternalReport,
    auxiliary_insights: tuple[AuxiliaryInsight, ...],
    review_revision: int | None = None,
) -> ReviewPacket:
    """构造步骤 4 代理人复核包。

    Args:
        evidence: 已通过证据闸门的客户事实。
        kernel: 当前诊断内核。
        report: 已通过一致性校验的对内报告。
        auxiliary_insights: 不进入八维的辅助财务观察。
        review_revision: 当前报告复核修订号。

    Returns:
        绑定 kernel hash 的复核包。
    """

    warnings = [
        *(f"假设：{_assumption_text(item.field_key, item.source)}" for item in kernel.assumptions),
        *(f"冲突：{item}" for item in kernel.conflicts),
    ]
    if not kernel.capabilities.get("policy_level", False):
        warnings.append("逐单能力未开启，不能判断旧条款优劣或直接处置保单")
    return ReviewPacket(
        review_id=kernel.review_id,
        revision=review_revision or kernel.revision,
        kernel_hash=kernel.kernel_hash,
        internal_report=report,
        customer_identity=(
            f"客户：{evidence.customer_name}",
            f"年龄：{evidence.field_value('age', '待确认')}岁",
            f"性别：{_gender_text(str(evidence.field_value('gender', 'undisclosed')))}",
            f"人生阶段：{evidence.field_value('life_stage_name', '待确认')}",
            f"财富水平：{evidence.field_value('wealth_level_name', '待确认')}",
            f"职业：{evidence.field_value('occupation', '待确认') or '待确认'}",
        ),
        trigger_context=(
            f"本次情境：{kernel.trigger_binding.scenario_materials[0] if kernel.trigger_binding.scenario_materials else '围绕当前家庭责任开展检视'}",
            f"检视路径：{_ROUTE_LABELS[kernel.customer_route.route.value]}",
            f"沟通方式：{_TEMPERATURE_LABELS[kernel.trigger_binding.temperature.value]}",
        ),
        dimension_facts=kernel.dimension_facts,
        auxiliary_observations=tuple(item.summary for item in auxiliary_insights),
        preserve_items=tuple(f"{item.object_ref}建议继续保留" for item in kernel.preserve_items),
        correction_items=tuple(f"{_correction_text(item.correction_type)}，需先由代理人核对" for item in kernel.correction_items),
        warnings=tuple(warnings),
    )


def build_customer_view_model(
    *,
    profile: CustomerProfile,
    evidence: EvidenceBundle,
    kernel: DiagnosisKernel,
    why_blocks: tuple[WhyBlock, ...],
    dimension_narratives: tuple[DimensionNarrative, ...],
    auxiliary_insights: tuple[AuxiliaryInsight, ...],
    action_block: ActionBlock,
    action_narrative: ActionNarrative,
    customer_copy: CustomerReportCopy,
    approved_kernel_hash: str,
    review_revision: int | None = None,
) -> CustomerReportViewModel:
    """从已批准内核构建对客报告 ViewModel。

    Args:
        profile: 已绑定当前任务的客户档案快照。
        evidence: 步骤 1 证据包。
        kernel: 当前诊断内核。
        why_blocks: 已校验的 5.2 解释块。
        dimension_narratives: 已批准的 5.2 语言投影。
        auxiliary_insights: 不进入八维的辅助财务观察。
        action_block: 已校验的 5.3 行动块。
        action_narrative: 已校验的 5.3 受控文案。
        customer_copy: 对客文案 Harness 输出。
        approved_kernel_hash: 代理人批准时绑定的内核哈希。
        review_revision: 已批准的报告复核修订号。

    Returns:
        固定 HTML 模板允许消费的结构化数据。

    Raises:
        ValueError: 批准哈希与当前内核不一致时抛出。
    """

    if approved_kernel_hash != kernel.kernel_hash:
        raise ValueError("approved kernel hash does not match current diagnosis kernel")
    if tuple(item.dimension_code for item in customer_copy.dimension_narratives) != tuple(item.dimension_code for item in kernel.dimension_facts):
        raise ValueError("customer copy dimension order does not match diagnosis kernel")
    if tuple(item.dimension_code for item in dimension_narratives) != tuple(item.dimension_code for item in kernel.dimension_facts):
        raise ValueError("approved dimension narrative order does not match diagnosis kernel")

    effective_review_revision = review_revision or kernel.revision
    fact_by_code = {item.dimension_code: item for item in kernel.dimension_facts}
    preserve = tuple(
        (
            f"{item.object_ref}已经达到当前目标，建议维持，不需要重复增加。"
            if item.dimension_code is not None and fact_by_code[item.dimension_code].state is DimensionState.SUFFICIENT
            else f"{item.object_ref}已形成有效基础，建议保留；本次只讨论尚未覆盖的部分。"
        )
        for item in kernel.preserve_items
        if item.capability_available
    )
    scenarios = _scenario_simulations(kernel, why_blocks)
    all_actions = (*action_block.actions, *action_block.maintain_items)
    future_outlook = _future_outlook(
        all_actions,
        action_narrative.items,
    )
    limits = [
        *(_assumption_text(item.field_key, item.source) for item in kernel.assumptions),
        *kernel.conflicts,
    ]
    if not kernel.capabilities.get("policy_level", False):
        limits.append("本报告不对单张保单条款、续保条件或退换保单作结论。")
    family_members = tuple(
        CustomerFamilyMember(
            role=_relationship_label(member.relationship.value),
            name=member.name,
            summary=" · ".join(
                part
                for part in (
                    f"{member.age}岁" if member.age is not None else None,
                    member.occupation,
                    member.health_summary,
                )
                if part
            )
            or "家庭成员信息已确认",
            is_core=member.economic_pillar,
        )
        for member in profile.members
    )
    return CustomerReportViewModel(
        report_id=f"{kernel.review_id}-customer-r{effective_review_revision}",
        customer_name=evidence.customer_name,
        approved_kernel_hash=approved_kernel_hash,
        revision=effective_review_revision,
        generated_at=datetime.now(UTC),
        summary="本报告基于已确认客户信息和公司保障检视测算结果，展示八维现状、建议维持项与行动次序。",
        salutation=customer_copy.salutation,
        opening_paragraphs=customer_copy.opening_paragraphs,
        family_members=family_members,
        family_summary=customer_copy.family_summary,
        financial_snapshot=_financial_snapshot(evidence, kernel),
        status_summary=customer_copy.status_summary,
        diagnosis_dimensions=kernel.dimension_facts,
        dimension_narratives=customer_copy.dimension_narratives,
        auxiliary_observations=tuple(item.summary for item in auxiliary_insights),
        preserve_items=preserve,
        preserve_intro=customer_copy.preserve_intro,
        priority_actions=all_actions,
        priority_action_copy=action_narrative.items,
        roadmap_intro=customer_copy.roadmap_intro,
        budget_tradeoff=action_narrative.budget_tradeoff,
        scenario_simulations=scenarios,
        future_outlook=future_outlook,
        assumptions_and_limits=tuple(limits),
        closing_paragraphs=customer_copy.closing_paragraphs,
        disclaimer="本报告用于保障方向沟通，不构成具体产品、投保保额、缴费方案或收益承诺。",
        generation_modes={
            "dimension_narratives": _combined_generation_mode(dimension_narratives),
            "action_narrative": action_narrative.generation_mode,
            "customer_copy": customer_copy.generation_mode,
        },
        knowledge_refs=customer_copy.knowledge_refs,
    )


async def render_customer_report(
    *,
    view_model: CustomerReportViewModel,
    kernel: DiagnosisKernel,
    action_block: ActionBlock,
    bundle: RuleBundle,
) -> CustomerReportArtifact:
    """使用固定模板异步渲染并校验对客 HTML。

    Args:
        view_model: 代理人批准后的模板输入。
        kernel: 当前不可变内核。
        action_block: 已校验的 5.3 行动块。
        bundle: 当前规则包。

    Returns:
        HTML、ViewModel、manifest 和校验结果。
    """

    template_text, stylesheet = await asyncio.to_thread(_load_template_assets)
    html = Template(template_text).substitute(
        report_title=escape(f"{view_model.customer_name}家庭保障体检报告"),
        stylesheet=stylesheet,
        customer_name=escape(view_model.customer_name),
        summary=escape(view_model.summary),
        generated_at=escape(format_datetime(view_model.generated_at)),
        salutation=escape(view_model.salutation),
        opening_paragraphs=_paragraphs(view_model.opening_paragraphs),
        family_cards=_family_cards(view_model.family_members),
        family_summary=escape(view_model.family_summary),
        financial_snapshot=_financial_snapshot_cards(view_model.financial_snapshot),
        auxiliary_observations=_auxiliary_observations(view_model.auxiliary_observations),
        status_summary=escape(view_model.status_summary),
        dimension_cards=_dimension_cards(view_model.diagnosis_dimensions, view_model.dimension_narratives),
        roadmap_intro=escape(view_model.roadmap_intro),
        preserve_items=_list_items(view_model.preserve_items or ("当前没有需要特别强调的维持项。",)),
        preserve_intro=escape(view_model.preserve_intro),
        priority_actions=_action_items(view_model.priority_actions, view_model.priority_action_copy, kernel),
        budget_tradeoff=_budget_tradeoff(view_model.budget_tradeoff),
        handoff_statement=escape(action_block.handoff_statement),
        scenario_blocks=_scenario_blocks(view_model.scenario_simulations),
        future_outlook=_future_outlook_blocks(view_model.future_outlook),
        assumptions=_list_items(view_model.assumptions_and_limits or ("本次没有额外假设。",)),
        closing_paragraphs=_paragraphs(view_model.closing_paragraphs),
        disclaimer=escape(view_model.disclaimer),
    )
    validations = validate_customer_report(
        html=html,
        view_model=view_model,
        kernel=kernel,
        action_block=action_block,
        bundle=bundle,
    )
    manifest = {
        "report_id": view_model.report_id,
        "kernel_hash": kernel.kernel_hash,
        "rule_bundle_hash": bundle.bundle_hash,
        "template_version": CUSTOMER_REPORT_TEMPLATE_VERSION,
        "view_model_hash": sha256_digest(view_model),
        "html_hash": sha256_digest(html),
        "generation_modes": {key: value.value for key, value in view_model.generation_modes.items()},
        "knowledge_refs": list(view_model.knowledge_refs),
    }
    return CustomerReportArtifact(
        kernel_hash=kernel.kernel_hash,
        html=html,
        view_model=view_model,
        manifest=manifest,
        validation_results=validations,
    )


def validate_customer_report(
    *,
    html: str,
    view_model: CustomerReportViewModel,
    kernel: DiagnosisKernel,
    action_block: ActionBlock,
    bundle: RuleBundle,
) -> tuple[str, ...]:
    """执行对客报告的机械内容门禁。

    Args:
        html: 已渲染 HTML。
        view_model: 模板输入。
        kernel: 当前诊断内核。
        action_block: 5.3 行动块。
        bundle: 当前规则包。

    Returns:
        通过的校验项。

    Raises:
        ValueError: 报告不完整、漂移或越过产品边界时抛出。
    """

    if view_model.approved_kernel_hash != kernel.kernel_hash:
        raise ValueError("customer report is not bound to approved kernel")
    if not kernel.policy_inventory_confirmed:
        raise ValueError("customer report requires confirmed eight-dimension current values")
    if len(view_model.diagnosis_dimensions) != 8:
        raise ValueError("customer report must contain exactly eight dimensions")
    if len(view_model.dimension_narratives) != 8:
        raise ValueError("customer report must contain eight dimension narratives")
    if tuple(item.dimension_code for item in view_model.dimension_narratives) != tuple(item.dimension_code for item in kernel.dimension_facts):
        raise ValueError("customer report dimension narrative order drift")
    if "EMERGENCY_LIQUIDITY_ALERT" in html:
        raise ValueError("auxiliary metric leaked into eight-dimension table")
    if action_block.boundary_hits:
        raise ValueError("customer action block contains boundary violations")
    expected_actions = {item.action_id for item in (*action_block.actions, *action_block.maintain_items)}
    if {item.action_id for item in view_model.priority_action_copy} != expected_actions:
        raise ValueError("customer action copy does not cover approved action block")
    if {item.action_id for item in view_model.priority_actions} != expected_actions:
        raise ValueError("customer roadmap does not cover all gap and maintain actions")
    if any(mode is GenerationMode.FALLBACK for mode in view_model.generation_modes.values()):
        raise ValueError("customer report contains fallback-generated language")
    narrative_by_code = {item.dimension_code: item for item in view_model.dimension_narratives}
    for fact in kernel.dimension_facts:
        narrative_text = " ".join(narrative_by_code[fact.dimension_code].calculation_explanation)
        missing_component = next(
            (component.label for component in fact.derivation_components if component.label not in narrative_text),
            None,
        )
        if missing_component is not None:
            raise ValueError(f"customer calculation explanation misses derivation component: {missing_component}")
    expected_scenarios = min(
        3,
        sum(item.priority_rank is not None for item in kernel.dimension_facts),
    )
    if len(view_model.scenario_simulations) < expected_scenarios:
        raise ValueError("customer report misses priority scenario simulations")
    if len(view_model.future_outlook) < 3:
        raise ValueError("customer report must contain three future phases")
    if kernel.annual_premium_budget_yuan is not None and not view_model.budget_tradeoff:
        raise ValueError("confirmed budget is missing from customer report")
    for pattern in bundle.style_contracts["boundary_patterns"]:
        if pattern in html:
            raise ValueError(f"customer report contains forbidden pattern: {pattern}")
    required_sections = (
        "你的家庭",
        "家庭财务摘要",
        "一句话现状",
        "八维保障体检",
        "一张行动路线图",
        "建议维持的安排",
        "情境推演",
        "未来检视安排",
    )
    missing_section = next((item for item in required_sections if item not in html), None)
    if missing_section is not None:
        raise ValueError(f"customer report missing required section: {missing_section}")
    internal_tokens = {
        "reason_code",
        "framework_code",
        "kernel_hash",
        "rule_bundle",
        "Knowledge Skill",
        "review_scope",
        "structure_misallocation",
        "PASS:",
        *(item.code for item in bundle.reasons),
        *(item.framework_code for item in bundle.reasons),
    }
    leaked = next((item for item in internal_tokens if item and item in html), None)
    if leaked is not None:
        raise ValueError(f"customer report contains internal token: {leaked}")
    if "$" in html:
        raise ValueError("customer report contains unresolved template placeholder")
    if "<script" in html.lower():
        raise ValueError("customer report template must not contain scripts")
    raw_unit = next(
        (item for item in ("CNY_PER_YEAR", "responsibility_tier", ">CNY<") if item in html),
        None,
    )
    if raw_unit is not None:
        raise ValueError(f"customer report contains raw unit: {raw_unit}")
    internal_medical_term = next(
        (item for item in ("层责任", "责任层") if item in html),
        None,
    )
    if internal_medical_term is not None:
        raise ValueError(f"customer report contains internal medical term: {internal_medical_term}")
    return (
        "PASS: approved kernel hash bound",
        "PASS: confirmed policy inventory bound",
        "PASS: eight dimensions and complete derivations rendered",
        "PASS: gap, maintain and budget projections reused",
        "PASS: no auxiliary ninth dimension",
        "PASS: scenarios and future phases rendered",
        "PASS: required sections, generation mode and internal-token guard passed",
        "PASS: product boundary and active-content guard passed",
    )


def _scenario_simulations(
    kernel: DiagnosisKernel,
    why_blocks: tuple[WhyBlock, ...],
) -> tuple[ScenarioSimulation, ...]:
    """用前三个优先维构造带事实锚点的情境模拟。

    Args:
        kernel: 已获批的不可变诊断内核。
        why_blocks: 八维 5.2 推导资产。

    Returns:
        最多三个与优先维度、触发场景和行动方向绑定的情境块。
    """

    top_facts = tuple(
        item
        for item in sorted(
            kernel.dimension_facts,
            key=lambda fact: fact.priority_rank or 99,
        )
        if item.priority_rank is not None
    )[:3]
    if not top_facts:
        return ()
    why_by_code = {item.dimension_code: item for item in why_blocks}
    trigger_materials = kernel.trigger_binding.scenario_materials or ("当前家庭责任是否能被现有安排承接",)
    assumptions = tuple(_assumption_text(item.field_key, item.source) for item in kernel.assumptions)
    return tuple(
        ScenarioSimulation(
            title=f"{fact.dimension_name}情境推演",
            trigger_condition=trigger_materials[index % len(trigger_materials)],
            reasoning_steps=tuple(item for item in why_by_code[fact.dimension_code].derivation_chain if why_by_code[fact.dimension_code].framework_code not in item),
            conclusion=_benefit_conclusion(fact),
            fact_refs=why_by_code[fact.dimension_code].fact_refs,
            dimension_refs=(fact.dimension_code,),
            assumptions=assumptions,
            unknowns=(("具体产品、核保和条款结果需进入方案设计后确认。",) if fact.requires_agent_review else ()),
            action_or_preserve=(
                "按既定优先级进入下一步方案设计。"
                if fact.state
                in {
                    DimensionState.MILD_GAP,
                    DimensionState.SIGNIFICANT_GAP,
                    DimensionState.SEVERE_GAP,
                }
                else "维持现有安排。"
            ),
        )
        for index, fact in enumerate(top_facts)
    )


def _benefit_conclusion(fact: DimensionFact) -> str:
    """根据内核状态生成情境模拟结论。"""

    if fact.state is DimensionState.SUFFICIENT:
        return "现有安排达到当前测算目标，建议维持。"
    if fact.state is DimensionState.NO_NEED:
        return "当前人生阶段不需要将该维列入行动清单。"
    if fact.state is DimensionState.UNKNOWN:
        return "信息不足，需先由代理人核验事实。"
    return f"权威测算显示该维缺口为 {format_value(fact.gap_value, fact.unit, role='gap')}，建议按既定次序讨论保障方向。"


def _financial_snapshot(
    evidence: EvidenceBundle,
    kernel: DiagnosisKernel,
) -> tuple[CustomerReportMetric, ...]:
    """构造对客报告的家庭财务摘要。

    Args:
        evidence: 已通过字段闸门的证据包。
        kernel: 已固化的诊断内核。

    Returns:
        只包含已确认数值的中文展示指标。
    """

    specs = (
        (
            "本人年收入",
            evidence.field_value("annual_income_wan"),
            "wan",
            "用于收入责任与工作能力影响测算",
        ),
        (
            "配偶年收入",
            evidence.field_value("spouse_annual_income_wan"),
            "wan",
            "用于家庭收入结构测算",
        ),
        (
            "家庭月支出",
            evidence.field_value("family_expense_yuan_month"),
            "CNY",
            "用于家庭生活责任测算",
        ),
        (
            "大额贷款余额",
            evidence.field_value("large_loan_wan"),
            "wan",
            "用于债务责任测算",
        ),
    )
    metrics = [
        CustomerReportMetric(
            label=label,
            value=(format_value(Decimal(str(value)) * Decimal("10000"), "CNY") if unit == "wan" else format_value(Decimal(str(value)), unit)),
            note=note,
        )
        for label, value, unit, note in specs
        if value is not None
    ]
    if kernel.annual_premium_budget_yuan is not None:
        metrics.append(
            CustomerReportMetric(
                label="年度保费预算",
                value=format_value(
                    kernel.annual_premium_budget_yuan,
                    "CNY",
                ),
                note="只用于安排先后，不用于倒推保额",
            )
        )
    return tuple(metrics)


def _future_outlook(
    actions: tuple[ActionItem, ...],
    copy_items: tuple[ActionNarrativeItem, ...],
) -> tuple[FutureOutlookItem, ...]:
    """把 5.3 波次组装为三阶段未来检视安排。

    Args:
        actions: 5.3 的缺口动作和维持动作。
        copy_items: 与动作 ID 一一绑定的受控文案。

    Returns:
        近期、中期和持续复核三个固定阶段。
    """

    copy_by_id = {item.action_id: item for item in copy_items}

    def titles(waves: set[ActionWave]) -> str:
        """汇总指定波次的行动标题。

        Args:
            waves: 当前阶段包含的行动波次。

        Returns:
            中文顿号连接的行动标题。
        """

        selected = [copy_by_id[item.action_id].title for item in actions if item.wave in waves]
        return "、".join(selected)

    near_term = titles({ActionWave.FIRST})
    medium_term = titles({ActionWave.SECOND, ActionWave.THIRD})
    maintain = titles({ActionWave.MAINTAIN})
    return (
        FutureOutlookItem(
            phase="近期",
            title="先完成第一波核对",
            summary=near_term or "先核对当前事实和第一优先方向，不急于进入产品选择。",
        ),
        FutureOutlookItem(
            phase="中期",
            title="按预算分阶段推进",
            summary=medium_term or "当前没有必须进入中期的新增方向，保持观察即可。",
        ),
        FutureOutlookItem(
            phase="持续",
            title="保留有效安排并定期复检",
            summary=maintain or "家庭责任、收入或负债变化后，建议重新进行八维检视。",
        ),
    )


def _combined_generation_mode(
    items: tuple[DimensionNarrative, ...],
) -> GenerationMode:
    """汇总一组维度文案的生成方式。

    Args:
        items: 八维语言投影。

    Returns:
        优先暴露 fallback，其次为模型生成，否则为规则受控生成。
    """

    modes = {item.generation_mode for item in items}
    if GenerationMode.FALLBACK in modes:
        return GenerationMode.FALLBACK
    if GenerationMode.HYBRID in modes:
        return GenerationMode.HYBRID
    if GenerationMode.MODEL in modes:
        return GenerationMode.MODEL
    return GenerationMode.RULE_BOUND


@lru_cache(maxsize=1)
def _load_template_assets() -> tuple[str, str]:
    """读取并缓存版本化 HTML 模板和样式。"""

    template = (TEMPLATE_DIR / "customer_report_v2_1.html").read_text(encoding="utf-8")
    stylesheet = (TEMPLATE_DIR / "customer_report_v2_1.css").read_text(encoding="utf-8")
    return template, stylesheet


def _dimension_cards(
    facts: tuple[DimensionFact, ...],
    narratives: tuple[CustomerDimensionNarrative, ...],
) -> str:
    """渲染固定结构的八维解释卡。

    Args:
        facts: 从获批诊断内核注入的八维数字。
        narratives: 与八维一一对应的对客文案。

    Returns:
        不包含模型生成 HTML 的维度卡标记。
    """

    narrative_by_code = {item.dimension_code: item for item in narratives}
    cards = []
    for fact in facts:
        narrative = narrative_by_code[fact.dimension_code]
        calculation = "".join(f"<li>{escape(item)}</li>" for item in narrative.calculation_explanation)
        priority = f"第 {fact.priority_rank} 顺位" if fact.priority_rank is not None else "维持或待确认"
        cards.append(
            '<article class="dimension-card">'
            f'<div class="dimension-head"><div><h3>{escape(narrative.headline)}</h3></div><span class="status">{escape(_STATE_LABELS[fact.state])}</span></div>'
            '<div class="metrics">'
            f"<div><span>当前</span><strong>{escape(format_value(fact.existing_value, fact.unit, role='existing'))}</strong></div>"
            f"<div><span>目标</span><strong>{escape(format_value(fact.ideal_value, fact.unit, role='target'))}</strong></div>"
            f"<div><span>缺口</span><strong>{escape(format_value(fact.gap_value, fact.unit, role='gap'))}</strong></div>"
            f"<div><span>次序</span><strong>{escape(priority)}</strong></div>"
            "</div>"
            f'<div class="calculation"><h4>这个结果怎么来</h4><ul>{calculation}</ul></div>'
            f'<p class="why"><b>为什么值得关注：</b>{escape(narrative.why_it_matters)}</p>'
            f'<p class="suggestion"><b>建议：</b>{escape(narrative.suggestion)}</p>'
            "</article>"
        )
    return "".join(cards)


def _list_items(items: tuple[str, ...]) -> str:
    """渲染转义后的无序列表项。"""

    return "".join(f"<li>{escape(item)}</li>" for item in items)


def _paragraphs(items: tuple[str, ...]) -> str:
    """渲染转义后的段落。

    Args:
        items: 已通过 ViewModel 校验的段落文案。

    Returns:
        HTML 段落标记。
    """

    return "".join(f"<p>{escape(item)}</p>" for item in items)


def _family_cards(items: tuple[CustomerFamilyMember, ...]) -> str:
    """渲染家庭成员摘要卡。

    Args:
        items: 客户档案中已确认的家庭成员。

    Returns:
        家庭成员卡标记。
    """

    if not items:
        return '<article class="family-member"><span>家庭信息</span><strong>待进一步确认</strong></article>'
    return "".join(f'<article class="family-member{" core" if item.is_core else ""}"><span>{escape(item.role)}</span><strong>{escape(item.name)}</strong><p>{escape(item.summary)}</p></article>' for item in items)


def _financial_snapshot_cards(
    items: tuple[CustomerReportMetric, ...],
) -> str:
    """渲染家庭财务摘要指标。

    Args:
        items: 已确认的家庭财务摘要。

    Returns:
        固定结构的财务摘要卡片。
    """

    if not items:
        return '<article class="financial-metric"><span>家庭财务信息</span><strong>待确认</strong><p>本次不展示未经确认的数值。</p></article>'
    return "".join(f'<article class="financial-metric"><span>{escape(item.label)}</span><strong>{escape(item.value)}</strong><p>{escape(item.note)}</p></article>' for item in items)


def _auxiliary_observations(items: tuple[str, ...]) -> str:
    """渲染独立于八维的辅助财务观察。

    Args:
        items: 5.2 辅助域的业务说明。

    Returns:
        辅助观察 HTML；没有可用指标时返回边界说明。
    """

    visible = items or ("本次没有足够数据形成备用金观察，该项不影响八维保障结论。",)
    return _list_items(visible)


def _budget_tradeoff(value: str | None) -> str:
    """渲染预算与阶段取舍说明。

    Args:
        value: 5.3 生成的预算约束说明。

    Returns:
        可直接插入固定模板的预算块。
    """

    if not value:
        return ""
    return f'<aside class="budget-note"><strong>预算与阶段取舍</strong><p>{escape(value)}</p></aside>'


def _action_items(
    actions: tuple[ActionItem, ...],
    copy_items: tuple[ActionNarrativeItem, ...],
    kernel: DiagnosisKernel,
) -> str:
    """渲染与获批动作 ID 绑定的对客路线图。

    Args:
        actions: 从获批 5.3 投影注入的封闭动作。
        copy_items: 只允许表达已有动作的受控文案。
        kernel: 用于获取维度名和锁定数字的内核。

    Returns:
        行动路线图标记。
    """

    copy_by_id = {item.action_id: item for item in copy_items}
    facts = {item.dimension_code: item for item in kernel.dimension_facts}
    rows = []
    for index, action in enumerate(actions, start=1):
        copy = copy_by_id[action.action_id]
        fact = facts.get(action.dimension_code)
        gap = "当前有效安排建议维持" if action.wave is ActionWave.MAINTAIN else format_value(fact.gap_value, fact.unit, role="gap") if fact is not None else "按家庭整体结构"
        rows.append(
            f'<li class="roadmap-item{" maintain" if action.wave is ActionWave.MAINTAIN else ""}">'
            f'<span class="roadmap-index">{index}</span>'
            f'<div><span class="wave">{escape(_WAVE_LABELS[action.wave])}</span>'
            f"<h3>{escape(copy.title)}</h3><p>{escape(copy.rationale)}</p>"
            f'<p class="agent-language">{escape(copy.agent_language)}</p>'
            f"<small>{'安排说明' if action.wave is ActionWave.MAINTAIN else '当前缺口'}：{escape(gap)}</small></div>"
            "</li>"
        )
    return "".join(rows)


def _scenario_blocks(items: tuple[ScenarioSimulation, ...]) -> str:
    """渲染带事实锚点的情境模拟。"""

    if not items:
        return '<p class="scenario">当前没有足够事实生成情境推演。</p>'
    blocks = []
    for item in items:
        steps = "".join(f"<li>{escape(step)}</li>" for step in item.reasoning_steps)
        assumptions = _list_items(item.assumptions) if item.assumptions else "<li>本情境只使用已确认事实。</li>"
        unknowns = _list_items(item.unknowns) if item.unknowns else "<li>暂无额外待确认项。</li>"
        blocks.append(
            f'<article class="scenario"><h3>{escape(item.title)}</h3>'
            f'<p class="scenario-trigger">{escape(item.trigger_condition)}</p>'
            f"<ol>{steps}</ol><p>{escape(item.conclusion)}</p>"
            f'<div class="scenario-meta"><div><strong>假设</strong><ul>{assumptions}</ul></div>'
            f"<div><strong>仍待确认</strong><ul>{unknowns}</ul></div></div>"
            f'<p class="scenario-action">{escape(item.action_or_preserve)}</p>'
            "</article>"
        )
    return "".join(blocks)


def _future_outlook_blocks(items: tuple[FutureOutlookItem, ...]) -> str:
    """渲染未来三个阶段的检视安排。

    Args:
        items: 固定三阶段未来安排。

    Returns:
        时间轴卡片 HTML。
    """

    return "".join(f'<article class="future-item"><span class="future-phase">{escape(item.phase)}</span><h3>{escape(item.title)}</h3><p>{escape(item.summary)}</p></article>' for item in items)


def _relationship_label(value: str) -> str:
    """把档案关系枚举转换为对客称呼。

    Args:
        value: CustomerProfile 成员关系枚举值。

    Returns:
        对客家庭卡使用的中文角色。
    """

    return {
        "self": "本人",
        "spouse": "配偶",
        "child": "子女",
        "parent": "父母",
        "other": "家庭成员",
    }.get(value, "家庭成员")


def _gender_text(value: str) -> str:
    """把客户中心性别枚举转换为代理人可读文本。

    Args:
        value: 客户中心返回的规范性别值。

    Returns:
        中文性别说明。
    """

    return {
        "male": "男",
        "female": "女",
        "other": "其他",
        "undisclosed": "未披露",
    }.get(value, "待确认")


def _assumption_text(field_key: str, source: str) -> str:
    """把内核假设字段名转换为业务可读说明。

    Args:
        field_key: 内核记录的规范字段名。
        source: 包含默认值来源的原始说明。

    Returns:
        不暴露配置字段名的代理人和客户可读文本。
    """

    label = _ASSUMPTION_FIELD_LABELS.get(field_key, "本次测算参数")
    value_text = source.removeprefix(field_key).strip()
    return f"{label}{value_text}" if value_text else f"{label}采用已批准默认口径"


def _correction_text(correction_type: str) -> str:
    """把纠错枚举转换为代理人可读说明。

    Args:
        correction_type: 内核中的纠错类型。

    Returns:
        不暴露开发枚举的业务描述。
    """

    return _CORRECTION_LABELS.get(correction_type, "现有安排与家庭责任需要进一步核对")


def _cell(value: str) -> str:
    """转义 Markdown 表格单元格中的分隔符。

    Args:
        value: 待写入表格的文本。

    Returns:
        不会破坏表格结构的文本。
    """

    return value.replace("|", "\\|").replace("\n", " ")
