"""Deterministic incremental Wiki compiler for Archon RAG."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


def _safe_slug(filename: str) -> str:
    stem = Path(filename).stem
    slug = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff-]", "_", stem)[:60]
    return slug or "document"


def _wiki_root(base_dir: str) -> str:
    kr_dir = os.environ.get("KNOWLEDGE_RAG_DIR", os.path.join(base_dir, "shared", "knowledge_rag"))
    return os.path.normpath(os.path.join(kr_dir, "..", "wiki"))


def _ensure_dirs(base_dir: str, department: str) -> dict:
    root = _wiki_root(base_dir)
    dept = os.path.join(root, department)
    paths = {
        "dept": dept,
        "summaries": os.path.join(dept, "summaries"),
        "concepts": os.path.join(dept, "concepts"),
        "entities": os.path.join(dept, "entities"),
    }
    for path in paths.values():
        os.makedirs(path, exist_ok=True)
    return paths


def _append_reference(path: str, doc_slug: str, summary: str, key_data: dict) -> None:
    line = f"- [{doc_slug}](../summaries/{doc_slug}.md) - {summary[:120]}"
    if key_data:
        line += f" | {json.dumps(key_data, ensure_ascii=False)[:120]}"
    line += "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def compile_auto_wiki(base_dir: str, department: str, structured: dict, record_id: str = "") -> dict:
    """Write/update summaries, index, concepts, and entities for one document."""
    paths = _ensure_dirs(base_dir, department)
    filename = structured.get("filename", "unknown")
    slug = _safe_slug(filename)
    summary = structured.get("summary", "")
    tags = structured.get("tags", [])
    entities = structured.get("entities", [])
    key_data = structured.get("key_data", {})
    chunks = structured.get("_docling_chunks") or []

    summary_path = os.path.join(paths["summaries"], f"{slug}.md")
    lines = [
        f"# {filename}",
        "",
        f"**摘要**: {summary}",
        f"**甲方**: {structured.get('client_name', '')}",
        f"**项目名称**: {structured.get('project_name', '')}",
        f"**产品/产能**: {structured.get('product_capacity', '')}",
        f"**质量/规格**: {structured.get('quality_summary', '')}",
        f"**目标**: {structured.get('objective', '')}",
        f"**流程/工艺**: {structured.get('process', '')}",
        "",
        "## 章节",
        "",
    ]
    for chunk in chunks:
        heading = chunk.get("heading", "")
        content = " ".join(str(chunk.get("content", "")).split())[:300]
        lines.append(f"### {heading}")
        lines.append(content)
        lines.append("")
    if key_data:
        lines.append("## 关键数据")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---|")
        for k, v in key_data.items():
            lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append(f"<!-- auto-generated record_id: {record_id} -->")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    index_path = os.path.join(paths["dept"], "index.md")
    if not os.path.exists(index_path):
        with open(index_path, "w", encoding="utf-8") as f:
            f.write("# 文档索引\n\n| 文档 | 甲方 | 项目 | 摘要 |\n|---|---|---|---|\n")
    with open(index_path, "a", encoding="utf-8") as f:
        f.write(
            f"| [{filename}](summaries/{slug}.md) | {structured.get('client_name', '')} | "
            f"{structured.get('project_name', '')} | {summary[:100]} |\n"
        )

    for tag in tags:
        tag_slug = _safe_slug(tag)
        _append_reference(os.path.join(paths["concepts"], f"{tag_slug}.md"), slug, summary, key_data)
    for entity in entities:
        entity_slug = _safe_slug(entity)
        _append_reference(os.path.join(paths["entities"], f"{entity_slug}.md"), slug, summary, key_data)

    return {"status": "ok", "doc_slug": slug, "summary_path": summary_path, "index_path": index_path}
