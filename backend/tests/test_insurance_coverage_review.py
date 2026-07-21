"""企业级保障检视规则、证据、内核与投影契约测试。"""

from __future__ import annotations

import asyncio
import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.insurance.coverage_review import (
    DIMENSION_ORDER,
    ActionType,
    DimensionCode,
    DimensionState,
    ModelNarrativeHarness,
    RuleBoundNarrativeHarness,
    build_calculation_request,
    build_customer_view_model,
    build_diagnosis_kernel,
    build_evidence_bundle,
    build_mock_tools,
    collect_field_evidence,
    compile_rule_bundle,
    extract_policy_report,
    load_compiled_rule_bundle,
    load_default_knowledge_registry,
    load_default_rule_bundle,
    load_knowledge_excerpt,
    normalize_calculator_payload,
    project_step_5_2,
    project_step_5_3,
    render_customer_report,
    resolve_trigger_binding,
    select_knowledge_skills,
    validate_projection_consistency,
)
from app.insurance.coverage_review.rules import DEFAULT_SOURCE_DIR, RuleCompilerError
from app.insurance.coverage_review.tools import RESOURCE_DIR, MockZhongbaoxinReportTool
from app.insurance.mock_data import (
    build_complete_mock_profile,
    build_incomplete_mock_profile,
)


def _run(value: Any) -> Any:
    """在不依赖 pytest-asyncio 的环境中执行协程。"""

    return asyncio.run(value)


def _tool_call_model(tool_name: str, payload: dict[str, Any]) -> MagicMock:
    """构造返回唯一结构化工具调用的模型桩。"""

    response = MagicMock()
    response.tool_calls = [
        {
            "name": tool_name,
            "args": payload,
            "id": "call-test",
            "type": "tool_call",
        }
    ]
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=response)
    model = MagicMock()
    model.bind_tools.return_value = bound
    return model


def _build_pipeline_parts() -> tuple[Any, Any, Any, Any]:
    """构造一组完整证据、测算、内核和规则包。"""

    profile = build_complete_mock_profile()
    tools = build_mock_tools()
    bundle = load_default_rule_bundle()
    center = _run(tools.customer_center.lookup(profile))
    snapshot = _run(tools.customer_profile.lookup(profile, 1))
    raw = _run(tools.policy_report.fetch(profile))
    policy_report = extract_policy_report(raw)
    trigger = resolve_trigger_binding(["A2", "B1"], bundle)
    fields, missing, assumptions = collect_field_evidence(
        profile=profile,
        center=center,
        profile_snapshot=snapshot,
        answers={},
        bundle=bundle,
    )
    assert not missing
    evidence = build_evidence_bundle(
        profile=profile,
        profile_version=1,
        center=center,
        fields=fields,
        policy_report=policy_report,
        trigger_binding=trigger,
        assumptions=assumptions,
        bundle=bundle,
    )
    request = build_calculation_request(evidence)
    calculation = _run(tools.precise_calculator.calculate(request, profile))
    kernel = build_diagnosis_kernel(
        review_id="review-golden-001",
        revision=1,
        evidence=evidence,
        calculation=calculation,
        bundle=bundle,
    )
    return evidence, calculation, kernel, bundle


def test_rule_bundle_closes_eight_dimensions_twenty_reasons_and_twenty_nine_triggers() -> None:
    """规则编译必须锁住最新材料中的三个核心计数。"""

    bundle = load_default_rule_bundle()
    compiled = compile_rule_bundle()

    assert bundle.bundle_hash == compiled.bundle_hash
    assert tuple(item.code for item in bundle.dimensions) == DIMENSION_ORDER
    assert len(bundle.reasons) == 20
    assert len(bundle.triggers) == 29
    assert bundle.trigger("C6").focus_dimensions == (
        DimensionCode.DISEASE,
        DimensionCode.MEDICAL,
    )
    assert bundle.dimension(DimensionCode.DISABILITY).name == "伤残保障"
    assert bundle.reason("EMERGENCY_LIQUIDITY_ALERT").actions == ()
    assert bundle.compiler_version == "coverage-rule-compiler-v2.1"
    assert bundle.reason_code_set_hash.startswith("sha256:")
    assert bundle.decision_coverage_hash.startswith("sha256:")
    assert set(bundle.golden_case_results.values()) == {"PASS"}


