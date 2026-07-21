#!/usr/bin/env python3
"""Build source-grounded book skills from extracted chapter corpora."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path


PROFILES = {
    "family-insurance-quickstart": {
        "title": "1小时搞定全家保险", "author": "陈铜",
        "topics": "家庭风险识别、生命周期保障规划、投保决策、合同阅读和理赔",
        "core": [
            "先识别家庭风险，再讨论产品；产品只是风险转移工具，不是规划起点。",
            "用家庭生命周期判断保障责任：单身、新婚、育儿、赡养和退休阶段的责任不同。",
            "把保额价值与保费价格分开判断，先确定损失缺口，再校验预算承受力。",
            "按买前、买中、买后三阶段管理保险：规划与选择、关系人和条款确认、保全与理赔。",
            "把保险建议书当作说明材料，把保险合同当作权利义务的最终依据。",
        ],
        "rules": ["家庭责任越大，越应优先保障主要收入贡献者。", "先补高损失、低承受能力的风险缺口，再考虑储蓄属性。", "家庭结构或收入显著变化时重新检视保额和关系人。", "投保前核对告知、等待期、免责和续保条件。", "出险后先报案、保存证据，再按合同和流程申请理赔。"],
    },
    "professional-insurance-sales": {
        "title": "保险专业销售技术", "author": "王健康、徐沈新",
        "topics": "客户投保行为、专业销售流程、产品分析、建议书和自我管理",
        "core": [
            "专业销售由客户需求、保险原理、产品责任和规范流程共同构成。",
            "从需求、心理和行为三个层面分析客户投保决策，不能只处理表面异议。",
            "完整流程是准保户开拓、拜访准备、接洽、洽谈、异议处理、促成和售后服务。",
            "产品分析必须把人身险与财产险的保障对象、责任、期限和适用场景分开。",
            "保险建议书要把客户事实、风险分析、方案理由和责任边界清晰对应。",
            "用形象管理、活动管理和复盘维持长期专业产能。",
        ],
        "rules": ["客户行为与表达不一致时，继续验证真实动机。", "未完成需求分析前不进入产品比较。", "建议书中的每项责任都应能追溯到客户风险。", "异议处理后必须确认客户是否真正理解。", "成交后立即进入保单递送和持续服务流程。"],
    },
    "insurance-precision-marketing": {
        "title": "保险精准营销", "author": "沃晟学苑",
        "topics": "13类客户细分、KYC、需求导向展业、案例复盘和客户多重身份",
        "core": [
            "用客户细分建立分析入口，但不把标签当结论；同一客户通常具有多重身份。",
            "每类客户都按认识客户、展业流程、案例复盘、展业心得四层学习。",
            "KYC不仅采集资产，还要覆盖家庭、企业、婚姻、身份、税务、目标和风险偏好。",
            "从客户特征推导风险点，再从风险点选择沟通顺序，避免人为制造需求。",
            "案例复盘用于检验方法，不应被机械复制到背景不同的客户。",
        ],
        "rules": ["识别客户至少两个可能重叠的身份标签。", "先列事实与未知项，再列风险假设。", "以客户最关心的风险作为首个沟通入口。", "案例只提供分析路径，不直接提供方案答案。", "每次面谈后更新KYC和下一步验证问题。"],
    },
    "insurance-sales-scenarios": {
        "title": "保险这样卖才对：保险销售人员超级情景训练", "author": "晋鹏",
        "topics": "电话约访、登门拜访、产品推介、异议处理、促成和售后服务情景",
        "core": [
            "把保险销售训练拆成可复现的客户情景，而不是背诵一套万能话术。",
            "先判断客户异议属于产品、需求、信用、支付还是决策障碍，再选择回应。",
            "回应顺序采用倾听确认、追问原因、提供信息、确认理解和约定下一步。",
            "产品推介从客户需求出发，用责任和场景说明价值，不用销量替代适配性。",
            "售后服务覆盖保单递送、持续联络、保全、理赔协助和转介绍。",
        ],
        "rules": ["客户说忙时先争取明确的下一次沟通许可。", "客户要求资料时先确认其真正关心的问题。", "客户比较价格时同步比较责任、期限和限制。", "客户犹豫时找出未解决的单一障碍。", "理赔场景只承诺协助流程，不承诺结果。"],
    },
    "insurance-sales-playbook": {
        "title": "保险销售一本就够：基础知识+销售话术+实战技巧+成功案例", "author": "付刚",
        "topics": "销售准备、拜访准备、面谈、异议处理、促成、售后和客户开拓",
        "core": [
            "销售准备同时包含产品知识、职业道德、表达、仪表、礼节、心态和目标。",
            "拜访前完成客户筛选、资料收集、预约、计划和建议书准备。",
            "面谈依靠第一印象、氛围、倾听、投保心理判断和充分产品说明。",
            "异议解决使用识别原因、调整策略、回应验证和再次访问的闭环。",
            "成交是识别购买信号并解决最后障碍，不是用压力取代客户决定。",
            "售后和客户关系管理是续期、口碑与转介绍的来源。",
        ],
        "rules": ["没有明确拜访目标时先不约访。", "客户资料不足时建议书只能作为假设稿。", "面谈中倾听时间不应被产品讲解完全挤占。", "拒绝后记录原因和重访条件。", "成交后设置固定服务触点。"],
    },
    "insurance-sales-objection-handling": {
        "title": "保险销售实战口才训练", "author": "丁正",
        "topics": "拜访障碍、保险偏见、公司信任、需求排斥、支付、险种、促成和服务",
        "core": [
            "先给客户表达空间，再区分真实异议与礼貌性拒绝。",
            "把异议分为拜访、保险认知、公司信用、需求、支付、险种、决策和售后八类。",
            "回应不靠反驳，而靠共情、澄清、证据、选择和确认。",
            "对社保、储蓄、投资和商业保险做功能比较，避免把不同工具放在单一收益维度竞争。",
            "话术训练的目标是形成判断能力，而不是机械复述句子。",
        ],
        "rules": ["先复述客户观点，确认没有误解。", "一次只处理一个核心异议。", "涉及公司和理赔时使用可核验事实。", "支付异议先区分预算不足与价值未建立。", "客户仍未准备好时保留退出和再次联系空间。"],
    },
    "big-policy-sales": {
        "title": "大保单销售", "author": "杨响华、王萍",
        "topics": "大单销售八大系统、目标市场、约访、沟通、方案、成交、服务和品牌",
        "core": [
            "大保单包含单量大、保额高和保费大三个维度，不能只按保费判断。",
            "销售高手依靠系统训练：信念、目标市场、销售流程、服务、自我管理、沟通和品牌经营相互支撑。",
            "目标市场建设先于零散开拓，通过持续经营形成可重复的客户来源。",
            "把约访、观念沟通、方案设计、成交和拒绝处理做成连续流程。",
            "客户服务强调及时出现、持续关系和分层价值，并反向促进转介绍。",
            "知信行合一：知识必须转化为相信并持续行动，才能形成稳定业绩。",
        ],
        "rules": ["大单目标必须拆成活动量和可跟踪行为。", "先经营目标市场，再扩大名单。", "方案以足额保障和客户承受力同时校验。", "拒绝是流程反馈，不是对销售人员的否定。", "个人品牌必须由专业能力和持续服务支撑。"],
    },
    "large-policy-closing": {
        "title": "大额保单成交攻略", "author": "曾祥霞",
        "topics": "人身风险、品质生活、婚姻财富、家企隔离、税务和家族传承",
        "core": [
            "把大额保单放入家庭整体财富风险管理，而不是孤立强调保单金额。",
            "六类攻略依次覆盖人身风险、品质生活、婚姻财富、家企隔离、税务安排和家族传承。",
            "方案采用风险案例、风险分析、成交攻略的结构，将事实、风险和工具对应。",
            "关系人、现金价值、受益安排和缴费现金流共同决定保单的实际法律经济效果。",
            "保险与信托、公司治理、婚姻安排等工具组合时，要明确各自边界和前提。",
        ],
        "rules": ["先画家庭与企业关系图，再讨论投保架构。", "先测算人力资本和现金流缺口。", "关系人设置必须与传承和控制目标一致。", "债务或税务效果不得绝对化承诺。", "复杂方案必须经过法律、税务和核保复核。"],
    },
    "large-policy-practical-guide": {
        "title": "大额保单操作实务", "author": "贾明军等",
        "topics": "大额保单、债务隔离、婚姻财富、传承、税收、健康、信托和境外配置",
        "core": [
            "从高净值家庭的资产、控制、婚姻、代际、债务、税务和跨境烦恼识别规划目标。",
            "理解大额保单的订立、变更、撤销、解除、现金价值和关系人后再设计架构。",
            "所谓债务隔离是有条件的法律效果，必须分析资金来源、时间、主体和可执行财产。",
            "婚姻财富与传承规划要把投保人、被保险人、受益人和保单权益分别判断。",
            "保险、私人信托、高端医疗和境外保单是不同工具，组合前先比较法律和流动性约束。",
            "综合案例用于演练事实梳理、目标排序、工具组合和风险披露。",
        ],
        "rules": ["先识别权属和资金来源，再谈隔离。", "先核验婚姻、继承和债务事实。", "税收筹划必须以合法和现行税制为前提。", "跨境配置同时核验外汇、税务、监管和司法管辖。", "所有结论标注适用条件、证据和待复核项。"],
    },
    "large-policy-family-law": {
        "title": "大额保单配置法商攻略", "author": "沃晟学苑",
        "topics": "婚姻财富、定向传承、企业家家庭、税务、CRS和高客资产配置",
        "core": [
            "采用典型案例、专家解析、保单规划三步法处理法商场景。",
            "婚姻财富保护先辨认财产性质、出资来源、关系人和控制目标。",
            "定向传承要同时考虑受益安排、继承程序、未成年人和再婚家庭。",
            "企业家家庭需要识别家企混同、共同债务、代持、出资和担保风险。",
            "税务与CRS问题必须区分信息披露、纳税义务、外汇限制和保险功能。",
            "高客资产配置用分散、流动性和长期责任匹配来判断保险位置。",
        ],
        "rules": ["先列法律关系，再选工具。", "每个保单方案写明投保人、被保险人、受益人和资金来源。", "跨境家庭分别核验税收居民身份。", "CRS不是税种，不能与纳税义务混为一谈。", "案例结论不可脱离事实直接复用。"],
    },
    "enterprise-large-customer-sales": {
        "title": "成交高于一切：大客户销售十八招（全新修订版3.0）", "author": "孟昭春",
        "topics": "四维成交法、一网打尽、一剑封喉、关键人、销售工具和复制模式",
        "core": [
            "四维成交法用点、线、面、体分析大客户组织和多角色决策，不把单一联系人当作客户全貌。",
            "一网打尽要求画组织架构图，识别四类关键人，并利用教练理解内部决策。",
            "一剑封喉强调把句号变问号，用提问、反问和谈判控制揭示拒绝原因。",
            "战胜盲点通过人性规律、意愿图像和客户数量管理稳定销售心态。",
            "无敌工具包括六把快刀、文字行销、快速说明和忠诚客户经营。",
            "轻松复制通过客户教育、招标流程和第三方案例把个人经验变成模式。",
        ],
        "rules": ["未画出组织和影响关系前，不判断真正决策者。", "关键人态度不一致时先补关系和信息。", "陈述结束后用问题验证客户理解。", "拒绝原因未锁定前不连续说服。", "案例和权威材料只能辅助证据，不能替代客户适配性。"],
    },
    "insurance-contract-compliance": {
        "title": "法眼看保险：人身保险合同合规销售指引和实务问题精析", "author": "闫准",
        "topics": "人身保险合同法律价值、合规销售、法条、保险原则和纠纷热点",
        "core": [
            "从合同成立前的合规销售延伸到合同成立后的争议处理，形成售前、售中、售后法律视角。",
            "人身保险合同的功能必须落实到具体权利、主体、条款和适用条件，不能只讲抽象价值。",
            "四大原则包括保险利益、近因、损失补偿和最大诚信；不同原则适用边界不同。",
            "八个基础知识围绕当事人、关系人、成立生效、解除撤销和无效等合同结构展开。",
            "纠纷复核重点包括如实告知、解除权、受益与遗产、自杀故意犯罪和医疗理赔。",
            "法条、司法观点和典型案例共同构成合规依据，但历史材料必须核验时效。",
        ],
        "rules": ["销售表述必须能回到合同条款或有效法律依据。", "区分投保人、被保险人、受益人和保险人的权利义务。", "如实告知问题同时核验询问范围、事实重要性和解除期间。", "不把个案裁判结论包装成普遍保证。", "引用法条前核验现行文本、司法解释和监管规则。"],
    },
}


LEGAL_SKILLS = {
    "large-policy-closing", "large-policy-practical-guide",
    "large-policy-family-law", "insurance-contract-compliance",
}

GENERIC_HEADINGS = {
    "法条汇编", "法条解读", "典型案例", "风险案例", "风险分析", "成交攻略",
    "案例复盘", "展业心得", "复习思考题", "实训题", "专家解析", "保单规划",
}


def clean(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(" ：:；;")


def split_sentences(value: str) -> list[str]:
    return [clean(item) for item in re.split(r"(?<=[。！？；])", value) if 18 <= len(clean(item)) <= 260]


def compact(value: str, limit: int = 180) -> str:
    value = clean(value)
    value = re.sub(r"^(本书|本章|本节|作者|笔者)(认为|指出|介绍|说明|强调)?[，,:：]?", "", value)
    if len(value) > limit:
        value = value[:limit].rstrip("，,；;：:") + "。"
    return value


def normalize_term(value: str) -> str:
    value = re.sub(r"^第[一二三四五六七八九十百\d]+[章节篇]\s*", "", value)
    value = re.sub(r"^[一二三四五六七八九十\d]+[.、]\s*", "", value)
    return clean(value)


def parse_chapter(path: Path) -> tuple[str, list[dict]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    title = lines[0].lstrip("# ").strip()
    sections: list[dict] = [{"heading": title, "level": "H1", "text": []}]
    for line in lines[1:]:
        match = re.match(r"^\[(H[1-6])\]\s+(.+)$", line.strip())
        if match:
            sections.append({"heading": clean(match.group(2)), "level": match.group(1), "text": []})
        elif clean(line):
            sections[-1]["text"].append(clean(line))
    return title, sections


def useful_headings(sections: list[dict], limit: int = 10) -> list[dict]:
    result = []
    for section in sections[1:]:
        heading = section["heading"]
        if heading in GENERIC_HEADINGS or len(heading) < 3 or len(heading) > 55:
            continue
        if heading not in [item["heading"] for item in result]:
            result.append(section)
        if len(result) >= limit:
            break
    return result


def section_signal(section: dict) -> str:
    sentences = split_sentences(" ".join(section["text"]))
    scored = sorted(
        sentences,
        key=lambda s: (
            sum(key in s for key in ("第一", "第二", "分为", "包括", "是指", "意味着", "需要", "应当", "可以")),
            min(len(s), 180),
        ),
        reverse=True,
    )
    return compact(scored[0] if scored else (section["text"][0] if section["text"] else section["heading"]))


def framework_sections(sections: list[dict], concepts: list[dict]) -> list[dict]:
    markers = (
        "原则", "方法", "技巧", "步骤", "流程", "系统", "模型", "攻略", "策略", "法则",
        "管理", "规划", "架构", "分析", "训练", "沟通", "成交", "服务", "配置", "风险",
    )
    excluded = ("案例", "情景", "复习", "实训", "参考文献")
    selected = [
        section for section in concepts
        if any(key in section["heading"] for key in markers)
        and not any(key in section["heading"] for key in excluded)
    ]
    if len(selected) < 2:
        selected.extend(
            section for section in concepts
            if section not in selected and not any(key in section["heading"] for key in excluded)
        )
    return selected[:4]


def anti_patterns(sections: list[dict], frameworks: list[dict]) -> list[tuple[str, str]]:
    candidates = []
    for section in sections:
        if any(key in section["heading"] for key in ("误区", "雷区", "禁忌", "错误", "障碍", "拒绝")):
            candidates.append((section["heading"], section_signal(section)))
        if len(candidates) >= 3:
            break
    if not candidates:
        candidates = [
            (
                f"跳过“{section['heading']}”的适用条件",
                "只记结论、不核对客户事实和限制，会把方法用到错误场景。",
            )
            for section in frameworks[:3]
        ]
    return candidates


def worked_example(sections: list[dict]) -> tuple[str, list[str]] | None:
    for section in sections:
        if any(key in section["heading"] for key in ("案例", "情景", "实训")) and section["text"]:
            sentences = split_sentences(" ".join(section["text"]))[:4]
            if sentences:
                return section["heading"], [compact(s, 150) for s in sentences]
    return None


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def build_chapter(skill: str, profile: dict, number: int, source_path: Path, next_number: int | None) -> tuple[str, str, list[tuple[str, str]]]:
    title, sections = parse_chapter(source_path)
    selected = useful_headings(sections, limit=32)
    concepts = []
    for index, section in enumerate(selected[:8]):
        signal = section_signal(section)
        if signal == section["heading"] or len(signal) < len(section["heading"]) + 8:
            signal = profile["core"][(number + index - 1) % len(profile["core"])]
        concepts.append((section["heading"], signal))
    if not concepts:
        concepts = [(title, section_signal(sections[0]))]

    framework_source = framework_sections(sections, selected)
    frameworks = [(section["heading"], section_signal(section)) for section in framework_source]
    if not frameworks:
        frameworks = [(title, profile["core"][(number - 1) % len(profile["core"])])]
    negatives = anti_patterns(sections, framework_source)
    if not negatives:
        negatives = [(f"跳过“{title}”的适用条件", "只记结论、不核对客户事实和限制，会把方法用到错误场景。")]
    example = worked_example(sections)
    filename = f"ch{number:02d}-chapter-{number:02d}.md"
    concept_lines = "\n".join(f"- **{name}**：{signal}" for name, signal in concepts)
    framework_lines = "\n".join(
        f"- **{name}**：{signal}\n  - When to use：处理“{title}”相关客户事实、决策或沟通时。\n  - How：围绕“{normalize_term(name)}”整理已知事实、未知项、判断标准和下一步；重点核验上述方法的前提与限制。"
        for name, signal in frameworks
    )
    mental_lines = "\n".join(
        f"- **Use {name} when**：需要处理“{title}”中的相关判断时。把它当作信息组织模型，并用“{profile['rules'][index % len(profile['rules'])]}”校验行动。"
        for index, (name, _) in enumerate(frameworks[:3])
    )
    anti_lines = "\n".join(f"- **{name}**：{reason}" for name, reason in negatives)
    if example:
        ex_title, ex_sentences = example
        example_block = f"""## Worked Example

