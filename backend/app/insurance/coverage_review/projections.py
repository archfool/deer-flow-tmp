"""诊断内核的 3.1、5.2 与 5.3 独立投影。"""

from __future__ import annotations

from decimal import Decimal

from app.insurance.coverage_review.display import format_value
from app.insurance.coverage_review.models import (
    ActionBlock,
    ActionItem,
    ActionType,
    ActionWave,
    AuxiliaryInsight,
    DerivationComponent,
    DiagnosisKernel,
    DimensionCode,
    DimensionFact,
    DimensionState,
    WhyBlock,
)
from app.insurance.coverage_review.rules import RuleBundle


def project_step_3_1(kernel: DiagnosisKernel) -> dict[str, object]:
    """把内核投影为 3.1 的结构化事实视图。

    Args:
        kernel: 不可变诊断内核。

    Returns:
        不含自然语言叙事的 3.1 事实字典。
    """

    return {
        "kernel_hash": kernel.kernel_hash,
        "dimension_facts": [item.model_dump(mode="json") for item in kernel.dimension_facts],
        "preserve_items": [item.model_dump(mode="json") for item in kernel.preserve_items],
        "correction_items": [item.model_dump(mode="json") for item in kernel.correction_items],
        "priority_order": [item.value for item in kernel.priority_order],
        "customer_route": kernel.customer_route.model_dump(mode="json"),
        "trigger_binding": kernel.trigger_binding.model_dump(mode="json"),
    }


def project_step_5_2(
    kernel: DiagnosisKernel,
    bundle: RuleBundle,
) -> tuple[tuple[WhyBlock, ...], tuple[AuxiliaryInsight, ...]]:
    """从内核独立生成 5.2 解释资产。

    Args:
        kernel: 不可变诊断内核。
        bundle: 当前规则包。

    Returns:
        八维解释块和独立辅助财务说明。
    """

    blocks: list[WhyBlock] = []
    for fact in kernel.dimension_facts:
        reason_code = fact.reason_codes[0]
        reason = bundle.reason(reason_code)
        skeleton = _narrative_skeleton(
            category=reason.category,
            temperature=kernel.trigger_binding.temperature.value,
            bundle=bundle,
        )
        derivation = _derivation_chain(fact)
        framework_guidance = tuple(
            bundle.projection_assets["explanation_templates"].get(
                reason.framework_code,
                (),
            )
        )
        blocks.append(
            WhyBlock(
                kernel_hash=kernel.kernel_hash,
                rule_version="projection-5.2-v2.1",
                dimension_code=fact.dimension_code,
                reason_code=reason_code,
                framework_code=reason.framework_code,
                framework_selection_basis=(
                    f"reason={reason_code}",
                    f"route={kernel.customer_route.route.value}",
                    f"state={fact.state.value}",
                ),
                derivation_chain=derivation,
                narrative_skeleton=tuple(skeleton),
                benefit_language=(
                    _benefit_language(fact),
                    reason.explanation,
                    *framework_guidance,
                ),
                confidence=fact.confidence,
                fact_refs=tuple(dict.fromkeys([*fact.fact_refs, *fact.calculation_refs])),
            )
        )

    auxiliary: list[AuxiliaryInsight] = []
    for metric in kernel.auxiliary_diagnostics:
        if metric.metric_code != "EMERGENCY_LIQUIDITY_ALERT" or metric.status == "unavailable" or metric.value is None:
            continue
        value = format_value(metric.value, "months")
        auxiliary.append(
            AuxiliaryInsight(
                kernel_hash=kernel.kernel_hash,
                metric_code=metric.metric_code,
                summary=f"家庭备用金当前约可覆盖 {value}，该项仅作辅助财务观察。",
                fact_refs=metric.calculation_refs,
            )
        )
    return tuple(blocks), tuple(auxiliary)