def test_compiled_rule_bundle_rejects_tampered_content(tmp_path: Path) -> None:
    """已发布 Bundle 内容被改写但未重签哈希时必须拒绝加载。"""

    bundle = load_default_rule_bundle().model_dump(mode="json")
    bundle["dimensions"][0]["name"] = "被篡改的维度"
    path = tmp_path / "tampered-bundle.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuleCompilerError, match="hash mismatch"):
        load_compiled_rule_bundle(path)


def test_rule_compiler_rejects_legacy_accident_semantic(tmp_path: Path) -> None:
    """规则源将 D3 退回意外口径时必须在编译阶段失败。"""

    source_dir = tmp_path / "rule-source"
    shutil.copytree(DEFAULT_SOURCE_DIR, source_dir)
    registry_path = source_dir / "dimension_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["dimensions"][2]["name"] = "意外保障"
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(RuleCompilerError, match="D3 must use the disability semantic"):
        compile_rule_bundle(source_dir)


def test_knowledge_registry_routes_minimal_communication_only_excerpts(
    tmp_path: Path,
) -> None:
    """Knowledge Skill 必须受权限约束，每个节点只选最小集。"""

    registry = load_default_knowledge_registry()
    selected = select_knowledge_skills(
        registry,
        node="meeting-support",
        topics={"family_risk", "meeting", "objection", "contract_boundary"},
        max_skills=3,
    )

    assert len(registry.skills) == 11
    assert len(selected) == 3
    assert all(item.authority == "COMMUNICATION_KNOWLEDGE" for item in selected)
    assert all("CUSTOMER_FACT" in item.prohibited_uses for item in selected)
    customer_copy_skills = select_knowledge_skills(
        registry,
        node="customer-copy",
        topics={"family_risk", "consultative", "contract_boundary"},
        max_skills=3,
    )
    assert customer_copy_skills
    assert all("customer-copy" in item.allowed_nodes for item in customer_copy_skills)

    skill = registry.get("family-insurance-quickstart")
    skill_dir = tmp_path / skill.skill_id
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 家庭风险\n" + "方法" * 400, encoding="utf-8")
    excerpt = load_knowledge_excerpt(
        registry,
        skill_id=skill.skill_id,
        skills_root=tmp_path,
        max_chars=500,
    )

    assert excerpt.truncated is True
    assert len(excerpt.content) == 500
    assert excerpt.authority == "COMMUNICATION_KNOWLEDGE"