**场景**：{ex_title}

- **案例任务**：从该场景中分别提取当事人、关键事实、目标、约束、争议和结果，不直接复制原书结论。
- **复盘步骤**：列出当事人和事实 → 明确目标与争议 → 选择本章框架 → 核验条件与例外 → 记录结论及未决项。
- **迁移限制**：只复用分析路径，不把书中个案结论直接迁移到事实不同的客户。"""
    else:
        example_block = "## Worked Example\n\n本章没有稳定识别到独立案例标题。练习时用真实客户事实依次填写：背景、目标、约束、风险、可选动作、限制和待验证项。"
    takeaways = [(name, signal) for name, signal in frameworks[:4]]
    if len(takeaways) < 3:
        takeaways.extend(concepts[: 3 - len(takeaways)])
    takeaway_lines = "\n".join(
        f"{idx}. **{name}**：{signal}"
        for idx, (name, signal) in enumerate(takeaways[:4], start=1)
    )
    takeaway_lines += f"\n{len(takeaways[:4]) + 1}. **行动校验**：{profile['rules'][(number - 1) % len(profile['rules'])]}"
    connect = f"- **ch{next_number:02d}**：继续查看下一主要章节如何扩展或应用本章方法。" if next_number else "- **全书核心框架**：回到 SKILL.md 将本章方法放入全书工作流。"
    legal = "\n> 时效提示：涉及法律、税务、监管、跨境或保险合同效力的内容，必须在实际使用时核验现行规则。\n" if skill in LEGAL_SKILLS else ""
    body = f"""# Chapter {number}: {title}