def project_step_5_3(
    kernel: DiagnosisKernel,
    bundle: RuleBundle,
) -> ActionBlock:
    """从内核独立生成封闭的 5.3 行动块。

    Args:
        kernel: 不可变诊断内核。
        bundle: 当前规则包。

    Returns:
        只含六种保障动作的行动块。
    """

    actions: list[ActionItem] = []
    maintain_items: list[ActionItem] = []

    for correction in kernel.correction_items:
        if not correction.capability_available:
            continue
        target = _correction_target(correction.dimension_codes, kernel)
        wave = _wave_for_fact(target, bundle)
        actions.append(
            ActionItem(
                action_id=f"action-{correction.item_id}",
                wave=wave,
                dimension_code=target.dimension_code,
                action_type=ActionType.ADJUST,
                reason_code=correction.reason_code,
                sequence_reason_codes=("CORRECTION_BEFORE_GAP",),
                requires_human_review=False,
                fact_refs=correction.fact_refs,
            )
        )

    for fact in kernel.dimension_facts:
        if fact.state not in {
            DimensionState.MILD_GAP,
            DimensionState.SIGNIFICANT_GAP,
            DimensionState.SEVERE_GAP,
        }:
            continue
        reason_code = fact.reason_codes[0]
        reason = bundle.reason(reason_code)
        if not reason.actions:
            continue
        action_type = ActionType.NEW_POLICY if (fact.existing_value or Decimal("0")) <= 0 else ActionType.ADD_COVERAGE
        if action_type not in reason.actions:
            action_type = reason.actions[0]
        actions.append(
            ActionItem(
                action_id=f"action-gap-{fact.dimension_code.value}",
                wave=_wave_for_fact(fact, bundle),
                dimension_code=fact.dimension_code,
                action_type=action_type,
                reason_code=reason_code,
                sequence_reason_codes=fact.urgency_reason_codes,
                background_gap_value=fact.gap_value,
                unit=fact.unit,
                requires_human_review=action_type in {ActionType.REDUCED_PAID_UP, ActionType.SURRENDER},
                fact_refs=fact.fact_refs,
            )
        )

    for preserve in kernel.preserve_items:
        if not preserve.capability_available or preserve.dimension_code is None:
            continue
        fact = next(item for item in kernel.dimension_facts if item.dimension_code is preserve.dimension_code)
        maintain_items.append(
            ActionItem(
                action_id=f"action-{preserve.item_id}",
                wave=ActionWave.MAINTAIN,
                dimension_code=preserve.dimension_code,
                action_type=ActionType.MAINTAIN,
                reason_code=preserve.reason_code,
                sequence_reason_codes=("PRESERVE_EXPLICIT",),
                requires_human_review=False,
                fact_refs=fact.fact_refs,
            )
        )

    actions.sort(
        key=lambda item: (
            _wave_rank(item.wave),
            _priority_rank(item.dimension_code, kernel),
            0 if item.action_type is ActionType.ADJUST else 1,
        )
    )
    human_flags = tuple(item.action_id for item in actions if item.requires_human_review)
    hits = _boundary_hits(actions, bundle)
    budget_tradeoff = None
    if kernel.annual_premium_budget_yuan is not None:
        budget_tradeoff = f"已确认年度保费预算为 {format_value(kernel.annual_premium_budget_yuan, 'CNY')}。阶段取舍优先保留第一波，第三波可后移；各维目标继续沿用独立测算结果，不按预算反推。"
    return ActionBlock(
        kernel_hash=kernel.kernel_hash,
        rule_version="projection-5.3-v2.1",
        actions=tuple(actions),
        maintain_items=tuple(maintain_items),
        budget_tradeoff=budget_tradeoff,
        human_review_flags=human_flags,
        handoff_statement=bundle.projection_assets["action_rules"]["handoff_statement"],
        boundary_hits=hits,
    )