def test_model_narrative_harness_falls_back_when_internal_code_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型输出即使满足 Schema，只要夹带内部码也必须降级。"""

    evidence, _calculation, kernel, _bundle = _build_pipeline_parts()
    fallback = RuleBoundNarrativeHarness()
    expected = _run(fallback.analyze_customer(evidence, kernel, {}))
    leaked = kernel.dimension_facts[0].reason_codes[0]
    invalid = expected.model_copy(update={"one_line_profile": f"错误泄漏 {leaked}"})
    model = _tool_call_model(
        "CustomerAnalysis",
        invalid.model_dump(mode="json"),
    )
    monkeypatch.setattr(
        "app.insurance.coverage_review.narrative.create_chat_model",
        lambda **_kwargs: model,
    )

    result = _run(ModelNarrativeHarness(fallback=fallback).analyze_customer(evidence, kernel, {}))

    assert result.one_line_profile == expected.one_line_profile
    assert result.generation_mode.value == "fallback"
    assert leaked not in result.one_line_profile


def test_model_narrative_harness_rejects_direct_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MiniMax 未调用唯一 Schema 工具时不得把自由文本写入状态。"""

    evidence, _calculation, kernel, _bundle = _build_pipeline_parts()
    fallback = RuleBoundNarrativeHarness()
    expected = _run(fallback.analyze_customer(evidence, kernel, {}))
    response = MagicMock()
    response.content = "看起来像 JSON，但没有通过工具边界。"
    response.tool_calls = []
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=response)
    model = MagicMock()
    model.bind_tools.return_value = bound
    monkeypatch.setattr(
        "app.insurance.coverage_review.narrative.create_chat_model",
        lambda **_kwargs: model,
    )

    result = _run(
        ModelNarrativeHarness(fallback=fallback).analyze_customer(
            evidence,
            kernel,
            {},
        )
    )

    assert result.one_line_profile == expected.one_line_profile
    assert result.generation_mode.value == "fallback"
    model.bind_tools.assert_called_once()
    assert model.bind_tools.call_args.kwargs == {"tool_choice": "auto"}


def test_model_narrative_harness_corrects_missing_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MiniMax 首次直接回答时应在节点内部纠偏后再写入状态。"""

    evidence, _calculation, kernel, _bundle = _build_pipeline_parts()
    fallback = RuleBoundNarrativeHarness()
    generated = _run(fallback.analyze_customer(evidence, kernel, {})).model_dump(mode="json")
    direct_response = MagicMock()
    direct_response.content = "我先直接说明结论。"
    direct_response.tool_calls = []
    tool_response = MagicMock()
    tool_response.tool_calls = [
        {
            "name": "CustomerAnalysis",
            "args": generated,
            "id": "call-corrected",
            "type": "tool_call",
        }
    ]
    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=[direct_response, tool_response])
    model = MagicMock()
    model.bind_tools.return_value = bound
    monkeypatch.setattr(
        "app.insurance.coverage_review.narrative.create_chat_model",
        lambda **_kwargs: model,
    )

    result = _run(
        ModelNarrativeHarness(fallback=fallback).analyze_customer(
            evidence,
            kernel,
            {},
        )
    )

    assert result.generation_mode.value == "model"
    assert bound.ainvoke.await_count == 2
    correction_messages = bound.ainvoke.await_args_list[1].args[0]
    assert "禁止直接回答" in correction_messages[-1].content


def test_model_narrative_harness_binds_audit_metadata_in_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型生成事实字段名时应由 Harness 重绑审计引用和生成模式。"""

    evidence, _calculation, kernel, _bundle = _build_pipeline_parts()
    fallback = RuleBoundNarrativeHarness()
    generated = _run(fallback.analyze_customer(evidence, kernel, {})).model_dump(mode="json")
    generated["fact_refs"] = ["age", "annual_income_wan"]
    generated["generation_mode"] = "fallback"
    model = _tool_call_model("CustomerAnalysis", generated)
    monkeypatch.setattr(
        "app.insurance.coverage_review.narrative.create_chat_model",
        lambda **_kwargs: model,
    )

    result = _run(
        ModelNarrativeHarness(fallback=fallback).analyze_customer(
            evidence,
            kernel,
            {},
        )
    )

    assert result.generation_mode.value == "model"
    assert "age" not in result.fact_refs
    assert "annual_income_wan" not in result.fact_refs
    assert set(result.fact_refs) == {
        *(item.source_ref for item in evidence.fields),
        *(item.fact_id for item in evidence.policy_report.policy_facts),
    }
    invocation_config = model.bind_tools.return_value.ainvoke.call_args.kwargs["config"]
    assert invocation_config["tags"] == [
        "coverage-review",
        "model:default",
        "prompt:coverage-review-narrative-prompt-v2.3",
    ]


