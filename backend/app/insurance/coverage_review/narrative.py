"""保障检视报告的受约束语言 Harness。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from app.insurance.coverage_review.display import format_value
from app.insurance.coverage_review.hashing import sha256_digest
from app.insurance.coverage_review.knowledge import KnowledgeExcerpt
from app.insurance.coverage_review.models import (
    DIMENSION_ORDER,
    ActionBlock,
    ActionNarrative,
    ActionNarrativeItem,
    ActionType,
    AgentScript,
    ConfirmationQuestion,
    CustomerAnalysis,
    CustomerDimensionNarrative,
    CustomerReportCopy,
    DiagnosisKernel,
    DimensionNarrative,
    DimensionState,
    EvidenceBundle,
    GenerationMode,
    MeetingPlan,
    ObjectionResponse,
    ReviewPatchExtraction,
    WhyBlock,
)
from deerflow.models import create_chat_model

logger = logging.getLogger(__name__)

NARRATIVE_HARNESS_VERSION = "coverage-review-narrative-harness-v2.3"
NARRATIVE_PROMPT_VERSION = "coverage-review-narrative-prompt-v2.3"
NARRATIVE_STRUCTURED_OUTPUT_MODE = "tool-call-auto-pydantic-v1"

_STATE_TEXT = {
    DimensionState.SEVERE_GAP: "缺口较大",
    DimensionState.SIGNIFICANT_GAP: "有明显缺口",
    DimensionState.MILD_GAP: "有部分缺口",
    DimensionState.SUFFICIENT: "当前充足",
    DimensionState.NO_NEED: "当前无需增加",
    DimensionState.UNKNOWN: "待确认",
}

_ACTION_TEXT = {
    ActionType.NEW_POLICY: "建立基础安排",
    ActionType.ADD_COVERAGE: "补充现有安排",
    ActionType.ADJUST: "优先核对并调整",
    ActionType.REDUCED_PAID_UP: "由代理人复核处置候选",
    ActionType.SURRENDER: "由代理人复核处置候选",
    ActionType.MAINTAIN: "保留已有有效部分",
}

_ROUTE_TEXT = {
    "standard": "标准家庭保障检视",
    "high_net_worth": "高净值家庭综合检视",
    "needs_confirmation": "信息待确认",
}

_QUESTION_LABELS = {
    "customer_goal": "客户本次最想解决的目标",
    "recent_concern": "客户近期最关注的风险",
    "risk_attitude": "客户面对风险时的态度",
}

_CUSTOMER_VISIBLE_BOUNDARY_PATTERNS = (
    "建议投保",
    "推荐购买",
    "产品代码",
    "缴费期",
    "保证收益",
    "收益率",
    "最后机会",
)


class CoverageReviewNarrativeHarness(Protocol):
    """保障检视语言节点的可注入契约。"""

    @property
    def runtime_metadata(self) -> dict[str, Any]:
        """返回当前语言 Harness 的可审计运行元数据。"""

    async def analyze_customer(
        self,
        evidence: EvidenceBundle,
        kernel: DiagnosisKernel,
        preferences: dict[str, Any],
    ) -> CustomerAnalysis:
        """生成受白名单事实约束的客户分析。"""

    async def verbalize_explanations(
        self,
        kernel: DiagnosisKernel,
        why_blocks: tuple[WhyBlock, ...],
        preferences: dict[str, Any],
    ) -> tuple[DimensionNarrative, ...]:
        """将 5.2 规则资产翻译为代理人可读表达。"""

    async def verbalize_actions(
        self,
        kernel: DiagnosisKernel,
        action_block: ActionBlock,
        preferences: dict[str, Any],
    ) -> ActionNarrative:
        """将 5.3 封闭行动资产翻译为代理人可读表达。"""

    async def plan_meeting(
        self,
        kernel: DiagnosisKernel,
        analysis: CustomerAnalysis,
        dimension_narratives: tuple[DimensionNarrative, ...],
        action_narrative: ActionNarrative,
        knowledge: tuple[KnowledgeExcerpt, ...],
        preferences: dict[str, Any],
    ) -> MeetingPlan:
        """生成面谈计划、代理人话术和异议处理。"""

    async def write_customer_copy(
        self,
        evidence: EvidenceBundle,
        kernel: DiagnosisKernel,
        why_blocks: tuple[WhyBlock, ...],
        dimension_narratives: tuple[DimensionNarrative, ...],
        action_narrative: ActionNarrative,
        knowledge: tuple[KnowledgeExcerpt, ...],
        preferences: dict[str, Any],
    ) -> CustomerReportCopy:
        """生成对客 ViewModel 中允许模型填写的文案。"""

    async def extract_review_patch(
        self,
        feedback: str,
        current_input: dict[str, Any],
    ) -> ReviewPatchExtraction:
        """将代理人的自然语言修改意见提取为候选 Patch。"""


class RuleBoundNarrativeHarness:
    """在不增加事实的前提下生成保守的结构化文案。"""

    @property
    def runtime_metadata(self) -> dict[str, Any]:
        """返回规则受控降级实现的版本元数据。

        Returns:
            可固化到任务上下文的模型、Harness 与提示词版本。
        """

        return {
            "model_name": "rule-bound",
            "provider_model": "rule-bound",
            "harness_version": NARRATIVE_HARNESS_VERSION,
            "prompt_versions": {"shared": NARRATIVE_PROMPT_VERSION},
            "structured_output_mode": "rule-bound-pydantic-v1",
        }

    async def analyze_customer(
        self,
        evidence: EvidenceBundle,
        kernel: DiagnosisKernel,
        preferences: dict[str, Any],
    ) -> CustomerAnalysis:
        """根据已固化事实生成客户速读。

        Args:
            evidence: 已通过证据闸门的客户事实。
            kernel: 不可变诊断内核。
            preferences: 代理人已确认的叙事偏好。

        Returns:
            带事实引用的客户分析。
        """

        focus = tuple(item.dimension_name for item in sorted(kernel.dimension_facts, key=lambda fact: fact.priority_rank or 99) if item.priority_rank is not None)[:3]
        life_stage = evidence.field_value("life_stage_name", "人生阶段待确认")
        wealth = evidence.field_value("wealth_level_name", "财富水平待确认")
        occupation = evidence.field_value("occupation") or "职业待确认"
        questions = tuple(
            ConfirmationQuestion(
                item=_QUESTION_LABELS.get(question.field_key, "需要进一步确认的信息"),
                importance=question.reason,
                natural_question=question.prompt,
                fact_refs=(),
            )
            for question in evidence.enhancement_questions
        )
        refs = tuple(dict.fromkeys(item.source_ref for item in evidence.fields if item.source_ref))
        focus_text = "、".join(focus) or "当前已有安排"
        communication = "先核对事实和测算口径，再讨论行动次序，不推动当场决策。"
        preference_note = _preference_note(preferences)
        if preference_note:
            communication = f"{communication}{preference_note}"
        return CustomerAnalysis(
            one_line_profile=f"{evidence.customer_name}处于{life_stage}，职业为{occupation}，当前检视重心为{focus_text}。",
            customer_segment=f"{wealth} · {_ROUTE_TEXT[kernel.customer_route.route.value]}",
            strongest_hooks=tuple(f"从{item}的家庭责任和已有安排切入" for item in focus),
            conversion_assessment=communication,
            differentiation_notes=(
                f"本次从{kernel.trigger_binding.scenario_materials[0] if kernel.trigger_binding.scenario_materials else '当前家庭责任'}切入。",
                *kernel.trigger_binding.guardrails,
            ),
            summary_points=(f"人生阶段：{life_stage}。", f"财富水平：{wealth}。", f"检视路径：{_ROUTE_TEXT[kernel.customer_route.route.value]}。"),
            core_focus=focus,
            confirmation_questions=questions,
            fact_refs=refs,
            generation_mode=GenerationMode.RULE_BOUND,
        )

    async def verbalize_explanations(
        self,
        kernel: DiagnosisKernel,
        why_blocks: tuple[WhyBlock, ...],
        preferences: dict[str, Any],
    ) -> tuple[DimensionNarrative, ...]:
        """使用固定资产生成不暴露理由码的维度解释。

        Args:
            kernel: 不可变诊断内核。
            why_blocks: 已选中的 5.2 解释资产。
            preferences: 代理人已确认的叙事偏好。

        Returns:
            与八维一一对应的受控文案。
        """

        del preferences
        facts = {item.dimension_code: item for item in kernel.dimension_facts}
        result = []
        for block in why_blocks:
            fact = facts[block.dimension_code]
            state = _STATE_TEXT[fact.state]
            result.append(
                DimensionNarrative(
                    dimension_code=block.dimension_code,
                    heading=f"{fact.dimension_name}：{state}",
                    calculation_explanation=_public_derivation(block),
                    why_it_matters=_dimension_significance(
                        fact.dimension_name,
                        block,
                        fact.existing_value,
                        fact.ideal_value,
                        fact.unit,
                    ),
                    agent_guidance=_agent_guidance(fact.state, fact.dimension_name),
                    fact_refs=block.fact_refs,
                    generation_mode=GenerationMode.RULE_BOUND,
                )
            )
        return tuple(result)

    async def verbalize_actions(
        self,
        kernel: DiagnosisKernel,
        action_block: ActionBlock,
        preferences: dict[str, Any],
    ) -> ActionNarrative:
        """按内核次序生成行动表达。

        Args:
            kernel: 不可变诊断内核。
            action_block: 已通过边界检查的 5.3 行动块。
            preferences: 代理人已确认的叙事偏好。

        Returns:
            不包含内部理由码的行动文案。
        """

        del preferences
        facts = {item.dimension_code: item for item in kernel.dimension_facts}
        items = []
        for action in (*action_block.actions, *action_block.maintain_items):
            dimension = facts[action.dimension_code].dimension_name if action.dimension_code is not None else "家庭整体结构"
            action_text = _ACTION_TEXT[action.action_type]
            items.append(
                ActionNarrativeItem(
                    action_id=action.action_id,
                    title=f"{dimension}：{action_text}",
                    rationale=_action_rationale(action, facts.get(action.dimension_code)),
                    agent_language=_action_language(
                        dimension,
                        action_text,
                        action.sequence_reason_codes,
                    ),
                )
            )
        return ActionNarrative(
            overview="行动次序由家庭责任、缺口程度和触发场景共同确定，先处理当前影响最大的方向。",
            items=tuple(items),
            closing_boundary=action_block.handoff_statement,
            budget_tradeoff=action_block.budget_tradeoff,
            generation_mode=GenerationMode.RULE_BOUND,
        )

    async def plan_meeting(
        self,
        kernel: DiagnosisKernel,
        analysis: CustomerAnalysis,
        dimension_narratives: tuple[DimensionNarrative, ...],
        action_narrative: ActionNarrative,
        knowledge: tuple[KnowledgeExcerpt, ...],
        preferences: dict[str, Any],
    ) -> MeetingPlan:
        """生成与当前客户事实绑定的面谈支持。

        Args:
            kernel: 不可变诊断内核。
            analysis: 已校验的 5.1 客户分析。
            dimension_narratives: 已校验的 5.2 维度文案。
            action_narrative: 已校验的 5.3 行动文案。
            knowledge: 最小化加载的书籍 Knowledge Skill 片段。
            preferences: 代理人已确认的叙事偏好。

        Returns:
            会谈目标、话术、异议处理和下一步动作。
        """

        del preferences
        focus = "、".join(analysis.core_focus) or "已确认的保障安排"
        scripts = [
            AgentScript(
                topic="开场",
                script=f"我们今天先不谈具体产品，先把你们家的{focus}看清楚，确认哪些已经安排得好，哪些值得补充。",
                rationale="先降低销售压力，再进入事实核对。",
                fact_refs=analysis.fact_refs,
            )
        ]
        scripts.extend(
            AgentScript(
                topic=item.heading,
                script=item.agent_guidance,
                rationale=item.why_it_matters,
                fact_refs=item.fact_refs,
            )
            for item in dimension_narratives[:3]
        )
        return MeetingPlan(
            objectives=("确认客户事实、假设和触发场景。", f"围绕{focus}说清缺口和保留项。", "得到客户对下一步方向的明确意愿。"),
            agenda=("说明本次检视的数据范围。", "核对待确认信息。", "解释八维现状和行动次序。", "记录新事实并确认后续动作。"),
            scripts=tuple(scripts),
            objection_responses=(
                ObjectionResponse(objection="现在不着急", strategy="回到检视目标", response="可以不急着做决定，先把现有安排是否足够确认清楚。"),
                ObjectionResponse(objection="担心预算", strategy="保留优先级", response="先保留影响最大的第一波方向，长期项可以分阶段讨论。"),
                ObjectionResponse(objection="想再比较", strategy="把需求与产品分开", response="先把需求和次序定清楚，具体产品放到方案设计环节再比较。"),
            ),
            red_lines=(*kernel.trigger_binding.guardrails, "不承诺收益，不制造焦虑，不绕过健康告知和核保。", "未开启逐单能力时，不评判旧条款优劣或建议退换保。"),
            next_actions=tuple(item.agent_language for item in action_narrative.items[:4]),
            knowledge_refs=tuple(f"{item.skill_id}@{item.version}" for item in knowledge),
            generation_mode=GenerationMode.RULE_BOUND,
        )

    async def write_customer_copy(
        self,
        evidence: EvidenceBundle,
        kernel: DiagnosisKernel,
        why_blocks: tuple[WhyBlock, ...],
        dimension_narratives: tuple[DimensionNarrative, ...],
        action_narrative: ActionNarrative,
        knowledge: tuple[KnowledgeExcerpt, ...],
        preferences: dict[str, Any],
    ) -> CustomerReportCopy:
        """生成与八维数字锁定的对客文案。

        Args:
            evidence: 已通过证据闸门的客户事实。
            kernel: 不可变诊断内核。
            why_blocks: 已校验的 5.2 资产。
            dimension_narratives: 已批准的 5.2 语言投影。
            action_narrative: 已校验的 5.3 文案。
            knowledge: 仅用于沟通表达的书籍知识片段。
            preferences: 代理人已确认的叙事偏好。

        Returns:
            只包含对客文案字段的结构化结果。
        """

        preferred_name = str(preferences.get("称呼", evidence.customer_name)).strip() or evidence.customer_name
        del why_blocks
        narrative_by_code = {item.dimension_code: item for item in dimension_narratives}
        narratives = []
        for fact in kernel.dimension_facts:
            source = narrative_by_code[fact.dimension_code]
            narratives.append(
                CustomerDimensionNarrative(
                    dimension_code=fact.dimension_code,
                    headline=f"{fact.dimension_name}：{_STATE_TEXT[fact.state]}",
                    calculation_explanation=source.calculation_explanation,
                    why_it_matters=source.why_it_matters,
                    suggestion=_customer_suggestion(fact.state, fact.dimension_name),
                    fact_refs=source.fact_refs,
                )
            )
        return CustomerReportCopy(
            salutation=f"{preferred_name}，你好。",
            opening_paragraphs=("这份报告把你们家的保障现状整理成一张清楚的体检表。", "我们会同时看哪些已经安排得很好，哪些还需要补充，决定权始终在你。"),
            family_summary=f"本次检视以{evidence.customer_name}家庭当前已确认的成员、收入、责任和保单信息为基础。",
            status_summary=_status_summary(kernel),
            dimension_narratives=tuple(narratives),
            roadmap_intro=action_narrative.overview,
            preserve_intro="检视不只是找缺口，已经有效的安排也应该明确保留。",
            closing_paragraphs=("保障规划的价值，是让家庭在面对不确定时仍然有选择。", "下一步可以从第一波方向开始，把数字、节奏和实际想法再一起核对。"),
            knowledge_refs=tuple(f"{item.skill_id}@{item.version}" for item in knowledge),
            generation_mode=GenerationMode.RULE_BOUND,
        )

    async def extract_review_patch(
        self,
        feedback: str,
        current_input: dict[str, Any],
    ) -> ReviewPatchExtraction:
        """提取常见财务字段和叙事偏好。

        Args:
            feedback: 代理人在复核框中输入的自然语言。
            current_input: 当前任务输入，仅用于判断允许字段。

        Returns:
            经白名单约束的候选 Patch 和未解析片段。
        """

        del current_input
        patches: dict[str, Any] = {}
        field_specs = (
            ("spouse_annual_income_wan", r"(?:配偶|老公|妻子|爱人)年收入", "wan"),
            ("annual_income_wan", r"(?:(?:本人|客户)年收入|(?<!配偶)(?<!老公)(?<!妻子)(?<!爱人)年收入)", "wan"),
            ("family_expense_yuan_month", r"(?:家庭)?(?:月支出|每月开支)", "yuan"),
            ("large_loan_wan", r"(?:大额)?(?:贷款|房贷)(?:余额)?", "wan"),
            ("existing_disease_coverage_wan", r"(?:疾病|重疾)(?:保障)?(?:保额|额度)", "wan"),
            ("existing_medical_responsibility_tier", r"(?:商业)?医疗(?:保障)?(?:责任)?层级", "plain"),
            ("existing_disability_coverage_wan", r"伤残(?:保障)?(?:保额|额度)", "wan"),
            ("existing_care_coverage_wan", r"(?:长期)?护理(?:保障)?(?:保额|额度)", "wan"),
            ("existing_death_coverage_wan", r"(?:身故|寿险)(?:保障)?(?:保额|额度)", "wan"),
            ("existing_wealth_reserve_wan", r"(?:长期)?财富储备", "wan"),
            ("existing_retirement_cashflow_yuan_year", r"(?:商业)?养老(?:年度)?现金流", "yuan"),
            ("existing_legacy_reserve_wan", r"传承储备", "wan"),
            ("annual_premium_budget_yuan", r"(?:年度)?保费预算", "yuan"),
        )
        for key, label_pattern, target_unit in field_specs:
            value = _extract_latest_number(feedback, label_pattern, target_unit=target_unit)
            if value is not None:
                patches[key] = value
        trigger_ids = tuple(dict.fromkeys(re.findall(r"\b[A-D]\d+\b", feedback.upper())))
        narrative_preferences: dict[str, Any] = {}
        salutation = re.search(r"称呼(?:改为|用|为|是)?\s*([^，。；,;\s]{1,12})", feedback)
        if salutation is not None:
            narrative_preferences["称呼"] = salutation.group(1)
        density = re.search(r"(?:解释|话术|表达)[^，。；,;]{0,8}(简洁|详细|适中)", feedback)
        if density is not None:
            narrative_preferences["解释密度"] = density.group(1)
        unresolved = () if patches or trigger_ids or narrative_preferences else (feedback,)
        return ReviewPatchExtraction(
            fact_patches=patches,
            trigger_ids=trigger_ids,
            narrative_preferences=narrative_preferences,
            unresolved=unresolved,
        )


class ModelNarrativeHarness:
    """使用 DeerFlow 配置模型生成结构化文案，失败时安全降级。"""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        provider_model: str | None = None,
        fallback: CoverageReviewNarrativeHarness | None = None,
        timeout_seconds: float = 90.0,
    ) -> None:
        """初始化模型语言 Harness。

        Args:
            model_name: 可选的 DeerFlow 模型名称，为空时使用默认模型。
            provider_model: 提供方实际模型标识，用于审计与历史重放。
            fallback: 模型调用或 Schema 校验失败时的保守降级实现。
            timeout_seconds: 单次语言节点调用的超时秒数。
        """

        self._model_name = model_name
        self._provider_model = provider_model or model_name or "default"
        self._fallback = fallback or RuleBoundNarrativeHarness()
        self._timeout_seconds = timeout_seconds

    @property
    def runtime_metadata(self) -> dict[str, Any]:
        """返回模型语言节点的固定运行元数据。

        Returns:
            当前模型、提供方模型、Harness 和提示词版本。
        """

        return {
            "model_name": self._model_name or "default",
            "provider_model": self._provider_model,
            "harness_version": NARRATIVE_HARNESS_VERSION,
            "prompt_versions": {"shared": NARRATIVE_PROMPT_VERSION},
            "structured_output_mode": NARRATIVE_STRUCTURED_OUTPUT_MODE,
        }

    async def analyze_customer(self, evidence: EvidenceBundle, kernel: DiagnosisKernel, preferences: dict[str, Any]) -> CustomerAnalysis:
        """调用受约束模型生成客户分析。"""

        payload = {
            "evidence": _evidence_language_view(evidence),
            "kernel": _kernel_language_view(kernel),
            "preferences": preferences,
        }
        result = await self._invoke_or_fallback(
            CustomerAnalysis,
            task="生成代理人可读的客户速读、核心抓手和自然追问。",
            payload=payload,
            fallback=lambda: self._fallback.analyze_customer(evidence, kernel, preferences),
        )
        result = result.model_copy(
            update={
                "fact_refs": _ordered_evidence_refs(evidence),
                "confirmation_questions": tuple(item.model_copy(update={"fact_refs": ()}) for item in result.confirmation_questions),
            }
        )
        try:
            _assert_no_internal_tokens(result, _kernel_internal_tokens(kernel))
            _assert_fact_refs(result.fact_refs, _evidence_refs(evidence))
        except ValueError as exc:
            logger.warning("客户分析未通过语言边界，使用保守降级文案: %s", exc)
            return _mark_fallback(
                await self._fallback.analyze_customer(
                    evidence,
                    kernel,
                    preferences,
                )
            )
        return result

    async def verbalize_explanations(self, kernel: DiagnosisKernel, why_blocks: tuple[WhyBlock, ...], preferences: dict[str, Any]) -> tuple[DimensionNarrative, ...]:
        """调用受约束模型翻译 5.2 解释资产。"""

        payload = {
            "kernel": _kernel_language_view(kernel),
            "selected_assets": [_why_block_language_view(item) for item in why_blocks],
            "preferences": preferences,
        }
        wrapper = await self._invoke_or_fallback(
            _DimensionNarrativeList,
            task="把已选中的解释资产翻译为代理人可读文案，不重算、不改变结论、不输出理由码或框架码。",
            payload=payload,
            fallback=lambda: _wrap_dimensions(self._fallback.verbalize_explanations(kernel, why_blocks, preferences)),
        )
        wrapper = wrapper.model_copy(update={"items": _canonicalize_dimension_order(wrapper.items)})
        derivation_by_code = {item.dimension_code: _public_derivation(item) for item in why_blocks}
        wrapper = wrapper.model_copy(
            update={
                "items": tuple(
                    item.model_copy(
                        update={
                            "calculation_explanation": derivation_by_code[item.dimension_code],
                        }
                    )
                    for item in wrapper.items
                ),
            }
        )
        refs_by_code = {item.dimension_code: item.fact_refs for item in why_blocks}
        wrapper = wrapper.model_copy(update={"items": tuple(item.model_copy(update={"fact_refs": refs_by_code[item.dimension_code]}) for item in wrapper.items)})
        try:
            _validate_dimension_codes(wrapper.items)
            _assert_no_internal_tokens(
                wrapper,
                (*_kernel_internal_tokens(kernel), *(item.reason_code for item in why_blocks), *(item.framework_code for item in why_blocks)),
            )
            allowed_refs_by_code = {item.dimension_code: set(item.fact_refs) for item in why_blocks}
            for item in wrapper.items:
                _assert_fact_refs(
                    item.fact_refs,
                    allowed_refs_by_code[item.dimension_code],
                )
        except ValueError as exc:
            logger.warning("维度解释未通过语言边界，使用保守降级文案: %s", exc)
            return tuple(
                _mark_fallback(item)
                for item in await self._fallback.verbalize_explanations(
                    kernel,
                    why_blocks,
                    preferences,
                )
            )
        return wrapper.items

    async def verbalize_actions(self, kernel: DiagnosisKernel, action_block: ActionBlock, preferences: dict[str, Any]) -> ActionNarrative:
        """调用受约束模型翻译 5.3 行动资产。"""

        payload = {
            "kernel": _kernel_language_view(kernel),
            "action_block": _action_block_language_view(
                action_block,
                kernel,
            ),
            "preferences": preferences,
        }
        draft = await self._invoke_or_fallback(
            _ActionNarrativeDraft,
            task="按输入 actions 的固定顺序生成等长的 titles、rationales 和 agent_languages；不得输出 action_id。使用输入中的中文展示值，不输出原始单位、具体产品、缴费方案、收益或内部理由码。",
            payload=payload,
            fallback=lambda: _wrap_action_narrative(
                self._fallback.verbalize_actions(
                    kernel,
                    action_block,
                    preferences,
                )
            ),
        )
        ordered_actions = (*action_block.actions, *action_block.maintain_items)
        try:
            expected_count = len(ordered_actions)
            if not all(
                len(items) == expected_count
                for items in (
                    draft.titles,
                    draft.rationales,
                    draft.agent_languages,
                )
            ):
                raise ValueError("行动文案与封闭动作数量不一致")
            result = ActionNarrative(
                overview=draft.overview,
                items=tuple(
                    ActionNarrativeItem(
                        action_id=action.action_id,
                        title=draft.titles[index],
                        rationale=draft.rationales[index],
                        agent_language=draft.agent_languages[index],
                    )
                    for index, action in enumerate(ordered_actions)
                ),
                closing_boundary=action_block.handoff_statement,
                budget_tradeoff=action_block.budget_tradeoff,
                generation_mode=draft.generation_mode,
            )
            _assert_no_internal_tokens(
                result,
                (
                    *_kernel_internal_tokens(kernel),
                    *(
                        item.reason_code
                        for item in (
                            *action_block.actions,
                            *action_block.maintain_items,
                        )
                    ),
                ),
            )
            _assert_no_raw_units(result)
        except ValueError as exc:
            logger.warning("行动文案未通过语言边界，使用保守降级文案: %s", exc)
            return _mark_fallback(
                await self._fallback.verbalize_actions(
                    kernel,
                    action_block,
                    preferences,
                )
            )
        return result

    async def plan_meeting(
        self,
        kernel: DiagnosisKernel,
        analysis: CustomerAnalysis,
        dimension_narratives: tuple[DimensionNarrative, ...],
        action_narrative: ActionNarrative,
        knowledge: tuple[KnowledgeExcerpt, ...],
        preferences: dict[str, Any],
    ) -> MeetingPlan:
        """调用受约束模型生成个性化面谈材料。"""

        payload = {
            "kernel": _kernel_language_view(kernel),
            "analysis": analysis.model_dump(
                mode="json",
                exclude={"fact_refs", "generation_mode"},
            ),
            "dimension_narratives": [
                item.model_dump(
                    mode="json",
                    exclude={"fact_refs", "generation_mode"},
                )
                for item in dimension_narratives
            ],
            "action_narrative": action_narrative.model_dump(
                mode="json",
                exclude={"generation_mode"},
            ),
            "knowledge": [item.model_dump(mode="json") for item in knowledge],
            "preferences": preferences,
        }
        result = await self._invoke_or_fallback(
            MeetingPlan,
            task="生成对当前客户有针对性的面谈目标、话术和异议处理。书籍片段只能用于沟通方法，不得当作客户事实、测算规则或条款依据。",
            payload=payload,
            fallback=lambda: self._fallback.plan_meeting(kernel, analysis, dimension_narratives, action_narrative, knowledge, preferences),
        )
        result = result.model_copy(
            update={
                "scripts": tuple(
                    script.model_copy(
                        update={
                            "fact_refs": _script_fact_refs(
                                script.topic,
                                analysis,
                                dimension_narratives,
                            )
                        }
                    )
                    for script in result.scripts
                )
            }
        )
        try:
            _assert_no_internal_tokens(result, _kernel_internal_tokens(kernel))
            allowed_refs = set(analysis.fact_refs).union(*(set(item.fact_refs) for item in dimension_narratives))
            for script in result.scripts:
                _assert_fact_refs(script.fact_refs, allowed_refs)
        except ValueError as exc:
            logger.warning("面谈计划未通过语言边界，使用保守降级文案: %s", exc)
            return _mark_fallback(
                await self._fallback.plan_meeting(
                    kernel,
                    analysis,
                    dimension_narratives,
                    action_narrative,
                    knowledge,
                    preferences,
                )
            )
        return result

    async def write_customer_copy(
        self,
        evidence: EvidenceBundle,
        kernel: DiagnosisKernel,
        why_blocks: tuple[WhyBlock, ...],
        dimension_narratives: tuple[DimensionNarrative, ...],
        action_narrative: ActionNarrative,
        knowledge: tuple[KnowledgeExcerpt, ...],
        preferences: dict[str, Any],
    ) -> CustomerReportCopy:
        """调用受约束模型生成对客文案字段。"""

        customer_dimensions = _customer_safe_dimension_narratives(
            kernel,
            why_blocks,
            dimension_narratives,
        )
        payload = {
            "customer_name": evidence.customer_name,
            "kernel": _kernel_language_view(kernel),
            "approved_explanation_assets": [_why_block_language_view(item) for item in why_blocks],
            "approved_dimension_narratives": [
                item.model_dump(
                    mode="json",
                    exclude={"fact_refs", "generation_mode"},
                )
                for item in customer_dimensions
            ],
            "action_narrative": action_narrative.model_dump(
                mode="json",
                exclude={"generation_mode"},
            ),
            "communication_knowledge": [item.model_dump(mode="json") for item in knowledge],
            "preferences": preferences,
        }
        result = await self._invoke_or_fallback(
            CustomerReportCopy,
            task=(
                "生成客户可见报告的受控文案字段。必须逐字保留 "
                "approved_dimension_narratives 中的 calculation_explanation，"
                "只优化客户可读表达；书籍片段仅用于沟通方法。语气温和、"
                "克制、可验证，明确肯定保留项，不输出理由码、框架码、"
                "内核字段、原始单位、具体产品或收益承诺。"
            ),
            payload=payload,
            fallback=lambda: self._fallback.write_customer_copy(
                evidence,
                kernel,
                why_blocks,
                customer_dimensions,
                action_narrative,
                knowledge,
                preferences,
            ),
        )
        result = result.model_copy(update={"dimension_narratives": _canonicalize_dimension_order(result.dimension_narratives)})
        if _contains_customer_boundary(result):
            fallback_copy = await self._fallback.write_customer_copy(
                evidence,
                kernel,
                why_blocks,
                customer_dimensions,
                action_narrative,
                knowledge,
                preferences,
            )
            result = _repair_customer_copy_boundaries(result, fallback_copy)
            logger.warning("对客文案命中禁用表达，已按字段替换为规则受控表达")
        refs_by_code = {item.dimension_code: item.fact_refs for item in why_blocks}
        result = result.model_copy(update={"dimension_narratives": tuple(item.model_copy(update={"fact_refs": refs_by_code[item.dimension_code]}) for item in result.dimension_narratives)})
        try:
            _validate_dimension_codes(result.dimension_narratives)
            _assert_no_internal_tokens(
                result,
                (*_kernel_internal_tokens(kernel), *(item.reason_code for item in why_blocks), *(item.framework_code for item in why_blocks)),
            )
            refs_by_code = {item.dimension_code: set(item.fact_refs) for item in why_blocks}
            approved_by_code = {item.dimension_code: item for item in customer_dimensions}
            for item in result.dimension_narratives:
                _assert_fact_refs(item.fact_refs, refs_by_code[item.dimension_code])
                if item.calculation_explanation != approved_by_code[item.dimension_code].calculation_explanation:
                    raise ValueError(f"customer copy changed approved derivation: {item.dimension_code.value}")
            _assert_no_raw_units(result)
        except ValueError as exc:
            logger.warning("对客文案未通过语言边界，使用保守降级文案: %s", exc)
            fallback = await self._fallback.write_customer_copy(
                evidence,
                kernel,
                why_blocks,
                customer_dimensions,
                action_narrative,
                knowledge,
                preferences,
            )
            return fallback.model_copy(update={"generation_mode": GenerationMode.FALLBACK})
        return result.model_copy(update={"knowledge_refs": tuple(f"{item.skill_id}@{item.version}" for item in knowledge)})

    async def extract_review_patch(
        self,
        feedback: str,
        current_input: dict[str, Any],
    ) -> ReviewPatchExtraction:
        """使用受约束模型提取代理人复核候选 Patch。

        Args:
            feedback: 代理人自然语言复核意见。
            current_input: 当前任务的结构化输入。

        Returns:
            仅包含白名单补录字段、触发编号和叙事偏好的候选 Patch。
        """

        allowed_answers = (
            "annual_income_wan",
            "spouse_annual_income_wan",
            "family_expense_yuan_month",
            "large_loan_wan",
            "existing_disease_coverage_wan",
            "existing_medical_responsibility_tier",
            "existing_disability_coverage_wan",
            "existing_care_coverage_wan",
            "existing_death_coverage_wan",
            "existing_wealth_reserve_wan",
            "existing_retirement_cashflow_yuan_year",
            "existing_legacy_reserve_wan",
            "annual_premium_budget_yuan",
            "investment_risk_tolerance",
            "review_scope",
        )
        payload = {
            "feedback": feedback,
            "allowed_fact_patch_keys": allowed_answers,
            "allowed_narrative_preference_keys": ("称呼", "解释密度"),
            "current_answers": {key: value for key, value in dict(current_input.get("answers", {})).items() if key in allowed_answers},
            "current_trigger_ids": current_input.get("trigger_ids", []),
        }
        result = await self._invoke_or_fallback(
            ReviewPatchExtraction,
            task="将代理人复核意见提取为候选变更。fact_patches 只能使用 allowed_fact_patch_keys；未能明确映射的内容必须放入 unresolved，不得猜测。",
            payload=payload,
            fallback=lambda: self._fallback.extract_review_patch(feedback, current_input),
        )
        invalid = set(result.fact_patches) - set(allowed_answers)
        invalid_preferences = set(result.narrative_preferences) - {"称呼", "解释密度"}
        if invalid or invalid_preferences:
            logger.warning(
                "复核意见提取超出白名单，使用机械提取结果: fields=%s preferences=%s",
                sorted(invalid),
                sorted(invalid_preferences),
            )
            return await self._fallback.extract_review_patch(feedback, current_input)
        return result

    async def _invoke_or_fallback[SchemaT: BaseModel](
        self,
        schema: type[SchemaT],
        *,
        task: str,
        payload: dict[str, Any],
        fallback: Any,
    ) -> SchemaT:
        """调用结构化模型，异常时返回同 Schema 降级结果。

        Args:
            schema: 模型必须满足的 Pydantic 输出类型。
            task: 当前语言节点的窄任务描述。
            payload: 仅含允许读取的结构化资产。
            fallback: 返回同 Schema 结果的异步回调。

        Returns:
            通过 Schema 校验的语言资产。
        """

        try:
            model = create_chat_model(name=self._model_name, thinking_enabled=True) if self._model_name else create_chat_model(thinking_enabled=True)
            # DashScope 的 MiniMax-M2.5 不支持原生 Structured Output，并且当前
            # 部署强制思考模式。思考模式只允许 auto/none，因此绑定唯一工具后
            # 由本地代码校验工具名、调用数量和 Pydantic 参数，拒绝自由文本。
            structured = model.bind_tools([schema], tool_choice="auto")
            payload_hash = sha256_digest(payload)
            messages = [
                SystemMessage(content=_system_instruction(task)),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
            ]
            invocation_config = {
                "run_name": f"coverage_review_{schema.__name__}",
                "tags": [
                    "coverage-review",
                    f"model:{self._model_name or 'default'}",
                    f"prompt:{NARRATIVE_PROMPT_VERSION}",
                ],
                "metadata": {
                    "coverage_review_model": self._model_name or "default",
                    "coverage_review_provider_model": self._provider_model,
                    "coverage_review_harness_version": NARRATIVE_HARNESS_VERSION,
                    "coverage_review_prompt_version": NARRATIVE_PROMPT_VERSION,
                    "coverage_review_schema": schema.__name__,
                    "coverage_review_input_hash": payload_hash,
                },
            }
            async with asyncio.timeout(self._timeout_seconds):
                response = await structured.ainvoke(messages, config=invocation_config)
                try:
                    return _validated_tool_result(schema, response.tool_calls)
                except ValueError as exc:
                    logger.warning(
                        "保障检视语言节点首次结构化输出不合规，自动纠偏: schema=%s error=%s",
                        schema.__name__,
                        exc,
                    )
                response = await structured.ainvoke(
                    [
                        *messages,
                        response,
                        HumanMessage(content=(f"上一次没有正确调用 {schema.__name__}。现在禁止直接回答，只调用当前唯一工具一次，并完整填写所有必填字段。")),
                    ],
                    config=invocation_config,
                )
                return _validated_tool_result(schema, response.tool_calls)
        except TimeoutError:
            logger.warning(
                "保障检视语言节点超过 %.1f 秒，转入受约束降级文案: %s",
                self._timeout_seconds,
                schema.__name__,
            )
            return _mark_fallback(await fallback())
        except Exception:
            logger.exception("保障检视语言节点失败，转入受约束降级文案: %s", schema.__name__)
            return _mark_fallback(await fallback())


def _validated_tool_result[SchemaT: BaseModel](
    schema: type[SchemaT],
    tool_calls: Sequence[dict[str, Any]],
) -> SchemaT:
    """校验唯一工具调用并返回已标记的模型结果。

    Args:
        schema: 当前语言节点的 Pydantic 输出类型。
        tool_calls: 模型响应中的结构化工具调用。

    Returns:
        通过工具名、数量和 Schema 校验的模型结果。

    Raises:
        ValueError: 工具调用数量、名称或参数不符合契约时抛出。
    """

    if len(tool_calls) != 1:
        raise ValueError(f"expected exactly one {schema.__name__} tool call, got {len(tool_calls)}")
    tool_call = tool_calls[0]
    if tool_call.get("name") != schema.__name__:
        raise ValueError(f"unexpected structured output tool: {tool_call.get('name')}")
    return _mark_model(schema.model_validate(tool_call.get("args")))


class _DimensionNarrativeList(BaseModel):
    """封装维度文案列表，用于结构化模型输出。"""

    items: tuple[DimensionNarrative, ...]


class _ActionNarrativeDraft(BaseModel):
    """只允许模型填写的 5.3 语言槽位。"""

    overview: str
    titles: tuple[str, ...]
    rationales: tuple[str, ...]
    agent_languages: tuple[str, ...]
    generation_mode: GenerationMode = GenerationMode.MODEL


async def _wrap_dimensions(value: Any) -> _DimensionNarrativeList:
    """将降级维度文案包装为模型输出 Schema。"""

    return _DimensionNarrativeList(items=await value)


async def _wrap_action_narrative(value: Any) -> _ActionNarrativeDraft:
    """把规则行动文案转换为模型最小输出 Schema。"""

    narrative = await value
    return _ActionNarrativeDraft(
        overview=narrative.overview,
        titles=tuple(item.title for item in narrative.items),
        rationales=tuple(item.rationale for item in narrative.items),
        agent_languages=tuple(item.agent_language for item in narrative.items),
        generation_mode=narrative.generation_mode,
    )


def _system_instruction(task: str) -> str:
    """组装所有语言节点共用的硬边界。"""

    return (
        "你是保障检视报告的受约束语言节点。"
        f"当前任务：{task}"
        "必须调用当前绑定的唯一结构化输出工具返回结果，禁止直接输出文本。"
        "只能使用输入 JSON 中的事实、数字、结论和允许表达；不得计算新数字，不得新增保障结论，不得更改维度状态和行动次序。"
        "不得输出 reason_code、framework_code、kernel_hash、rule_bundle_hash 或其他开发字段。"
        "所有数值必须使用输入提供的中文展示值，不得输出 CNY、CNY_PER_YEAR、responsibility_tier 等原始单位。"
        "医疗保障必须使用尚未配置商业医疗保障、基础住院医疗责任、基础及扩展医疗责任等业务表达，禁止使用层责任或责任层等内部术语。"
        "不得承诺收益、伪造条款、制造焦虑或给出未经人工复核的退换保结论。"
    )


def _kernel_language_view(kernel: DiagnosisKernel) -> dict[str, Any]:
    """为语言节点生成不含内部码和原始单位的最小内核视图。

    Args:
        kernel: 当前不可变诊断内核。

    Returns:
        仅含客户可理解事实、次序、触发和边界的字典。
    """

    return {
        "dimensions": [
            {
                "dimension_code": item.dimension_code.value,
                "dimension_name": item.dimension_name,
                "state": item.state.value,
                "existing": format_value(item.existing_value, item.unit, role="existing"),
                "target": format_value(item.ideal_value, item.unit, role="target"),
                "gap": format_value(item.gap_value, item.unit, role="gap"),
                "priority_rank": item.priority_rank,
                "confidence": item.confidence.value,
                "requires_agent_review": item.requires_agent_review,
            }
            for item in kernel.dimension_facts
        ],
        "preserve_items": [
            {
                "dimension_code": (item.dimension_code.value if item.dimension_code is not None else None),
                "object": item.object_ref,
            }
            for item in kernel.preserve_items
        ],
        "correction_items": [
            {
                "dimensions": [code.value for code in item.dimension_codes],
                "requires_agent_review": True,
            }
            for item in kernel.correction_items
        ],
        "priority_order": [item.dimension_name for code in kernel.priority_order for item in kernel.dimension_facts if item.dimension_code is code],
        "trigger": kernel.trigger_binding.model_dump(mode="json"),
        "customer_route": kernel.customer_route.route.value,
        "assumptions": [_assumption_public_text(item.field_key, item.source) for item in kernel.assumptions],
        "conflicts": list(kernel.conflicts),
        "policy_inventory_confirmed": kernel.policy_inventory_confirmed,
    }


def _evidence_language_view(evidence: EvidenceBundle) -> dict[str, Any]:
    """生成不含审计标识的客户分析最小证据视图。

    Args:
        evidence: 已通过证据闸门的客户事实。

    Returns:
        只包含模型生成业务表达所需事实的字典。
    """

    return {
        "customer_name": evidence.customer_name,
        "as_of_date": evidence.as_of_date.isoformat(),
        "fields": [
            {
                "field_key": item.field_key,
                "value": item.value,
                "verification_status": item.verification_status.value,
            }
            for item in evidence.fields
        ],
        "policy_facts": [
            {
                "category": item.category,
                "value": item.value,
                "unit": item.unit,
            }
            for item in evidence.policy_report.policy_facts
        ],
        "assumptions": list(evidence.assumptions),
        "conflicts": list(evidence.conflicts),
        "enhancement_questions": [
            {
                "item": item.field_key,
                "prompt": item.prompt,
                "reason": item.reason,
            }
            for item in evidence.enhancement_questions
        ],
    }


def _why_block_language_view(block: WhyBlock) -> dict[str, Any]:
    """把 5.2 资产投影为不暴露理由码和框架码的语言输入。

    Args:
        block: 已由确定性选择器选中的 5.2 资产。

    Returns:
        模型只可翻译的推导链、叙事骨架和利益语言。
    """

    return {
        "dimension_code": block.dimension_code.value,
        "derivation_chain": list(block.derivation_chain),
        "narrative_skeleton": list(block.narrative_skeleton),
        "benefit_language": [
            item
            for item in block.benefit_language
            if not any(
                token in item
                for token in (
                    "不得",
                    "禁止",
                    *_CUSTOMER_VISIBLE_BOUNDARY_PATTERNS,
                )
            )
        ],
        "confidence": block.confidence.value,
    }


def _action_block_language_view(
    action_block: ActionBlock,
    kernel: DiagnosisKernel,
) -> dict[str, Any]:
    """把 5.3 行动块转换为客户可理解的封闭语言输入。

    Args:
        action_block: 已通过边界校验的行动集合。
        kernel: 用于读取维度名和格式化缺口值的诊断内核。

    Returns:
        不含理由码和原始单位的行动输入。
    """

    facts = {item.dimension_code: item for item in kernel.dimension_facts}

    def item_view(item: Any) -> dict[str, Any]:
        """转换一个行动项。

        Args:
            item: ActionItem 实例。

        Returns:
            绑定 action_id 的客户可读行动事实。
        """

        fact = facts.get(item.dimension_code)
        return {
            "action_id": item.action_id,
            "wave": item.wave.value,
            "dimension": (fact.dimension_name if fact is not None else "家庭整体结构"),
            "action": _ACTION_TEXT[item.action_type],
            "gap": (format_value(fact.gap_value, fact.unit, role="gap") if fact is not None else "按家庭整体结构"),
            "sequence_basis": [_sequence_reason_text(code) for code in item.sequence_reason_codes],
            "requires_human_review": item.requires_human_review,
        }

    return {
        "actions": [item_view(item) for item in (*action_block.actions, *action_block.maintain_items)],
        "budget_tradeoff": action_block.budget_tradeoff,
        "handoff_statement": action_block.handoff_statement,
    }


def _validate_dimension_codes(items: Sequence[DimensionNarrative | CustomerDimensionNarrative]) -> None:
    """校验语言输出与八维固定顺序一致。"""

    codes = tuple(item.dimension_code for item in items)
    if codes != DIMENSION_ORDER:
        raise ValueError("narrative output must contain the eight canonical dimensions in fixed order")


def _canonicalize_dimension_order(
    items: Sequence[DimensionNarrative | CustomerDimensionNarrative],
) -> tuple[DimensionNarrative | CustomerDimensionNarrative, ...]:
    """在维度完整且不重复时按规范顺序重排模型文案。

    模型可以根据业务优先级调整输出顺序，但 HTML 的八维结构由代码 Harness
    所有。这里只修正顺序；缺维或重复仍交给后续语言边界阻断。

    Args:
        items: 模型返回的代理人或客户维度文案。

    Returns:
        维度集合完整时返回规范顺序，否则原样返回以触发严格校验。
    """

    by_code = {item.dimension_code: item for item in items}
    if len(items) != len(DIMENSION_ORDER) or set(by_code) != set(DIMENSION_ORDER):
        return tuple(items)
    return tuple(by_code[code] for code in DIMENSION_ORDER)


def _assert_no_internal_tokens(value: BaseModel, extra_tokens: Sequence[str] = ()) -> None:
    """防止模型把开发字段带入代理人或客户文案。"""

    text = _visible_narrative_text(value)
    forbidden = (
        "reason_code",
        "framework_code",
        "kernel_hash",
        "rule_bundle",
        "INCOME_REPLACE_GAP",
        "MEDICAL_LIABILITY_GAP",
        "DISABILITY_GAP",
        "层责任",
        "责任层",
        *extra_tokens,
    )
    hit = next((item for item in forbidden if item in text), None)
    if hit is not None:
        raise ValueError(f"customer narrative contains internal token: {hit}")


def _visible_narrative_text(value: BaseModel) -> str:
    """提取实际可展示文案并排除代码绑定的审计元数据。

    Args:
        value: 待执行语言边界校验的结构化资产。

    Returns:
        可供内部码和原始单位扫描的展示文本。
    """

    hidden_fields = {
        "action_id",
        "dimension_code",
        "fact_refs",
        "generation_mode",
        "knowledge_refs",
    }
    parts: list[str] = []

    def collect(item: Any, field_name: str | None = None) -> None:
        """递归收集展示字段中的字符串。

        Args:
            item: 当前待遍历的 JSON 兼容值。
            field_name: 当前值对应的字段名。
        """

        if field_name in hidden_fields:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                collect(child, key)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                collect(child)
            return
        if isinstance(item, str):
            parts.append(item)

    collect(value.model_dump(mode="json"))
    return "\n".join(parts)


def _contains_customer_boundary(value: BaseModel | str) -> bool:
    """判断客户可见表达是否命中规则包边界词。

    Args:
        value: 结构化语言资产或单个展示字段。

    Returns:
        命中任一客户禁用表达时返回 ``True``。
    """

    text = value if isinstance(value, str) else _visible_narrative_text(value)
    return any(pattern in text for pattern in _CUSTOMER_VISIBLE_BOUNDARY_PATTERNS)


def _customer_safe_dimension_narratives(
    kernel: DiagnosisKernel,
    why_blocks: tuple[WhyBlock, ...],
    dimensions: tuple[DimensionNarrative, ...],
) -> tuple[DimensionNarrative, ...]:
    """把代理人解释资产投影为可供对客模型引用的安全版本。

    Args:
        kernel: 已固化的诊断内核。
        why_blocks: 与八维一一对应的 5.2 配置资产。
        dimensions: 代理人复核过的八维解释文案。

    Returns:
        保留安全模型表达，并以规则资产替换守则类客户禁用表达的八维文案。
    """

    facts = {item.dimension_code: item for item in kernel.dimension_facts}
    blocks = {item.dimension_code: item for item in why_blocks}
    projected: list[DimensionNarrative] = []
    for item in dimensions:
        fact = facts[item.dimension_code]
        block = blocks[item.dimension_code]
        fallback_heading = f"{fact.dimension_name}：{_STATE_TEXT[fact.state]}"
        fallback_calculation = _public_derivation(block)
        fallback_significance = _dimension_significance(
            fact.dimension_name,
            block,
            fact.existing_value,
            fact.ideal_value,
            fact.unit,
        )
        fallback_guidance = _agent_guidance(fact.state, fact.dimension_name)
        projected.append(
            item.model_copy(
                update={
                    "heading": (fallback_heading if _contains_customer_boundary(item.heading) else item.heading),
                    "calculation_explanation": fallback_calculation,
                    "why_it_matters": (fallback_significance if _contains_customer_boundary(item.why_it_matters) else item.why_it_matters),
                    "agent_guidance": (fallback_guidance if _contains_customer_boundary(item.agent_guidance) else item.agent_guidance),
                }
            )
        )
    return tuple(projected)


def _repair_customer_copy_boundaries(
    generated: CustomerReportCopy,
    fallback: CustomerReportCopy,
) -> CustomerReportCopy:
    """只替换模型输出中命中客户边界词的文案字段。

    Args:
        generated: 已通过 Schema 的模型对客文案。
        fallback: 与同一内核绑定的规则受控文案。

    Returns:
        保留安全模型表达、替换越界字段并标记为混合生成的文案。
    """

    fallback_by_code = {item.dimension_code: item for item in fallback.dimension_narratives}

    def safe_text(value: str, fallback_value: str) -> str:
        """命中边界词时使用同字段规则表达。

        Args:
            value: 模型生成字段。
            fallback_value: 同字段规则受控表达。

        Returns:
            可进入客户报告的字段内容。
        """

        return fallback_value if _contains_customer_boundary(value) else value

    def safe_paragraphs(
        value: tuple[str, ...],
        fallback_value: tuple[str, ...],
    ) -> tuple[str, ...]:
        """整组替换包含边界词的段落。

        Args:
            value: 模型生成段落组。
            fallback_value: 规则受控段落组。

        Returns:
            可进入客户报告的段落组。
        """

        return fallback_value if any(_contains_customer_boundary(item) for item in value) else value

    dimensions = tuple(
        item.model_copy(
            update={
                "headline": safe_text(
                    item.headline,
                    fallback_by_code[item.dimension_code].headline,
                ),
                "calculation_explanation": safe_paragraphs(
                    item.calculation_explanation,
                    fallback_by_code[item.dimension_code].calculation_explanation,
                ),
                "why_it_matters": safe_text(
                    item.why_it_matters,
                    fallback_by_code[item.dimension_code].why_it_matters,
                ),
                "suggestion": safe_text(
                    item.suggestion,
                    fallback_by_code[item.dimension_code].suggestion,
                ),
            }
        )
        for item in generated.dimension_narratives
    )
    return generated.model_copy(
        update={
            "salutation": safe_text(generated.salutation, fallback.salutation),
            "opening_paragraphs": safe_paragraphs(
                generated.opening_paragraphs,
                fallback.opening_paragraphs,
            ),
            "family_summary": safe_text(
                generated.family_summary,
                fallback.family_summary,
            ),
            "status_summary": safe_text(
                generated.status_summary,
                fallback.status_summary,
            ),
            "dimension_narratives": dimensions,
            "roadmap_intro": safe_text(
                generated.roadmap_intro,
                fallback.roadmap_intro,
            ),
            "preserve_intro": safe_text(
                generated.preserve_intro,
                fallback.preserve_intro,
            ),
            "closing_paragraphs": safe_paragraphs(
                generated.closing_paragraphs,
                fallback.closing_paragraphs,
            ),
            "generation_mode": GenerationMode.HYBRID,
        }
    )


def _kernel_internal_tokens(kernel: DiagnosisKernel) -> tuple[str, ...]:
    """收集当前内核中不得出现在自然语言里的内部判断码。"""

    return tuple(
        dict.fromkeys(
            (
                *(code for item in kernel.dimension_facts for code in (*item.reason_codes, *item.urgency_reason_codes)),
                *(item.reason_code for item in kernel.preserve_items),
                *(item.reason_code for item in kernel.correction_items),
                *kernel.customer_route.basis_codes,
            )
        )
    )


def _evidence_refs(evidence: EvidenceBundle) -> set[str]:
    """收集客户分析允许引用的证据标识。"""

    return set(_ordered_evidence_refs(evidence))


def _ordered_evidence_refs(evidence: EvidenceBundle) -> tuple[str, ...]:
    """按证据出现顺序收集唯一审计引用。

    Args:
        evidence: 已通过证据闸门的客户事实。

    Returns:
        去重且顺序稳定的来源引用。
    """

    return tuple(
        dict.fromkeys(
            (
                *(item.source_ref for item in evidence.fields if item.source_ref),
                *(item.fact_id for item in evidence.policy_report.policy_facts if item.fact_id),
            )
        )
    )


def _script_fact_refs(
    topic: str,
    analysis: CustomerAnalysis,
    dimension_narratives: tuple[DimensionNarrative, ...],
) -> tuple[str, ...]:
    """由代码为面谈话术绑定已批准的事实引用。

    Args:
        topic: 模型生成的话术主题。
        analysis: 已批准的客户分析。
        dimension_narratives: 已批准的八维解释。

    Returns:
        对应维度引用；无法定位维度时回退到客户分析引用。
    """

    for item in dimension_narratives:
        dimension_name = item.heading.split("：", maxsplit=1)[0]
        if dimension_name and dimension_name in topic:
            return item.fact_refs
    return analysis.fact_refs


def _assert_fact_refs(actual: Sequence[str], allowed: set[str]) -> None:
    """校验模型没有编造证据引用。"""

    invented = set(actual) - allowed
    if invented:
        raise ValueError(f"narrative invented fact refs: {sorted(invented)}")


def _agent_guidance(state: DimensionState, dimension_name: str) -> str:
    """根据固定状态生成代理人表达边界。"""

    if state is DimensionState.SUFFICIENT:
        return f"{dimension_name}已经形成有效基础，这一项应该明确肯定并建议维持，不重复增加。"
    if state is DimensionState.NO_NEED:
        return f"{dimension_name}当前不在行动清单中，不需要为了齐全而购买。"
    if state is DimensionState.UNKNOWN:
        return f"{dimension_name}的信息还不足，先请客户确认事实，不提前下结论。"
    return f"可以用现有、目标和缺口三个数字说清{dimension_name}的影响，再让客户决定是否进入方案设计。"


def _dimension_significance(
    dimension_name: str,
    block: WhyBlock,
    existing_value: Any,
    ideal_value: Any,
    unit: str,
) -> str:
    """把 5.2 骨架、利益语言和锁定数字合成为业务解释。

    Args:
        dimension_name: 当前八维名称。
        block: 已选中的 5.2 规则资产。
        existing_value: 诊断内核锁定的现有值。
        ideal_value: 诊断内核锁定的目标值。
        unit: 当前维度的标准单位。

    Returns:
        不新增事实、可供代理人和对客文案复用的意义说明。
    """

    public_benefits = tuple(
        item
        for item in block.benefit_language
        if not any(
            token in item
            for token in (
                "不得",
                "禁止",
                "收益率",
                "产品代码",
                "缴费期",
            )
        )
    )
    benefit = "".join(item if item.endswith(("。", "！", "？")) else f"{item}。" for item in public_benefits)
    existing = format_value(existing_value, unit, role="existing")
    ideal = format_value(ideal_value, unit, role="target")
    return f"{dimension_name}当前为{existing}，测算目标为{ideal}。{benefit}"


def _action_language(
    dimension_name: str,
    action_text: str,
    sequence_reason_codes: Sequence[str],
) -> str:
    """把封闭动作及排序依据转换为代理人可用表达。

    Args:
        dimension_name: 动作关联的维度名称。
        action_text: 六类动作的业务显示名称。
        sequence_reason_codes: 只用于确定表达重点的排序理由码。

    Returns:
        不泄漏内部码的行动表达。
    """

    basis = "、".join(dict.fromkeys(_sequence_reason_text(code) for code in sequence_reason_codes))
    if action_text == _ACTION_TEXT[ActionType.MAINTAIN]:
        return f"{dimension_name}已有安排应明确肯定并继续保留，本次不因追求项目齐全而重复增加。"
    suffix = f"，排序主要考虑{basis}" if basis else ""
    return f"建议先与客户确认{dimension_name}方向是否进入下一步方案设计{suffix}；本阶段不讨论具体产品和缴费方案。"


def _sequence_reason_text(code: str) -> str:
    """把行动排序理由码转换为业务表达。

    Args:
        code: 5.3 行动项中的排序理由码。

    Returns:
        不暴露开发字段的中文排序依据。
    """

    return {
        "SEVERITY_HIGH": "缺口程度",
        "SEVERITY_MEDIUM": "缺口程度",
        "EXPOSURE_FAMILY": "家庭责任影响",
        "EXPOSURE_MAJOR": "主要风险影响",
        "TRIGGER_MATCH": "本次检视场景",
        "TOOL_PRIORITY_HINT": "精确测算提示",
        "CORRECTION_BEFORE_GAP": "先核对现有结构",
        "PRESERVE_EXPLICIT": "已有安排的保留价值",
    }.get(code, "已确认的家庭责任与缺口次序")


def _assumption_public_text(field_key: str, source: str) -> str:
    """把内核假设转换为不含配置键的模型输入。

    Args:
        field_key: 内核假设的规范字段名。
        source: 假设来源说明。

    Returns:
        客户可理解的假设说明。
    """

    labels = {
        "investment_risk_tolerance": "投资风险偏好",
        "review_scope": "本次检视范围",
    }
    label = labels.get(field_key, "本次测算参数")
    value_text = source.removeprefix(field_key).strip()
    return f"{label}{value_text}" if value_text else f"{label}采用已批准默认口径"


def _assert_no_raw_units(value: BaseModel) -> None:
    """校验自然语言资产没有泄漏内部单位。

    Args:
        value: 待检查的结构化语言资产。

    Raises:
        ValueError: 文案包含原始工具单位时抛出。
    """

    text = _visible_narrative_text(value)
    forbidden = (
        "CNY_PER_YEAR",
        "responsibility_tier",
        "CNY",
    )
    hit = next((item for item in forbidden if item in text), None)
    if hit is not None:
        raise ValueError(f"narrative contains raw unit: {hit}")


def _mark_fallback[FallbackT: BaseModel](value: FallbackT) -> FallbackT:
    """把模型降级结果标记为可被工作流识别的 fallback。

    Args:
        value: 规则降级返回的任意结构化语言资产。

    Returns:
        保持原 Schema、但显式标记生成方式的副本。
    """

    if isinstance(value, _DimensionNarrativeList):
        return value.model_copy(update={"items": tuple(item.model_copy(update={"generation_mode": GenerationMode.FALLBACK}) for item in value.items)})
    if "generation_mode" in type(value).model_fields:
        return value.model_copy(update={"generation_mode": GenerationMode.FALLBACK})
    return value


def _mark_model[ModelT: BaseModel](value: ModelT) -> ModelT:
    """把成功的模型结果标记为模型生成，禁止模型自行选择生成模式。

    Args:
        value: 已通过结构化 Schema 校验的模型结果。

    Returns:
        保持原 Schema、但由 Harness 固化生成方式的副本。
    """

    if isinstance(value, _DimensionNarrativeList):
        return value.model_copy(update={"items": tuple(item.model_copy(update={"generation_mode": GenerationMode.MODEL}) for item in value.items)})
    if "generation_mode" in type(value).model_fields:
        return value.model_copy(update={"generation_mode": GenerationMode.MODEL})
    return value


def _public_derivation(block: WhyBlock) -> tuple[str, ...]:
    """从 5.2 推导链中移除仅供审计的框架标识。

    Args:
        block: 规则引擎选中的 5.2 解释资产。

    Returns:
        只保留数字、状态和业务含义的推导步骤。
    """

    return tuple(item for item in block.derivation_chain if block.framework_code not in item)


def _preference_note(preferences: dict[str, Any]) -> str:
    """把代理人叙事偏好转换为业务表达，不打印原始配置键。

    Args:
        preferences: 代理人在复核闸门确认的称呼、解释密度或自然语言偏好。

    Returns:
        可直接用于对内沟通判断的补充说明。
    """

    density = str(preferences.get("解释密度", "")).strip()
    feedback = str(preferences.get("agent_feedback", "")).strip()
    parts = []
    if density:
        parts.append(f"代理人要求本轮采用{density}的解释节奏。")
    if feedback:
        parts.append(f"本轮沟通需同时遵循代理人已确认的表达方向：{feedback}")
    return "".join(parts)


def _extract_latest_number(
    text: str,
    label_pattern: str,
    *,
    target_unit: str,
) -> int | float | None:
    """提取字段语句中的最后一个数值，支持“不是旧值，是新值”。

    Args:
        text: 代理人的完整复核意见。
        label_pattern: 当前白名单字段的标签正则。
        target_unit: 规范目标单位，支持 ``wan`` 或 ``yuan``。

    Returns:
        已换算到目标单位的数值；字段未出现或没有明确数字时返回 ``None``。
    """

    label = re.search(label_pattern, text)
    if label is None:
        return None
    suffix = text[label.end() :]
    next_field = re.search(
        r"(?:配偶|老公|妻子|爱人|本人|客户)年收入|(?:家庭)?(?:月支出|每月开支)|(?:大额)?(?:贷款|房贷)(?:余额)?|[。；;\n]",
        suffix,
    )
    segment = suffix[: next_field.start()] if next_field is not None else suffix[:50]
    values = re.findall(r"(\d+(?:\.\d+)?)\s*(万元|万|元)?", segment)
    if not values:
        return None
    raw_value, source_unit = values[-1]
    value = float(raw_value)
    if target_unit == "yuan" and source_unit in {"万", "万元"}:
        value *= 10000
    elif target_unit == "wan" and source_unit == "元":
        value /= 10000
    return int(value) if value.is_integer() else value


def _customer_suggestion(state: DimensionState, dimension_name: str) -> str:
    """根据固定状态生成对客建议。"""

    if state is DimensionState.SUFFICIENT:
        return f"{dimension_name}已经安排得不错，建议继续保留，不需要重复增加。"
    if state is DimensionState.NO_NEED:
        return f"按当前人生阶段，{dimension_name}暂时不需要增加。"
    if state is DimensionState.UNKNOWN:
        return f"{dimension_name}需要先补充信息，本次不仓促下结论。"
    return f"建议把{dimension_name}放入后续保障方向的讨论清单，具体安排在方案设计时确定。"


def _action_rationale(action: Any, fact: Any) -> str:
    """使用内核中锁定的数字生成行动依据。"""

    if fact is None:
        return "该动作来自已确认的家庭整体结构。"
    if action.action_type is ActionType.MAINTAIN:
        return f"{fact.dimension_name}已有安排仍有价值，建议保留有效部分，不重复配置。"
    return f"{fact.dimension_name}当前缺口为 {format_value(fact.gap_value, fact.unit, role='gap')}，结合家庭责任与缺口程度列入当前顺位。"


def _status_summary(kernel: DiagnosisKernel) -> str:
    """从八维状态生成对客一句话现状。"""

    gaps = [item.dimension_name for item in kernel.dimension_facts if item.state in {DimensionState.SEVERE_GAP, DimensionState.SIGNIFICANT_GAP, DimensionState.MILD_GAP}]
    sufficient = [item.dimension_name for item in kernel.dimension_facts if item.state is DimensionState.SUFFICIENT]
    gap_text = "、".join(gaps[:3]) or "当前没有明显缺口"
    sufficient_text = "、".join(sufficient[:2]) or "已有安排"
    return f"当前需要优先关注{gap_text}，同时{sufficient_text}中已经有效的部分应继续保留。"