def validate_projection_consistency(
    *,
    kernel: DiagnosisKernel,
    why_blocks: tuple[WhyBlock, ...],
    auxiliary_insights: tuple[AuxiliaryInsight, ...],
    action_block: ActionBlock,
    bundle: RuleBundle,
) -> tuple[str, ...]:
    """机械校验 3.1、5.2、5.3 的同源与边界一致性。

    Args:
        kernel: 诊断内核。
        why_blocks: 5.2 八维解释块。
        auxiliary_insights: 5.2 辅助说明。
        action_block: 5.3 行动块。
        bundle: 当前规则包。

    Returns:
        通过的校验项。

    Raises:
        ValueError: 任一投影漂移、越界或遗漏保留项时抛出。
    """

    if len(why_blocks) != 8:
        raise ValueError("5.2 must contain exactly eight dimension blocks")
    if any(item.kernel_hash != kernel.kernel_hash for item in why_blocks):
        raise ValueError("5.2 kernel hash drift")
    if action_block.kernel_hash != kernel.kernel_hash:
        raise ValueError("5.3 kernel hash drift")
    if any(item.metric_code != "EMERGENCY_LIQUIDITY_ALERT" for item in auxiliary_insights):
        raise ValueError("unsupported auxiliary projection")
    if any(item.reason_code == "EMERGENCY_LIQUIDITY_ALERT" for item in action_block.actions):
        raise ValueError("emergency liquidity must not produce insurance actions")

    facts = {item.dimension_code: item for item in kernel.dimension_facts}
    for block in why_blocks:
        if block.reason_code not in facts[block.dimension_code].reason_codes:
            raise ValueError(f"5.2 reason drift for {block.dimension_code}")
        bundle.reason(block.reason_code)
        if not block.fact_refs:
            raise ValueError(f"5.2 block has no fact refs: {block.dimension_code}")

    allowed_reasons = {reason for fact in kernel.dimension_facts for reason in fact.reason_codes}
    allowed_reasons.update(item.reason_code for item in kernel.preserve_items)
    allowed_reasons.update(item.reason_code for item in kernel.correction_items)
    for action in (*action_block.actions, *action_block.maintain_items):
        if action.reason_code not in allowed_reasons:
            raise ValueError(f"5.3 action reason is not in kernel: {action.reason_code}")
        if not action.fact_refs:
            raise ValueError(f"5.3 action has no fact refs: {action.action_id}")

    preserve_ids = {item.dimension_code for item in kernel.preserve_items if item.dimension_code is not None and item.capability_available}
    maintain_ids = {item.dimension_code for item in action_block.maintain_items}
    if preserve_ids != maintain_ids:
        raise ValueError("preserve items must be projected as explicit maintain actions")
    if action_block.boundary_hits:
        raise ValueError(f"5.3 boundary violations: {action_block.boundary_hits}")
    if kernel.annual_premium_budget_yuan is not None and not action_block.budget_tradeoff:
        raise ValueError("confirmed budget constraint must produce a 5.3 tradeoff statement")

    return (
        "PASS: eight dimensions projected from one kernel",
        "PASS: 5.2 reason codes are closed",
        "PASS: 5.3 actions are closed and traceable",
        "PASS: preserve items remain explicit",
        "PASS: emergency liquidity stays in auxiliary domain",
        "PASS: product boundary guard passed",
    )


def _derivation_chain(fact: DimensionFact) -> tuple[str, ...]:
    """把工具分项和内核数字填入固定推导槽位。

    Args:
        fact: 已固化公式版本和推导分项的单维事实。

    Returns:
        不含内部框架码、可直接交给语言节点的推导步骤。
    """

    existing = format_value(fact.existing_value, fact.unit, role="existing")
    ideal = format_value(fact.ideal_value, fact.unit, role="target")
    gap = format_value(fact.gap_value, fact.unit, role="gap")
    if fact.state is DimensionState.NO_NEED:
        return (
            *(_component_line(component) for component in fact.derivation_components),
            "当前人生阶段规则判定为暂不需要。",
        )
    if fact.state is DimensionState.SUFFICIENT:
        return (
            *(_component_line(component) for component in fact.derivation_components),
            f"权威测算显示当前值 {existing}，已达到目标 {ideal}。",
        )
    component_lines = tuple(_component_line(component) for component in fact.derivation_components)
    return (
        *component_lines,
        f"权威测算目标值为 {ideal}。",
        f"已有值为 {existing}。",
        f"两者差额形成缺口 {gap}。",
    )