def test_dimension_explanation_binds_derivation_chain_in_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型改写测算标签时应由代码恢复 5.2 的完整推导链。"""

    _evidence, _calculation, kernel, bundle = _build_pipeline_parts()
    fallback = RuleBoundNarrativeHarness()
    why_blocks, _auxiliary = project_step_5_2(kernel, bundle)
    generated = [item.model_dump(mode="json") for item in _run(fallback.verbalize_explanations(kernel, why_blocks, {}))]
    medical = next(item for item in generated if item["dimension_code"] == DimensionCode.MEDICAL.value)
    medical["calculation_explanation"] = [
        "医疗责任还有一层差距。",
    ]
    model = _tool_call_model("_DimensionNarrativeList", {"items": generated})
    monkeypatch.setattr(
        "app.insurance.coverage_review.narrative.create_chat_model",
        lambda **_kwargs: model,
    )

    result = _run(
        ModelNarrativeHarness(fallback=fallback).verbalize_explanations(
            kernel,
            why_blocks,
            {},
        )
    )
    result_by_code = {item.dimension_code: item for item in result}
    block_by_code = {item.dimension_code: item for item in why_blocks}

    assert result_by_code[DimensionCode.MEDICAL].calculation_explanation == (block_by_code[DimensionCode.MEDICAL].derivation_chain)


def test_action_narrative_binds_ids_order_and_boundaries_in_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5.3 模型只填语言槽位，行动身份、顺序和边界必须由代码绑定。"""

    _evidence, _calculation, kernel, bundle = _build_pipeline_parts()
    fallback = RuleBoundNarrativeHarness()
    action_block = project_step_5_3(kernel, bundle)
    fallback_result = _run(fallback.verbalize_actions(kernel, action_block, {}))
    generated = {
        "overview": "先处理家庭责任最重的方向，再安排长期目标。",
        "titles": [item.title for item in fallback_result.items],
        "rationales": [item.rationale for item in fallback_result.items],
        "agent_languages": [item.agent_language for item in fallback_result.items],
        "generation_mode": "fallback",
    }
    model = _tool_call_model("_ActionNarrativeDraft", generated)
    monkeypatch.setattr(
        "app.insurance.coverage_review.narrative.create_chat_model",
        lambda **_kwargs: model,
    )

    result = _run(
        ModelNarrativeHarness(fallback=fallback).verbalize_actions(
            kernel,
            action_block,
            {},
        )
    )

    ordered_actions = (*action_block.actions, *action_block.maintain_items)
    assert tuple(item.action_id for item in result.items) == tuple(item.action_id for item in ordered_actions)
    assert result.closing_boundary == action_block.handoff_statement
    assert result.budget_tradeoff == action_block.budget_tradeoff
    assert result.generation_mode.value == "model"


