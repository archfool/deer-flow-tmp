#!/usr/bin/env python3
"""Extract chapter-oriented plain text from the WeRead-style HTML sources."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from bs4 import BeautifulSoup


BOOK_CONFIG = {
    "1小时搞定全家保险.html": ("family-insurance-quickstart", "h2", r"^第[一二三四五六七八九十]+章"),
    "保险专业销售技术.html": ("professional-insurance-sales", "h1", r"^第\d+章"),
    "保险精准营销.html": ("insurance-precision-marketing", "h1", r"^第\d+章"),
    "保险这样卖才对：保险销售人员超级情景训练.html": ("insurance-sales-scenarios", "h1", r"^第[一二三四五六七八九十]+章"),
    "保险销售一本就够：基础知识+销售话术+实战技巧+成功案例.html": ("insurance-sales-playbook", "h1", r"^第[一二三四五六七八九十]+章"),
    "保险销售实战口才训练.html": ("insurance-sales-objection-handling", "h1", r"^第[一二三四五六七八九十]+章"),
    "大保单销售.html": ("big-policy-sales", "h2", r".+"),
    "大额保单成交攻略.html": ("large-policy-closing", "h1", r"^第\d+章"),
    "大额保单操作实务.html": ("large-policy-practical-guide", "h1", r"^第[一二三四五六七八九十]+章"),
    "大额保单配置法商攻略.html": ("large-policy-family-law", "h1", r"^第[一二三四五六七八九十]+章"),
    "成交高于一切：大客户销售十八招（全新修订版3.0）.html": ("enterprise-large-customer-sales", "h1", r"^第[一二三四五六]+篇"),
    "法眼看保险：人身保险合同合规销售指引和实务问题精析.html": ("insurance-contract-compliance", "h2", r"^第[一二三四五六七八九十]+章"),
}


@dataclass
class ChapterMeta:
    number: int
    title: str
    filename: str
    chars: int
    headings: list[str]


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_book(source: Path, output_root: Path) -> dict:
    skill_name, chapter_tag, chapter_pattern = BOOK_CONFIG[source.name]
    soup = BeautifulSoup(source.read_text(encoding="utf-8", errors="ignore"), "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    out = output_root / skill_name
    out.mkdir(parents=True, exist_ok=True)
    chapters: list[dict] = []
    current: dict | None = None

    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"]):
        text = clean_text(node.get_text(" ", strip=True))
        if not text:
            continue

        if node.name == chapter_tag and re.search(chapter_pattern, text):
            current = {"title": text, "blocks": [], "headings": []}
            chapters.append(current)
            continue

        if current is None:
            continue

        if node.name and node.name.startswith("h"):
            if text not in current["headings"]:
                current["headings"].append(text)
            current["blocks"].append(f"\n[{node.name.upper()}] {text}\n")
        else:
            current["blocks"].append(text)

    # The source uses many h2 sections for this book. Merge thin sections into
    # practical groups later; retain the exact source boundaries here.
    metadata: list[ChapterMeta] = []
    for index, chapter in enumerate(chapters, start=1):
        filename = f"ch{index:02d}.txt"
        body = f"# {chapter['title']}\n\n" + "\n".join(chapter["blocks"])
        (out / filename).write_text(body, encoding="utf-8")
        metadata.append(
            ChapterMeta(
                number=index,
                title=chapter["title"],
                filename=filename,
                chars=len(body),
                headings=chapter["headings"],
            )
        )

    book_meta = {
        "source": source.name,
        "skill_name": skill_name,
        "chapter_tag": chapter_tag,
        "chapter_pattern": chapter_pattern,
        "chapters": [asdict(item) for item in metadata],
        "total_chars": sum(item.chars for item in metadata),
    }
    (out / "metadata.json").write_text(
        json.dumps(book_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return book_meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    if args.output_dir.exists():
        shutil.rmtree(args.output_dir)
    args.output_dir.mkdir(parents=True)

    books = []
    for filename in BOOK_CONFIG:
        source = args.source_dir / filename
        if not source.exists():
            raise FileNotFoundError(source)
        books.append(extract_book(source, args.output_dir))

    summary = {"books": books, "total_chars": sum(book["total_chars"] for book in books)}
    (args.output_dir / "metadata.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
