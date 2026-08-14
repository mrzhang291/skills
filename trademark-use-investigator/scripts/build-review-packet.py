#!/usr/bin/env python3

"""Build a small, local review packet from accepted candidate-page text.

The packet keeps the Agent from loading whole HTML/MHTML files or long body
text into its context.  It performs no browser or network activity.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import unicodedata


def read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Required JSON file not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def configured_terms(config: dict) -> list[str]:
    trademark = config.get("trademark") or {}
    values = [
        trademark.get("registration_number"), trademark.get("owner"),
        trademark.get("name"), *(trademark.get("goods_services") or []),
        "价格", "购买", "店铺", "包装", "厂家", "销售", "商品", "产品",
    ]
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = clean(value)
        key = term.casefold()
        if len(term) >= 2 and key not in seen:
            seen.add(key)
            output.append(term)
    return output


def merge_ranges(ranges: list[tuple[int, int]], gap: int = 80) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + gap:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def excerpts(text: str, terms: list[str], max_chars: int) -> tuple[list[str], list[str]]:
    compact = clean(text)
    folded = compact.casefold()
    matched: list[str] = []
    ranges: list[tuple[int, int]] = []
    for term in terms:
        needle = term.casefold()
        position = folded.find(needle)
        if position < 0:
            continue
        matched.append(term)
        ranges.append((max(0, position - 180), min(len(compact), position + len(term) + 320)))
    if not ranges and compact:
        ranges = [(0, min(len(compact), max_chars))]
    snippets: list[str] = []
    remaining = max_chars
    for start, end in merge_ranges(ranges):
        if remaining <= 0:
            break
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(compact) else ""
        available = max(0, remaining - len(prefix) - len(suffix))
        value = prefix + compact[start:end][:available] + suffix
        snippets.append(value)
        remaining -= len(value)
    return matched, snippets


def build(run_dir: Path, max_chars_per_candidate: int) -> tuple[Path, dict]:
    run_dir = run_dir.resolve()
    config = read_json(run_dir / "run-config.json")
    terms = configured_terms(config)
    items = []
    skipped = []
    for metadata_path in sorted((run_dir / "candidate-pages").glob("*/metadata.json")):
        metadata = read_json(metadata_path)
        candidate_id = str(metadata.get("candidate_id") or metadata_path.parent.name)
        if metadata.get("candidate_accepted") is not True:
            skipped.append({"candidate_id": candidate_id, "reason": "not_accepted"})
            continue
        body_record = (metadata.get("artifacts") or {}).get("body_text") or {}
        body_path = (metadata_path.parent / str(body_record.get("path") or "body-text.txt")).resolve()
        if not body_path.is_relative_to(metadata_path.parent.resolve()) or not body_path.is_file():
            skipped.append({"candidate_id": candidate_id, "reason": "body_text_missing"})
            continue
        body = body_path.read_text(encoding="utf-8", errors="replace")
        matched, snippets = excerpts(body, terms, max_chars_per_candidate)
        items.append({
            "candidate_id": candidate_id,
            "title": metadata.get("title"),
            "url": metadata.get("final_url") or metadata.get("normalized_url"),
            "page_type": metadata.get("page_type"),
            "page_state": metadata.get("page_state"),
            "text_chars": metadata.get("text_chars"),
            "main_content_chars": metadata.get("main_content_chars"),
            "substantive_image_count": metadata.get("substantive_image_count"),
            "commercial_signals": metadata.get("commercial_signals") or [],
            "matched_terms": matched,
            "excerpts": snippets,
            "excerpt_chars": sum(len(item) for item in snippets),
            "full_text_loaded_by_agent": False,
        })
    output = {
        "schema_version": "1.0",
        "record_type": "quick_candidate_review_packet",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": config.get("run_id"),
        "execution_profile": config.get("execution_profile") or "forensic",
        "network_used": False,
        "browser_used": False,
        "max_chars_per_candidate": max_chars_per_candidate,
        "candidate_count": len(items),
        "items": items,
        "skipped": skipped,
    }
    output_path = run_dir / "candidate-review-packet.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path, output


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a bounded local candidate review packet")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--max-chars-per-candidate", type=int, default=3000)
    args = parser.parse_args()
    if not 500 <= args.max_chars_per_candidate <= 10000:
        parser.error("--max-chars-per-candidate must be between 500 and 10000")
    output_path, output = build(Path(args.run_dir), args.max_chars_per_candidate)
    print(json.dumps({
        "created": True,
        "output": str(output_path),
        "candidate_count": output["candidate_count"],
        "network_used": False,
        "browser_used": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