def test_customer_copy_harness_reorders_complete_model_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型按优先级返回完整八维时应由代码重排而不是误判失败。"""

    evidence, _calculation, kernel, bundle = _build_pipeline_parts()
    fallback = RuleBoundNarrativeHarness()
    why_blocks, _auxiliary = project_step_5_2(kernel, bundle)
    why_blocks = tuple(
        item.model_copy(
            update={
                "fact_refs": (
                    *item.fact_refs,
                    "agent-answer:existing_medical_responsibility_tier",
                )
            }
        )
        if item.dimension_code is DimensionCode.MEDICAL
        else item
        for item in why_blocks
    )
    action_block = project_step_5_3(kernel, bundle)
    dimensions = _run(fallback.verbalize_explanations(kernel, why_blocks, {}))
    action_narrative = _run(fallback.verbalize_actions(kernel, action_block, {}))
    generated = _run(
        fallback.write_customer_copy(
            evidence,
            kernel,
            why_blocks,
            dimensions,
            action_narrative,
            (),
            {},
        )
    ).model_dump(mode="json")
    generated["salutation"] = "演示甲，您好。"
    generated["dimension_narratives"] = list(reversed(generated["dimension_narratives"]))
    model = _tool_call_model("CustomerReportCopy", generated)
    monkeypatch.setattr(
        "app.insurance.coverage_review.narrative.create_chat_model",
        lambda **_kwargs: model,
    )

    result = _run(
        ModelNarrativeHarness(fallback=fallback).write_customer_copy(
            evidence,
            kernel,
            why_blocks,
            dimensions,
            action_narrative,
            (),
            {},
        )
    )

    assert result.salutation == "演示甲，您好。"
    assert result.generation_mode.value == "model"
    assert tuple(item.dimension_code for item in result.dimension_narratives) == DIMENSION_ORDER
    medical = next(item for item in result.dimension_narratives if item.dimension_code is DimensionCode.MEDICAL)
    assert "agent-answer:existing_medical_responsibility_tier" in medical.fact_refs


def test_customer_copy_harness_repairs_only_forbidden_visible_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型回显规则守则时应局部替换并保留其他模型文案。"""

    evidence, _calculation, kernel, bundle = _build_pipeline_parts()
    fallback = RuleBoundNarrativeHarness()
    why_blocks, _auxiliary = project_step_5_2(kernel, bundle)
    action_block = project_step_5_3(kernel, bundle)
    dimensions = _run(fallback.verbalize_explanations(kernel, why_blocks, {}))
    dimensions = tuple(
        item.model_copy(
            update={
                "why_it_matters": "财富管理只讨论确定性。不得使用未经批准的收益率数字。",
            }
        )
        if item.dimension_code is DimensionCode.WEALTH
        else item
        for item in dimensions
    )
    action_narrative = _run(fallback.verbalize_actions(kernel, action_block, {}))
    generated = _run(
        fallback.write_customer_copy(
            evidence,
            kernel,
            why_blocks,
            dimensions,
            action_narrative,
            (),
            {},
        )
    ).model_dump(mode="json")
    generated["salutation"] = "演示甲，您好。"
    wealth = next(item for item in generated["dimension_narratives"] if item["dimension_code"] == DimensionCode.WEALTH.value)
    wealth["why_it_matters"] = "不得使用未经批准的收益率数字。"
    model = _tool_call_model("CustomerReportCopy", generated)
    monkeypatch.setattr(
        "app.insurance.coverage_review.narrative.create_chat_model",
        lambda **_kwargs: model,
    )

    result = _run(
        ModelNarrativeHarness(fallback=fallback).write_customer_copy(
            evidence,
            kernel,
            why_blocks,
            dimensions,
            action_narrative,
            (),
            {},
        )
    )

    assert result.generation_mode.value == "hybrid"
    assert result.salutation == "演示甲，您好。"
    assert "收益率" not in result.model_dump_json()
    view_model = build_customer_view_model(
        profile=build_complete_mock_profile(),
        evidence=evidence,
        kernel=kernel,
        why_blocks=why_blocks,
        dimension_narratives=dimensions,
        auxiliary_insights=_auxiliary,
        action_block=action_block,
        action_narrative=action_narrative,
        customer_copy=result,
        approved_kernel_hash=kernel.kernel_hash,
    )
    artifact = _run(
        render_customer_report(
            view_model=view_model,
            kernel=kernel,
            action_block=action_block,
            bundle=bundle,
        )
    )
    assert "收益率" not in artifact.html


