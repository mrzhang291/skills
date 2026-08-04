"""Validation gates for scan output and finalize input."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SCAN_REQUIRED_FIELDS = [
    "tags",
    "summary",
    "entities",
    "key_data",
    "client_name",
    "project_name",
    "product_capacity",
    "quality_summary",
    "objective",
    "process",
]


def _load_chapter_rules(profile: str | None = None) -> dict:
    try:
        scripts_dir = Path(__file__).resolve().parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import archon_config
        return archon_config.chapter_rules(profile)
    except Exception:
        return {}


def _missing(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _excluded_headings(structured: dict, profile: str | None = None) -> list:
    rules = _load_chapter_rules(profile)
    chunks = structured.get("_docling_chunks") or []
    excluded = []
    for item in rules.get("exclude", []):
        for alias in item.get("aliases", []):
            alias_clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", alias).lower()
            if not alias_clean:
                continue
            for chunk in chunks:
                heading = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(chunk.get("heading", ""))).lower()
                if alias_clean in heading:
                    excluded.append(chunk.get("heading", ""))
    return sorted(set(excluded))


def validate_scan_payload(structured: dict, profile: str | None = None) -> dict:
    """Validate AI scan output before it is written as a structured result."""
    errors = []
    for field in SCAN_REQUIRED_FIELDS:
        if field not in structured or _missing(structured.get(field)):
            errors.append(f"missing_field:{field}")

    chunks = structured.get("_docling_chunks") or []
    if not chunks and not structured.get("fragments") and not structured.get("full_text"):
        errors.append("missing_source_content")

    excluded = _excluded_headings(structured, profile)
    if excluded:
        errors.append(f"excluded_chapters:{','.join(excluded[:5])}")

    return {
        "valid": not errors,
        "errors": errors,
        "excluded_headings": excluded,
    }


def validate_finalize_payload(structured: dict, profile: str | None = None) -> dict:
    """Validate a structured result before encryption/indexing."""
    errors = []
    for field in SCAN_REQUIRED_FIELDS:
        if field not in structured or _missing(structured.get(field)):
            errors.append(f"missing_field:{field}")

    chunks = structured.get("_docling_chunks") or []
    if not chunks and not structured.get("full_text"):
        errors.append("missing_source_content")

    excluded = _excluded_headings(structured, profile)
    if excluded:
        errors.append(f"excluded_chapters:{','.join(excluded[:5])}")

    return {
        "valid": not errors,
        "errors": errors,
        "excluded_headings": excluded,
    }