def _component_line(component: DerivationComponent) -> str:
    """把确定性推导分项转换为业务可读行。

    Args:
        component: 内核中的 DerivationComponent。

    Returns:
        已格式化单位并保留必要说明的分项文本。
    """

    role = {
        "existing_tier": "existing",
        "target_tier": "target",
    }.get(component.key, "value")
    text = f"{component.label}：{format_value(component.value, component.unit, role=role)}。"
    return f"{text}{component.note}。" if component.note else text


def _narrative_skeleton(
    *,
    category: str,
    temperature: str,
    bundle: RuleBundle,
) -> tuple[str, ...]:
    """根据理由类别和触发温度选择四拍叙事骨架。

    Args:
        category: 理由码类别。
        temperature: 主触发温度。
        bundle: 当前规则包。

    Returns:
        缺口、保留、纠错或无需动作对应的四拍骨架。
    """

    typed = bundle.projection_assets.get("narrative_type_skeletons", {})
    category_key = "preserve" if category == "preserve" else "correction" if category == "correction" else "no_need" if category == "no_need" else "gap"
    selected = typed.get(category_key, {}).get(temperature)
    if selected:
        return tuple(selected)
    return tuple(bundle.projection_assets["narrative_skeletons"][temperature])


def _benefit_language(fact: DimensionFact) -> str:
    """生成不改变事实的低压力利益表达。"""

    if fact.state is DimensionState.SUFFICIENT:
        return f"{fact.dimension_name}已经达到当前测算标准，建议维持，不必重复增加。"
    if fact.state is DimensionState.NO_NEED:
        return f"{fact.dimension_name}按当前人生阶段暂不需要进入行动清单。"
    if fact.state is DimensionState.UNKNOWN:
        return f"{fact.dimension_name}的数据还不足，先核验，不做确定性判断。"
    return f"{fact.dimension_name}存在经工具确认的缺口，先看清原因和次序，不进入具体产品。"


def _wave_for_fact(fact: DimensionFact, bundle: RuleBundle) -> ActionWave:
    """把 3.1 序位翻译为行动波次。"""

    rank = fact.priority_rank or 99
    rules = bundle.projection_assets["action_rules"]
    if rank in rules["first_wave_ranks"]:
        return ActionWave.FIRST
    if rank in rules["second_wave_ranks"]:
        return ActionWave.SECOND
    return ActionWave.THIRD


def _correction_target(
    codes: tuple[DimensionCode, ...],
    kernel: DiagnosisKernel,
) -> DimensionFact:
    """选择纠错项关联维度中优先级最高的一维。"""

    facts = [fact for fact in kernel.dimension_facts if fact.dimension_code in codes]
    return min(facts, key=lambda fact: fact.priority_rank or 99)


def _wave_rank(wave: ActionWave) -> int:
    """返回行动波次的稳定排序值。"""

    return {
        ActionWave.FIRST: 0,
        ActionWave.SECOND: 1,
        ActionWave.THIRD: 2,
        ActionWave.MAINTAIN: 3,
    }[wave]


def _priority_rank(
    code: DimensionCode | None,
    kernel: DiagnosisKernel,
) -> int:
    """返回维度在内核中的优先级。"""

    if code is None:
        return 99
    fact = next(item for item in kernel.dimension_facts if item.dimension_code is code)
    return fact.priority_rank or 99


def _boundary_hits(
    actions: list[ActionItem],
    bundle: RuleBundle,
) -> tuple[str, ...]:
    """机械扫描行动块是否泄漏产品级内容。"""

    payload = " ".join(
        [
            *(item.action_id for item in actions),
            *(item.reason_code for item in actions),
        ]
    )
    return tuple(pattern for pattern in bundle.style_contracts["boundary_patterns"] if pattern in payload)