def test_calculator_normalizer_requires_d3_semantic_proof_and_migrates_legacy_code() -> None:
    """D3 无伤残语义证明时必须阻断，有证明才允许收敛历史标签。"""

    items = [
        ("D1", "疾病保障"),
        ("D2", "医疗保障"),
        ("D3", "意外保障"),
        ("D4", "身故保障"),
        ("D5", "护理保障"),
        ("A1", "财富管理"),
        ("B1", "养老储蓄"),
    ]
    health = [
        {
            "coverTypeCode": code,
            "coverTypeName": name,
            "existingCoverage": "10",
            "idealData": "100",
            "coverageGap": "90",
            "isFull": 0,
            "recommendFifst": 1,
        }
        for code, name in items
        if code.startswith("D")
    ]
    wealth = [
        {
            "coverTypeCode": code,
            "coverTypeName": name,
            "existingCoverage": "10",
            "idealData": "100",
            "coverageGap": "90",
            "isFull": 0,
            "recommendFirst": 0,
        }
        for code, name in items
        if code in {"A1", "B1"}
    ]
    payload = {
        "coverList": [
            {
                "coverItemList": [
                    {"coverTypeGroupCode": "D", "coverTypeList": health},
                    {"coverTypeGroupCode": "A", "coverTypeList": [wealth[0]]},
                    {"coverTypeGroupCode": "B", "coverTypeList": [wealth[1]]},
                    {
                        "coverTypeGroupCode": "C",
                        "coverTypeList": [
                            {
                                "coverTypeCode": "B1",
                                "coverTypeName": "传承储备",
                                "existingCoverage": "0",
                                "idealData": "0",
                                "coverageGap": "0",
                                "isFull": 1,
                                "recommendFirst": 0,
                            }
                        ],
                    },
                ]
            }
        ]
    }

    with pytest.raises(ValueError, match="DIMENSION_SEMANTIC_VERSION_MISMATCH"):
        normalize_calculator_payload(payload)

    payload["dimensionSemanticVersions"] = {"D3": "disability-v2.1"}
    result = normalize_calculator_payload(payload)

    assert tuple(item.dimension_code for item in result) == DIMENSION_ORDER
    disability = result[DIMENSION_ORDER.index(DimensionCode.DISABILITY)]
    assert disability.source_name == "意外保障"
    assert disability.dimension_code is DimensionCode.DISABILITY
    assert disability.semantic_version == "disability-v2.1"
    assert result[-1].dimension_code is DimensionCode.LEGACY


def test_policy_report_extraction_flags_conflicting_care_amounts() -> None:
    """中保信报告中的护理金额冲突不能被静默吸收。"""

    profile = build_complete_mock_profile()
    tool = MockZhongbaoxinReportTool(
        RESOURCE_DIR / "mock_zhongbaoxin_report.txt",
    )
    raw = _run(tool.fetch(profile))

    extraction = extract_policy_report(raw)

    assert extraction.total_active_policies == 31
    assert extraction.conflicts
    assert {fact.value for fact in extraction.policy_facts if fact.category == "护理保障"} == {
        Decimal("7688"),
        Decimal("0.768"),
    }


def test_trigger_composition_uses_hottest_temperature_and_dimension_union() -> None:
    """复合触发必须由最烫场景控制开场，并合并重心维度。"""

    binding = resolve_trigger_binding(["A2", "A4", "B1"], load_default_rule_bundle())

    assert binding.primary_trigger_id == "B1"
    assert binding.temperature.value == "fear"
    assert binding.focus_dimensions == (
        DimensionCode.DISEASE,
        DimensionCode.MEDICAL,
        DimensionCode.DEATH,
    )
    assert any("禁止开场甩缺口" in item for item in binding.guardrails)


def test_incomplete_profile_requests_only_missing_blocking_fact() -> None:
    """缺失负债时只生成对应 P0 缺口，不把未知误判成零。"""

    profile = build_incomplete_mock_profile()
    tools = build_mock_tools()
    bundle = load_default_rule_bundle()
    center = _run(tools.customer_center.lookup(profile))
    snapshot = _run(tools.customer_profile.lookup(profile, 1))

    _fields, missing, _assumptions = collect_field_evidence(
        profile=profile,
        center=center,
        profile_snapshot=snapshot,
        answers={},
        bundle=bundle,
    )

    assert [item.key for item in missing] == ["large_loan_wan"]