## Core Idea

本章围绕“{title}”展开，并通过 {"、".join(name for name, _ in concepts[:4])} 建立可操作的判断入口。使用时先确认事实和目标，再核验方法的适用条件。

## Frameworks Introduced

{framework_lines}

## Key Concepts

{concept_lines}

## Mental Models

{mental_lines}

## Anti-patterns

{anti_lines}
{legal}
{example_block}

## Key Takeaways

{takeaway_lines}

## Connects To

{connect}
"""
    indexed_concepts = []
    for index, section in enumerate(selected[:12]):
        signal = section_signal(section)
        if signal == section["heading"] or len(signal) < len(section["heading"]) + 8:
            signal = profile["core"][(number + index - 1) % len(profile["core"])]
        indexed_concepts.append((section["heading"], signal))
    return filename, body, indexed_concepts or concepts


def build_supporting_files(out: Path, profile: dict, topics: dict[str, dict]) -> None:
    glossary: dict[str, dict] = {}
    excluded = ("案例", "情景", "复习", "实训", "基本概念", "展业心得")
    for raw_term, item in topics.items():
        term = normalize_term(raw_term)
        if len(term) < 3 or len(term) > 36 or any(key in term for key in excluded):
            continue
        current = glossary.setdefault(term, {"refs": set(), "definition": item["definition"]})
        current["refs"].update(item["refs"])
        if len(item["definition"]) > len(current["definition"]):
            current["definition"] = item["definition"]
    glossary_lines = []
    for term in sorted(glossary)[:40]:
        refs = ", ".join(f"Ch {n}" for n in sorted(glossary[term]["refs"]))
        glossary_lines.append(f"**{term}** — {glossary[term]['definition']}（{refs}）")
    write(out / "glossary.md", "# Glossary\n\n" + "\n\n".join(glossary_lines))

    pattern_blocks = []
    for index, core in enumerate(profile["core"][:6], start=1):
        pattern_blocks.append(
            f"## Pattern {index}: {core.split('，')[0].rstrip('。')}\n"
            f"**When to use**: 当前任务与“{profile['topics']}”相关，且需要形成可执行判断时。\n"
            f"**How**: {core} 记录事实、判断依据、适用条件、风险和下一步。\n"
            "**Trade-offs**: 方法能提高一致性，但不能替代客户事实核验、合同审阅和专业复核。"
        )
    write(out / "patterns.md", "# Patterns\n\n" + "\n\n".join(pattern_blocks))

    rules = "\n".join(f"| {i} | {rule} |" for i, rule in enumerate(profile["rules"], start=1))
    write(out / "cheatsheet.md", f"""# Cheatsheet

