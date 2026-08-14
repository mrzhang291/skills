#!/usr/bin/env python3

from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from discovery_limits import (
    cap_query_results, positive_budget, remove_provider_results, validate_query_slot,
)
from provenance_utils import file_sha256, validate_provider
from url_utils import (
    hostname, is_search_result_url, normalize_url, require_safe_file_id, site_key, target_id,
)


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")


def collect(value, output: list[dict]) -> None:
    if isinstance(value, dict):
        url = value.get("url") or value.get("link")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            output.append({
                "url": url, "title": str(value.get("title") or value.get("name") or ""),
                "snippet": str(value.get("snippet") or value.get("description") or "")[:2000],
            })
        for child in value.values():
            collect(child, output)
    elif isinstance(value, list):
        for child in value:
            collect(child, output)


def terms(query: str) -> list[str]:
    value = re.sub(r'["“”\'‘’]', " ", query or "")
    return [item.casefold() for item in re.split(r"[\s,+|/]+", value) if len(item.strip()) >= 2]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import an actual structured search-tool response into the URL frontier")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument(
        "--source-kind", required=True,
        choices=["cherrystudio_export", "search_api_export", "self_test_fixture"],
        help="Recorded acquisition mode; it does not make an unknown provider trusted",
    )
    parser.add_argument("--provider-instance-id")
    parser.add_argument("--raw-file", required=True, help="Exact JSON response saved inside RUN_DIR/discovery/raw")
    parser.add_argument("--allow-zero-results", action="store_true")
    args = parser.parse_args()

    args.query_id = require_safe_file_id(args.query_id, "query-id")
    run_dir = Path(args.run_dir).resolve()
    config_path = run_dir / "run-config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"run-config.json not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    validate_query_slot(
        config,
        read_jsonl(run_dir / "discovery" / "queries.jsonl"),
        args.query_id,
        args.query,
    )
    args.provider, provider_key = validate_provider(
        args.provider,
        source_kind=args.source_kind,
        test_mode=config.get("test_mode") is True,
        provider_instance_id=args.provider_instance_id,
    )
    max_results = positive_budget(config, "max_results_per_query", 20)
    raw_file = Path(args.raw_file).resolve()
    raw_root = (run_dir / "discovery" / "raw").resolve()
    if not raw_file.is_relative_to(raw_root) or not raw_file.is_file():
        raise ValueError("raw-file must be an existing JSON file inside RUN_DIR/discovery/raw")
    payload = json.loads(raw_file.read_text(encoding="utf-8"))
    raw_sha256 = file_sha256(raw_file)
    found: list[dict] = []
    collect(payload, found)
    query_terms = terms(args.query)
    accepted = []
    seen = set()
    for item in found:
        normalized = normalize_url(item["url"])
        sample = f"{item.get('title')}\n{item.get('snippet')}\n{normalized or ''}".casefold()
        if not normalized or normalized in seen or is_search_result_url(normalized):
            continue
        if query_terms and not any(term in sample for term in query_terms):
            continue
        seen.add(normalized)
        accepted.append((item, normalized))
    if not accepted and not args.allow_zero_results:
        raise ValueError("Structured response contains no relevant non-search destination URLs")

    discovery_dir = run_dir / "discovery"
    now = datetime.now(timezone.utc).isoformat()
    prior_results = read_jsonl(discovery_dir / "results.jsonl")
    prior_results = remove_provider_results(prior_results, args.query_id, args.provider, provider_key)
    imported_results = []
    for rank, (item, normalized) in enumerate(accepted, start=1):
        imported_results.append({
            "schema_version": "2.0", "record_type": "discovered_target_url",
            "discovery_id": f"{args.query_id}-{args.provider.upper()}-{rank:03d}",
            "query_id": args.query_id, "query": args.query, "provider": args.provider,
            "provider_key": provider_key, "provider_instance_id": args.provider_instance_id,
            "source_kind": args.source_kind, "raw_sha256": raw_sha256, "rank": rank,
            "title": item.get("title", "")[:500], "snippet": item.get("snippet", "")[:2000],
            "result_url": item["url"], "normalized_url": normalized, "target_id": target_id(normalized),
            "domain": hostname(normalized), "site_key": site_key(normalized), "discovered_at": now,
        })
    other_queries = [item for item in prior_results if item.get("query_id") != args.query_id]
    query_candidates = [item for item in prior_results if item.get("query_id") == args.query_id]
    query_candidates.extend(imported_results)
    retained_query_results = cap_query_results(query_candidates, max_results)
    results = other_queries + retained_query_results
    results.sort(key=lambda item: (item.get("query_id", ""), item.get("provider", ""), item.get("rank", 0)))
    write_jsonl(discovery_dir / "results.jsonl", results)

    retained_counts = Counter(item.get("provider_key") for item in retained_query_results)
    query_records = read_jsonl(discovery_dir / "queries.jsonl")
    query_records = [item for item in query_records if not (
        item.get("query_id") == args.query_id and item.get("provider_key") == provider_key
    )]
    query_records.append({
        "schema_version": "2.0", "query_id": args.query_id, "query": args.query,
        "provider": args.provider, "provider_key": provider_key,
        "provider_instance_id": args.provider_instance_id, "source_kind": args.source_kind,
        "executed_at": now,
        "state": "normal" if accepted else "zero_results",
        "result_count": retained_counts.get(provider_key, 0),
        "extracted_result_count": len(accepted),
        "max_results_per_query": max_results,
        "budget_limited": retained_counts.get(provider_key, 0) < len(accepted),
        "raw_file": str(raw_file.relative_to(run_dir)).replace("\\", "/"),
        "raw_sha256": raw_sha256,
        "diagnostic_dir": None, "errors": [], "imported_structured_response": True,
    })
    for record in query_records:
        if record.get("query_id") == args.query_id:
            record["result_count"] = retained_counts.get(record.get("provider_key"), 0)
            record["max_results_per_query"] = max_results
            if record.get("extracted_result_count") is not None:
                record["budget_limited"] = record["result_count"] < int(record.get("extracted_result_count") or 0)
    query_records.sort(key=lambda item: (item.get("query_id", ""), item.get("provider", "")))
    write_jsonl(discovery_dir / "queries.jsonl", query_records)

    grouped = {}
    for item in results:
        url = item["normalized_url"]
        entry = grouped.setdefault(url, {
            "target_id": target_id(url), "normalized_url": url, "domain": hostname(url),
            "site_key": site_key(url), "title": item.get("title"), "status": "pending",
            "discovered_via": [],
        })
        entry["discovered_via"].append({
            "query_id": item.get("query_id"), "query": item.get("query"),
            "provider": item.get("provider"), "provider_key": item.get("provider_key"),
            "rank": item.get("rank"), "discovery_id": item.get("discovery_id"),
            "raw_sha256": item.get("raw_sha256"),
        })
        entry["discovered_via"].extend(item.get("additional_discoveries") or [])
    frontier = {
        "schema_version": "2.0", "updated_at": now, "target_count": len(grouped),
        "domain_count": len({item["site_key"] for item in grouped.values()}),
        "site_count": len({item["site_key"] for item in grouped.values()}),
        "items": sorted(grouped.values(), key=lambda item: item["target_id"]),
    }
    (discovery_dir / "url-frontier.json").write_text(json.dumps(frontier, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "imported": True, "provider": args.provider, "provider_key": provider_key,
        "source_kind": args.source_kind, "query_id": args.query_id, "raw_sha256": raw_sha256,
        "extracted_urls": len(accepted),
        "retained_provider_urls": retained_counts.get(provider_key, 0),
        "retained_query_urls": len(retained_query_results),
        "max_results_per_query": max_results,
        "frontier_targets": len(grouped),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