def test_kernel_and_projections_are_eight_dimensional_and_same_source() -> None:
    """内核、5.2 和 5.3 必须同源且应急指标不能形成保险动作。"""

    _evidence, _calculation, kernel, bundle = _build_pipeline_parts()
    why_blocks, auxiliary = project_step_5_2(kernel, bundle)
    actions = project_step_5_3(kernel, bundle)
    checks = validate_projection_consistency(
        kernel=kernel,
        why_blocks=why_blocks,
        auxiliary_insights=auxiliary,
        action_block=actions,
        bundle=bundle,
    )

    assert tuple(item.dimension_code for item in kernel.dimension_facts) == DIMENSION_ORDER
    assert kernel.customer_route.route.value == "standard"
    assert kernel.dimension_facts[2].dimension_name == "伤残保障"
    assert all(item.metric_code == "EMERGENCY_LIQUIDITY_ALERT" for item in kernel.auxiliary_diagnostics)
    assert len(why_blocks) == 8
    assert all(item.kernel_hash == kernel.kernel_hash for item in why_blocks)
    assert all(item.reason_code != "EMERGENCY_LIQUIDITY_ALERT" for item in actions.actions)
    assert all(item.action_type in set(ActionType) for item in (*actions.actions, *actions.maintain_items))
    assert len(checks) == 6


def test_customer_template_escapes_untrusted_narrative_content() -> None:
    """对客模板必须转义叙事字段而不生成可执行标签。"""

    evidence, _calculation, kernel, bundle = _build_pipeline_parts()
    why_blocks, _auxiliary = project_step_5_2(kernel, bundle)
    actions = project_step_5_3(kernel, bundle)
    harness = RuleBoundNarrativeHarness()
    dimension_narratives = _run(
        harness.verbalize_explanations(
            kernel,
            why_blocks,
            {},
        )
    )
    action_narrative = _run(harness.verbalize_actions(kernel, actions, {}))
    customer_copy = _run(
        harness.write_customer_copy(
            evidence,
            kernel,
            why_blocks,
            dimension_narratives,
            action_narrative,
            (),
            {},
        )
    )
    assert {item.action_id for item in action_narrative.items} == {item.action_id for item in (*actions.actions, *actions.maintain_items)}
    view_model = build_customer_view_model(
        profile=build_complete_mock_profile(),
        evidence=evidence,
        kernel=kernel,
        why_blocks=why_blocks,
        dimension_narratives=dimension_narratives,
        auxiliary_insights=_auxiliary,
        action_block=actions,
        action_narrative=action_narrative,
        customer_copy=customer_copy,
        approved_kernel_hash=kernel.kernel_hash,
    ).model_copy(update={"opening_paragraphs": ("<script>alert('unsafe')</script>",)})
    assert view_model.priority_actions == (
        *actions.actions,
        *actions.maintain_items,
    )

    artifact = _run(
        render_customer_report(
            view_model=view_model,
            kernel=kernel,
            action_block=actions,
            bundle=bundle,
        )
    )

    assert "<script>alert" not in artifact.html
    assert "&lt;script&gt;alert" in artifact.html
    assert "维持不动" in artifact.html
    assert "家庭财务摘要" in artifact.html
    assert "未来检视安排" in artifact.html
    assert "structure_misallocation" not in artifact.html
    assert "review_scope" not in artifact.html
    assert len(artifact.validation_results) >= 6
    assert all(item.startswith("PASS:") for item in artifact.validation_results)


def test_kernel_hash_is_stable_for_identical_frozen_inputs() -> None:
    """同一证据和精算快照必须得到相同诊断内核哈希。"""

    evidence, calculation, first, bundle = _build_pipeline_parts()

    second = build_diagnosis_kernel(
        review_id=first.review_id,
        revision=first.revision,
        evidence=evidence,
        calculation=calculation,
        bundle=bundle,
    )

    assert first.kernel_hash == second.kernel_hash
    assert any(fact.state is DimensionState.SEVERE_GAP for fact in first.dimension_facts)