## Decision Rules

| # | Rule |
|---|---|
{rules}

## Universal Check

事实是否完整 → 目标是否明确 → 方法是否适用 → 合同/规则是否核验 → 风险是否披露 → 下一步是否记录。

## Red Flags

- 用绝对化语言承诺收益、隔离、税务或理赔结果。
- 把客户标签、案例或话术当成事实结论。
- 未核验关系人、资金来源、期限、流动性和免责限制。
""")


def build_skill(corpus_dir: Path, destination: Path, skill: str, profile: dict) -> dict:
    meta = json.loads((corpus_dir / skill / "metadata.json").read_text(encoding="utf-8"))
    out = destination / skill
    if out.exists():
        shutil.rmtree(out)
    (out / "chapters").mkdir(parents=True)

    chapter_rows = []
    topics: dict[str, dict] = {}
    for chapter in meta["chapters"]:
        number = chapter["number"]
        next_number = number + 1 if number < len(meta["chapters"]) else None
        filename, body, concepts = build_chapter(
            skill, profile, number, corpus_dir / skill / chapter["filename"], next_number
        )
        write(out / "chapters" / filename, body)
        for term, definition in concepts:
            item = topics.setdefault(term, {"refs": set(), "definition": definition})
            item["refs"].add(number)
            if len(definition) > len(item["definition"]):
                item["definition"] = definition
        chapter_rows.append((number, chapter["title"], filename, [term for term, _ in concepts[:3]]))

    build_supporting_files(out, profile, topics)
    pages = max(1, round(meta["total_chars"] / 700))
    core_lines = "\n".join(f"- {item}" for item in profile["core"])
    index_rows = "\n".join(
        f"| [ch{number:02d}](chapters/{filename}) | {title} | {', '.join(terms)} |"
        for number, title, filename, terms in chapter_rows
    )
    topic_lines = "\n".join(
        f"- **{term}** → " + ", ".join(f"ch{number:02d}" for number in sorted(item["refs"]))
        for term, item in sorted(topics.items())[:60]
    )
    scope = (
        "本 skill 提取自书籍内容，仅用于知识检索、学习和内部工作辅助。涉及法律、税务、监管、跨境或合同效力时，必须核验现行规则并由相应专业人员复核。"
        if skill in LEGAL_SKILLS else
        "本 skill 提取自书籍内容，用于学习和工作辅助；实际保险建议仍须结合客户事实、保险合同、公司制度和现行监管要求。"
    )
    master = f"""---
