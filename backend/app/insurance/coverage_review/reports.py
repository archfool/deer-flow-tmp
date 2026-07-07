"""相互独立的客户可见版和内部版保障报告渲染器。"""

from __future__ import annotations

from app.insurance.coverage_review.models import (
    CoverageDimension,
    CoverageReviewResult,
    CustomerCoverageReport,
    InternalCoverageReport,
)

_LABELS = {
    CoverageDimension.LIFE: "寿险",
    CoverageDimension.CRITICAL_ILLNESS: "重疾",
    CoverageDimension.MEDICAL: "医疗",
    CoverageDimension.ACCIDENT: "意外",
    CoverageDimension.RETIREMENT: "养老",
    CoverageDimension.EMERGENCY_RESERVE: "应急储备",
}

_DISCLAIMER = "本报告使用 DRAFT 参数进行演示测算，不可用于真实销售；结果基于现有资料与假设，不构成承诺，最终以正式保险条款及经审核的利益演示为准。"


def render_customer_report(result: CoverageReviewResult) -> CustomerCoverageReport:
    """只渲染适合客户阅读的事实与解释。"""

    lines = [
        f"# 致 {result.household_name} 的一份保障体检",
        "",
        "> **演示测算，不可用于真实销售**",
        "",
        "## 家庭风险概览",
        "保障检视的目的不是一次补齐所有产品，而是先识别影响家庭稳定性的责任空白，再结合预算确定优先级。",
        "",
        "## 六维保障概览",
        "",
        "| 维度 | 状态 | 基准目标 | 当前 | 缺口/责任空白 |",
        "|---|---|---:|---:|---|",
    ]
    structured: dict[str, dict] = {}
    for dimension, assessment in result.dimensions.items():
        baseline = assessment.scenarios.get("baseline")
        if baseline is None:
            gap = "待补充资料"
            target = current = "—"
        elif baseline.missing_responsibilities:
            target = current = "责任型"
            gap = "、".join(baseline.missing_responsibilities)
        else:
            target = f"{baseline.target:,.2f}"
            current = f"{baseline.current:,.2f}"
            gap = f"{baseline.gap:,.2f}"
        lines.append(f"| {_LABELS[dimension]} | {assessment.status.value} | {target} | {current} | {gap} |")
        structured[dimension.value] = assessment.model_dump(mode="json")

    lines.extend(["", "## 每维说明", ""])
    for dimension, assessment in result.dimensions.items():
        lines.append(f"### {_LABELS[dimension]}")
        baseline = assessment.scenarios.get("baseline")
        if baseline is None:
            lines.append("资料尚不完整：" + "、".join(assessment.missing_facts))
        else:
            lines.append(baseline.explanation)
            if baseline.missing_responsibilities:
                lines.append("当前责任空白：" + "、".join(baseline.missing_responsibilities))
            elif baseline.gap > 0:
                lines.append(f"基准场景缺口：{baseline.gap:,.2f}。")
            else:
                lines.append("基准场景未发现数值缺口；仍需核对条款责任、期限和除外事项。")
        lines.append("")

    lines.extend(
        [
            "## 优先级路线图",
            "1. 先补充标记为待确认的客户事实。",
            "2. 优先处理家庭经济支柱的人身和收入中断风险。",
            "3. 再结合年度保费预算讨论责任取舍，避免一次性堆砌方案。",
            "",
            "## 下一步",
            "请与保险代理人逐项确认客户事实、现有保单责任和预算后，再进入正式方案设计。",
            "",
            "## 声明",
            _DISCLAIMER,
        ]
    )
    return CustomerCoverageReport(
        customer_id=result.customer_id,
        markdown="\n".join(lines),
        structured_dimensions=structured,
        disclaimer=_DISCLAIMER,
    )


def render_internal_report(result: CoverageReviewResult) -> InternalCoverageReport:
    """通过独立 Schema 和代码路径渲染仅供代理人使用的诊断信息。"""

    missing = tuple(sorted({path for assessment in result.dimensions.values() for path in assessment.missing_facts}))
    lines = [
        f"# {result.household_name} 保障检视——内部诊断版（不可外发）",
        "",
        f"- 客户档案版本：{result.profile_version or '未持久化 Mock'}",
        f"- 参数版本：{result.parameter_version} / {result.parameter_status}",
        f"- 测算基准日：{result.as_of_date.isoformat()}",
        "",
        "## 测算假设与置信度",
    ]
    lines.extend(f"- {note}" for note in result.confidence_notes)
    lines.extend(["", "## 缺失信息清单"])
    # 展开为字符串，而不是把生成器对象本身追加到列表；否则拼接 Markdown
    # 行时会失败。
    lines.extend(f"- {path}" for path in missing)
    if not missing:
        lines.append("- 当前计算所需硬事实完整。")
    lines.extend(["", "## DRAFT 参数提示"])
    lines.extend(f"- {note}" for note in result.draft_parameter_notes)
    lines.extend(
        [
            "",
            "## 预算与讲解建议",
            "- 先解释收入中断和家庭责任，再展示数字，避免直接以产品为中心。",
            "- 所有缺口应结合年度保费预算分层处理；当前版本不执行产品匹配。",
            "",
            "## 客户顾虑与心理画像",
            "暂无经客户确认的数据。不得根据模型推断直接写入客户档案或对客材料。",
            "",
            "## 异议预判",
            "仅在代理人补充真实沟通记录后生成；当前不进行无依据推断。",
        ]
    )
    return InternalCoverageReport(
        customer_id=result.customer_id,
        markdown="\n".join(lines),
        confidence_notes=result.confidence_notes,
        missing_facts=missing,
        draft_parameters=result.draft_parameter_notes,
    )
