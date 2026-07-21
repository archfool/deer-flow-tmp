from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


REQUIRED = [
    "Core Idea",
    "Frameworks Introduced",
    "Key Concepts",
    "Mental Models",
    "Anti-patterns",
    "Worked Example",
    "Key Takeaways",
    "Connects To",
]

FORBIDDEN = [
    "本章没有稳定识别到独立案例标题",
    "整理已知事实、未知项、判断标准和下一步",
    "只记结论、不核对客户事实和限制",
    "从该场景中分别提取当事人",
    "按本章语境识别并以客户事实验证",
    "继续查看下一主要章节如何扩展或应用本章方法",
]


def section(text: str, name: str) -> str:
    match = re.search(
        rf"^## {re.escape(name)}\s*$\n(.*?)(?=^## |\Z)",
        text,
        flags=re.M | re.S,
    )
    return match.group(1).strip() if match else ""


def compact_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def main(root: Path) -> int:
    errors: list[str] = []
    warnings: list[str] = []
    line_owners: dict[str, set[str]] = defaultdict(set)
    skill_dirs = sorted(path for path in root.iterdir() if path.is_dir())
    chapter_count = 0

    for skill in skill_dirs:
        master = skill / "SKILL.md"
        if not master.exists():
            errors.append(f"{skill.name}: missing SKILL.md")
            continue

        master_text = master.read_text(encoding="utf-8")
        core = section(master_text, "Core Frameworks & Mental Models")
        if compact_len(core) < 260:
            errors.append(f"{skill.name}: master core too shallow ({compact_len(core)} chars)")

        for support in ("glossary.md", "patterns.md", "cheatsheet.md"):
            path = skill / support
            if not path.exists() or compact_len(path.read_text(encoding="utf-8")) < 180:
                errors.append(f"{skill.name}: missing or shallow {support}")

        chapters = sorted((skill / "chapters").glob("ch*.md"))
        if not chapters:
            errors.append(f"{skill.name}: no chapters")

        for path in chapters:
            chapter_count += 1
            text = path.read_text(encoding="utf-8")
            owner = f"{skill.name}/{path.name}"

            for name in REQUIRED:
                body = section(text, name)
                if not body:
                    errors.append(f"{owner}: missing {name}")

            minima = {
                "Core Idea": 40,
                "Frameworks Introduced": 90,
                "Key Concepts": 55,
                "Mental Models": 48,
                "Anti-patterns": 28,
                "Worked Example": 28,
                "Key Takeaways": 55,
            }
            for name, minimum in minima.items():
                size = compact_len(section(text, name))
                if size < minimum:
                    errors.append(f"{owner}: shallow {name} ({size} chars)")

            for phrase in FORBIDDEN:
                if phrase in text:
                    errors.append(f"{owner}: forbidden template phrase: {phrase}")

            example = section(text, "Worked Example")
            if not re.search(
                r"客户|家庭|企业|销售|顾问|投保|项目|保单|年轻人|白领|老人|家族|团队|申请人|被保险人",
                example,
            ):
                warnings.append(f"{owner}: worked example may lack a concrete actor")

            for raw in text.splitlines():
                line = re.sub(r"\s+", " ", raw.strip())
                if len(line) < 42 or line.startswith("#") or line.startswith("- **ch"):
                    continue
                line_owners[line].add(owner)

    for line, owners in line_owners.items():
        if len(owners) >= 10:
            warnings.append(
                f"long line repeated in {len(owners)} chapters: {line[:100]}"
            )

    if len(skill_dirs) != 12:
        errors.append(f"expected 12 skills, found {len(skill_dirs)}")
    if chapter_count != 112:
        errors.append(f"expected 112 chapters, found {chapter_count}")

    print(f"skills={len(skill_dirs)} chapters={chapter_count}")
    print(f"semantic_errors={len(errors)} semantic_warnings={len(warnings)}")
    for item in errors:
        print(f"ERROR {item}")
    for item in warnings:
        print(f"WARN {item}")
    return 1 if errors else 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("保险书籍知识提取/skills")
    raise SystemExit(main(target))