name: {skill}
description: "Knowledge base from '{profile['title']}' by {profile['author']}. Use for {profile['topics']}."
---

<!-- argument-hint: [topic, framework name, customer scenario, or chapter number] -->

# {profile['title']}
**Author**: {profile['author']} | **Pages**: ~{pages} | **Chapters**: {len(chapter_rows)} | **Generated**: 2026-07-14

## How to Use This Skill

- 不带参数：加载下方核心框架。
- 指定主题或客户场景：先查 Topic Index，再读取对应章节。
- 指定 `chNN`：直接读取该章节的框架、概念、反模式和案例。
- 回答时区分书中方法、客户事实、当前规则和推断。

## Core Frameworks & Mental Models

{core_lines}

## Chapter Index

| # | Title | Key Frameworks |
|---|---|---|
{index_rows}

## Topic Index

{topic_lines}

## Supporting Files

- [glossary.md](glossary.md) — 术语及章节引用
- [patterns.md](patterns.md) — 本书方法模式
- [cheatsheet.md](cheatsheet.md) — 决策规则和风险提示

## Scope & Limits

{scope}
"""
    write(out / "SKILL.md", master)
    return {"skill": skill, "chapters": len(chapter_rows), "pages": pages, "topics": min(60, len(topics))}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus_dir", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)

    results = []
    for skill, profile in PROFILES.items():
        results.append(build_skill(args.corpus_dir, args.destination, skill, profile))

    readme_rows = "\n".join(
        f"| [{item['skill']}]({item['skill']}/SKILL.md) | {PROFILES[item['skill']]['title']} | {item['chapters']} | {item['topics']} |"
        for item in results
    )
    write(args.destination / "README.md", f"""# 保险书籍知识 Skills

12 本保险与销售书籍按 `book-to-skill` 结构分别转换。每个 skill 包含主索引、按需加载章节、术语表、方法模式和决策速查表。

| Skill | Book | Chapters | Indexed Topics |
|---|---|---:|---:|
{readme_rows}

来源文件保留在上一级目录。涉及法律、税务、监管和合同结论时，须核验现行规则。
""")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
