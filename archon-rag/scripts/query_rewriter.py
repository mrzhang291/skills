"""Rule-based query expansion and citation extraction."""

from __future__ import annotations

import re

SYNONYMS = {
    "cod": ["化学需氧量", "chemical oxygen demand"],
    "化学需氧量": ["cod", "chemical oxygen demand"],
    "厌氧": ["anaerobic", "uasb", "egsb"],
    "好氧": ["aerobic", "activated sludge"],
    "去除率": ["removal", "efficiency", "removal efficiency"],
    "工艺": ["process", "route", "method"],
    "客户": ["client", "甲方"],
    "甲方": ["client", "客户"],
    "项目": ["project"],
    "目标": ["objective", "goal"],
    "结论": ["conclusion", "summary"],
    "建议": ["recommendation", "suggestion"],
}


def expand_query(query: str, max_variants: int = 5) -> list:
    """Return the original query plus useful synonym expansions."""
    variants = [query]
    lowered = query.lower()
    for term, replacements in SYNONYMS.items():
        if term.lower() in lowered:
            for replacement in replacements[:2]:
                variant = query
                # Replace whole-word-ish occurrences, preserving original case.
                if term.isascii():
                    variant = re.sub(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", replacement, variant, flags=re.IGNORECASE)
                else:
                    variant = variant.replace(term, replacement)
                if variant != query:
                    variants.append(variant)
    seen = []
    for v in variants:
        if v not in seen:
            seen.append(v)
    return seen[:max_variants]


def build_citations(records: list, max_citations: int = 5) -> list:
    """Build lightweight source citations from search records."""
    citations = []
    for record in records[:max_citations]:
        content = record.get("content") or record.get("full_text") or ""
        heading = "Unknown section"
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()
                break
        quote = " ".join(content.split())[:200]
        citations.append({
            "source_filename": record.get("source_filename") or record.get("filename", ""),
            "section": heading,
            "quote": quote,
        })
    return citations
