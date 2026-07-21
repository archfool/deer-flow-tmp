#!/usr/bin/env python3
"""Validate generated book skills for structure, navigation, and obvious template failures."""

from __future__ import annotations

import argparse
import hashlib
import re
from collections import Counter
from pathlib import Path


REQUIRED_SECTIONS = (
    "## Core Idea",
    "## Frameworks Introduced",
    "## Key Concepts",
    "## Mental Models",
    "## Anti-patterns",
    "## Worked Example",
    "## Key Takeaways",
    "## Connects To",
)

REQUIRED_FRAMEWORKS = {
    "family-insurance-quickstart": ("买前策略", "买中策略", "买后策略"),
    "professional-insurance-sales": ("客户投保行为", "保险建议书", "异议处理"),
    "insurance-precision-marketing": ("认识客户", "展业流程", "案例复盘", "KYC"),
    "insurance-sales-scenarios": ("电话约访", "产品异议", "需求异议", "售后服务"),
    "insurance-sales-playbook": ("拜访准备", "异议解决四步走", "促成", "售后服务"),
    "insurance-sales-objection-handling": ("保险偏见", "保险公司的误解", "支付", "促成"),
    "big-policy-sales": ("八大系统", "知信行", "客户服务“四现”", "服务的3个层次"),
    "large-policy-closing": ("家企隔离", "保险金信托", "婚姻财富", "家族财富传承"),
    "large-policy-practical-guide": ("债务隔离", "婚姻财富管理", "私人信托", "境外大额保单"),
    "large-policy-family-law": ("婚姻财富保护", "定向传承", "CRS", "资产配置"),
    "enterprise-large-customer-sales": ("四维成交法", "一网打尽", "一剑封喉", "六把快刀"),
    "insurance-contract-compliance": ("保险利益原则", "近因原则", "损失补偿原则", "最大诚信原则", "两年不可抗辩"),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("skills_dir", type=Path)
    args = parser.parse_args()

    errors: list[str] = []
    warnings: list[str] = []
    skill_dirs = sorted(path for path in args.skills_dir.iterdir() if path.is_dir())
    chapter_hashes = Counter()
    total_chapters = 0
    total_routes = 0

    for skill_dir in skill_dirs:
        master = skill_dir / "SKILL.md"
        if not master.exists():
            errors.append(f"{skill_dir.name}: missing SKILL.md")
            continue
        master_text = master.read_text(encoding="utf-8")
        name_match = re.search(r"^name:\s*(.+)$", master_text, re.MULTILINE)
        if not name_match or name_match.group(1).strip() != skill_dir.name:
            errors.append(f"{skill_dir.name}: frontmatter name mismatch")
        if len(master_text) > 14000:
            warnings.append(f"{skill_dir.name}: SKILL.md may exceed 4,000 tokens")

        routes = re.findall(r"^- \*\*(.+?)\*\* → (ch\d+(?:, ch\d+)*)$", master_text, re.MULTILINE)
        total_routes += len(routes)
        if not routes:
            errors.append(f"{skill_dir.name}: no topic routes")

        for link in re.findall(r"\]\((chapters/[^)]+\.md)\)", master_text):
            if not (skill_dir / link).exists():
                errors.append(f"{skill_dir.name}: broken chapter link {link}")

        for support in ("glossary.md", "patterns.md", "cheatsheet.md"):
            path = skill_dir / support
            if not path.exists() or path.stat().st_size < 200:
                errors.append(f"{skill_dir.name}: missing or empty {support}")

        complete_text = "\n".join(path.read_text(encoding="utf-8") for path in skill_dir.rglob("*.md"))
        for framework in REQUIRED_FRAMEWORKS.get(skill_dir.name, ()):
            if framework not in complete_text:
                errors.append(f"{skill_dir.name}: missing required framework {framework}")

        chapters = sorted((skill_dir / "chapters").glob("*.md"))
        total_chapters += len(chapters)
        if not chapters:
            errors.append(f"{skill_dir.name}: no chapter files")
        for chapter in chapters:
            text = chapter.read_text(encoding="utf-8")
            for heading in REQUIRED_SECTIONS:
                if heading not in text:
                    errors.append(f"{chapter}: missing {heading}")
            if len(text) < 600:
                warnings.append(f"{chapter}: unusually thin ({len(text)} chars)")
            if max((len(line) for line in text.splitlines()), default=0) > 420:
                warnings.append(f"{chapter}: contains an overlong line")
            chapter_hashes[hashlib.sha256(text.encode("utf-8")).hexdigest()] += 1

    duplicate_chapters = sum(count - 1 for count in chapter_hashes.values() if count > 1)
    if duplicate_chapters:
        errors.append(f"duplicate chapter files: {duplicate_chapters}")

    print(f"skills={len(skill_dirs)} chapters={total_chapters} topic_routes={total_routes}")
    print(f"errors={len(errors)} warnings={len(warnings)} duplicate_chapters={duplicate_chapters}")
    for item in errors:
        print(f"ERROR: {item}")
    for item in warnings:
        print(f"WARN: {item}")
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
