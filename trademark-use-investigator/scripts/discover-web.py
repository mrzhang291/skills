#!/usr/bin/env python3

"""Discover real destination URLs. Search-result pages remain diagnostics only."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discovery_limits import (
    cap_query_results, positive_budget, remove_provider_results, validate_query_slot,
)
from provenance_utils import file_sha256, validate_provider
from url_utils import (
    hostname, is_search_result_url, normalize_url, require_safe_file_id, site_key, target_id,
)
from sales_platforms import allowed_url
from process_utils import run_bounded


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


BROWSER_PROVIDERS = {"bing", "so360", "yahoo", "baidu"}
QUICK_MAX_RESULTS_PER_QUERY = 6
QUICK_MAX_TIMEOUT_MS = 25_000


def execution_profile(config: dict) -> str:
    """Legacy run configurations retain the original forensic behaviour."""
    return str(config.get("execution_profile") or "forensic").strip().lower()


def browser_wall_timeout_seconds(timeout_ms: int) -> float:
    """Give the Node worker a short shutdown grace period without hanging forever."""
    return max(10.0, timeout_ms / 1000.0 + 15.0)


def timeout_output(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def query_terms(query: str) -> list[str]:
    value = re.sub(r'["“”\'‘’]', " ", query or "")
    return [
        item.lower() for item in re.split(r"[\s,+|/]+", value)
        if len(item.strip()) >= 2
        and item.lower() not in {"site", "www", "http", "https"}
        and not item.lower().startswith("site:")
    ]


def relevant(item: dict, query: str, normalized_url: str) -> bool:
    terms = query_terms(query)
    if not terms:
        return True
    sample = f"{item.get('title') or ''}\n{item.get('snippet') or ''}\n{normalized_url}".casefold()
    return any(term.casefold() in sample for term in terms)


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")


def safe_id(value: str) -> str:
    return require_safe_file_id(value, "query-id")


def collect_url_items(value, output: list[dict]) -> None:
    if isinstance(value, dict):
        url = value.get("url") or value.get("link")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            output.append({
                "url": url,
                "title": str(value.get("title") or value.get("name") or "").strip(),
                "snippet": str(value.get("description") or value.get("snippet") or value.get("markdown") or "")[:2000],
            })
        for child in value.values():
            collect_url_items(child, output)
    elif isinstance(value, list):
        for child in value:
            collect_url_items(child, output)


def run_browser_provider(args, provider: str, provider_dir: Path) -> tuple[dict, list[dict]]:
    script = Path(__file__).resolve().with_name("discover-search-results.mjs")
    command = [
        "node", str(script), "--query", args.query, "--query-id", args.query_id,
        "--provider", provider, "--output-dir", str(provider_dir),
        "--limit", str(args.limit_per_provider), "--timeout-ms", str(args.timeout_ms),
    ]
    if args.browser_executable:
        command.extend(["--browser-executable", args.browser_executable])
    if args.headed:
        command.append("--headed")
    if args.wait_for_unblock_ms:
        command.extend(["--wait-for-unblock-ms", str(args.wait_for_unblock_ms)])
    wall_timeout = browser_wall_timeout_seconds(args.timeout_ms)
    completed = run_bounded(command, timeout=wall_timeout)
    if completed.returncode == 124:
        detail = "\n".join(filter(None, [completed.stdout, completed.stderr]))[-2000:]
        report = {
            "schema_version": "2.0",
            "record_type": "discovery_provider_run",
            "query_id": args.query_id,
            "query": args.query,
            "provider": provider,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "state": "error",
            "result_count": 0,
            "results": [],
            "timed_out": True,
            "wall_timeout_seconds": wall_timeout,
            "errors": [{
                "stage": "subprocess_timeout",
                "message": f"browser provider exceeded {wall_timeout:.1f}s wall-clock timeout"
                + (f": {detail}" if detail else ""),
            }],
            "exit_code": None,
        }
        provider_dir.mkdir(parents=True, exist_ok=True)
        (provider_dir / "results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return report, []
    result_path = provider_dir / "results.json"
    report = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {
        "state": "error", "result_count": 0, "results": [],
        "errors": [{"stage": "subprocess", "message": completed.stderr[-2000:]}],
    }
    report["exit_code"] = completed.returncode
    return report, report.get("results") or []


def firecrawl_command() -> list[str] | None:
    executable = shutil.which("firecrawl")
    if executable:
        return [executable]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "firecrawl-cli@1.19.24"]
    return None


def run_firecrawl(args, provider_dir: Path, raw_path: Path) -> tuple[dict, list[dict]]:
    if not (os.environ.get("FIRECRAWL_API_KEY") or os.environ.get("FIRECRAWL_API_URL")):
        return ({"state": "unavailable", "result_count": 0, "errors": [{"stage": "preflight", "message": "FIRECRAWL_API_KEY/FIRECRAWL_API_URL is not configured"}]}, [])
    base = firecrawl_command()
    if not base:
        return ({"state": "unavailable", "result_count": 0, "errors": [{"stage": "preflight", "message": "firecrawl CLI and npx are unavailable"}]}, [])
    command = base + [
        "search", args.query, "--limit", str(args.limit_per_provider), "--country", args.country,
        "--json", "--output", str(raw_path),
    ]
    completed = run_bounded(command, timeout=max(60, args.timeout_ms // 1000 + 30))
    if not raw_path.is_file():
        raw_path.write_text(completed.stdout or completed.stderr or "", encoding="utf-8")
    parsed = None
    try:
        parsed = json.loads(raw_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass
    found: list[dict] = []
    if parsed is not None:
        collect_url_items(parsed, found)
    state = "normal" if completed.returncode == 0 and found else ("zero_results" if completed.returncode == 0 else "error")
    report = {
        "schema_version": "2.0", "record_type": "discovery_provider_run",
        "query_id": args.query_id, "query": args.query, "provider": "firecrawl",
        "captured_at": datetime.now(timezone.utc).isoformat(), "state": state,
        "result_count": len(found), "exit_code": completed.returncode,
        "errors": [] if completed.returncode == 0 else [{"stage": "subprocess", "message": (completed.stderr or completed.stdout)[-2000:]}],
    }
    (provider_dir / "results.json").write_text(json.dumps({**report, "results": found}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report, found


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover destination URLs without promoting search-result pages as evidence")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--providers", default="firecrawl,bing,so360,yahoo,baidu")
    parser.add_argument("--limit-per-provider", type=int, default=20)
    parser.add_argument("--min-success-providers", type=int)
    parser.add_argument("--country", default="CN")
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument("--browser-executable")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--wait-for-unblock-ms", type=int, default=0,
        help="With --headed, wait this long for manual search-provider login/CAPTCHA completion",
    )
    parser.add_argument("--allow-zero-results", action="store_true")
    parser.add_argument("--allowed-domain", action="append", default=[])
    args = parser.parse_args()

    args.query_id = safe_id(args.query_id)
    run_dir = Path(args.run_dir).resolve()
    config_path = run_dir / "run-config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"run-config.json not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    profile = execution_profile(config)
    validate_query_slot(
        config,
        read_jsonl(run_dir / "discovery" / "queries.jsonl"),
        args.query_id,
        args.query,
    )
    requirement = config.get("coverage_requirements") or {}
    minimum = args.min_success_providers if args.min_success_providers is not None else int(requirement.get("min_discovery_providers", 2))
    max_results = positive_budget(config, "max_results_per_query", 20)
    if args.limit_per_provider < 1:
        raise ValueError("limit-per-provider must be a positive integer")
    if args.timeout_ms < 1:
        raise ValueError("timeout-ms must be a positive integer")
    if args.wait_for_unblock_ms < 0:
        raise ValueError("wait-for-unblock-ms cannot be negative")
    if args.wait_for_unblock_ms and not args.headed:
        raise ValueError("--wait-for-unblock-ms requires --headed")
    providers = list(dict.fromkeys(item.strip().lower() for item in args.providers.split(",") if item.strip()))
    if profile == "quick":
        if args.headed or args.wait_for_unblock_ms:
            raise ValueError("Quick profile forbids --headed and --wait-for-unblock-ms")
        if len(providers) != 1 or providers[0] not in BROWSER_PROVIDERS:
            raise ValueError(
                "Quick profile requires exactly one browser provider: bing, so360, yahoo, or baidu; "
                "firecrawl and multi-provider discovery are forbidden"
            )
        max_results = min(max_results, QUICK_MAX_RESULTS_PER_QUERY)
        args.timeout_ms = min(args.timeout_ms, QUICK_MAX_TIMEOUT_MS)
    args.limit_per_provider = min(args.limit_per_provider, max_results)
    unsupported = [item for item in providers if item not in BROWSER_PROVIDERS | {"firecrawl"}]
    if unsupported:
        raise ValueError("Unsupported providers: " + ", ".join(unsupported))
    provider_keys = {}
    for provider in providers:
        source_kind = "search_api" if provider == "firecrawl" else "browser_search"
        _, provider_keys[provider] = validate_provider(provider, source_kind=source_kind)

    discovery_dir = run_dir / "discovery"
    raw_dir = discovery_dir / "raw"
    providers_dir = discovery_dir / "providers"
    raw_dir.mkdir(parents=True, exist_ok=True)
    providers_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    provider_runs = []
    result_records = []

    for provider in providers:
        provider_dir = providers_dir / f"{args.query_id}-{provider}"
        provider_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{args.query_id}-{provider}.json"
        if provider == "firecrawl":
            report, items = run_firecrawl(args, provider_dir, raw_path)
        else:
            report, items = run_browser_provider(args, provider, provider_dir)
            source_json = provider_dir / "results.json"
            if source_json.is_file():
                shutil.copy2(source_json, raw_path)
        raw_sha256 = file_sha256(raw_path) if raw_path.is_file() else None
        provider_key = provider_keys[provider]
        rank = 0
        seen_provider = set()
        for item in items:
            normalized = normalize_url(str(item.get("url") or ""))
            if (
                not normalized
                or normalized in seen_provider
                or is_search_result_url(normalized)
                or (args.allowed_domain and not allowed_url(normalized, args.allowed_domain))
                or not relevant(item, args.query, normalized)
            ):
                continue
            seen_provider.add(normalized)
            rank += 1
            result_records.append({
                "schema_version": "2.0", "record_type": "discovered_target_url",
                "discovery_id": f"{args.query_id}-{provider.upper()}-{rank:03d}",
                "query_id": args.query_id, "query": args.query, "provider": provider,
                "provider_key": provider_key,
                "source_kind": "search_api" if provider == "firecrawl" else "browser_search",
                "raw_sha256": raw_sha256, "rank": rank,
                "title": str(item.get("title") or "")[:500], "snippet": str(item.get("snippet") or "")[:2000],
                "result_url": str(item.get("url") or ""), "normalized_url": normalized,
                "target_id": target_id(normalized), "domain": hostname(normalized),
                "site_key": site_key(normalized), "discovered_at": now,
            })
        state = report.get("state", "error")
        if state == "normal" and rank == 0:
            state = "no_relevant_results"
        provider_runs.append({
            "schema_version": "2.0", "query_id": args.query_id, "query": args.query,
            "provider": provider, "provider_key": provider_key,
            "source_kind": "search_api" if provider == "firecrawl" else "browser_search",
            "executed_at": report.get("captured_at") or now,
            "state": state, "result_count": rank,
            "raw_file": str(raw_path.relative_to(run_dir)).replace("\\", "/") if raw_path.is_file() else None,
            "raw_sha256": raw_sha256,
            "diagnostic_dir": str(provider_dir.relative_to(run_dir)).replace("\\", "/"),
            "errors": report.get("errors") or [],
        })

    extracted_result_records = result_records

    # A partial rerun replaces only the provider(s) requested now.  Preserve
    # earlier CherryStudio/imported providers for this query, then re-apply the
    # one cross-provider query budget deterministically.
    all_results = read_jsonl(discovery_dir / "results.jsonl")
    for provider in providers:
        all_results = remove_provider_results(
            all_results, args.query_id, provider, provider_keys[provider]
        )
    other_query_results = [item for item in all_results if item.get("query_id") != args.query_id]
    query_candidates = [item for item in all_results if item.get("query_id") == args.query_id]
    query_candidates.extend(extracted_result_records)
    result_records = cap_query_results(query_candidates, max_results)
    retained_counts = Counter(item.get("provider_key") for item in result_records)
    for run in provider_runs:
        extracted_count = int(run.get("result_count") or 0)
        run["extracted_result_count"] = extracted_count
        run["result_count"] = retained_counts.get(run.get("provider_key"), 0)
        run["max_results_per_query"] = max_results
        run["budget_limited"] = run["result_count"] < extracted_count

    query_log = read_jsonl(discovery_dir / "queries.jsonl")
    query_log = [item for item in query_log if not (
        item.get("query_id") == args.query_id and item.get("provider_key") in set(provider_keys.values())
    )]
    query_log.extend(provider_runs)
    for run in query_log:
        if run.get("query_id") == args.query_id:
            run["result_count"] = retained_counts.get(run.get("provider_key"), 0)
            run["max_results_per_query"] = max_results
            if run.get("extracted_result_count") is not None:
                run["budget_limited"] = run["result_count"] < int(run.get("extracted_result_count") or 0)
    query_log.sort(key=lambda item: (item.get("query_id", ""), item.get("provider", "")))
    write_jsonl(discovery_dir / "queries.jsonl", query_log)

    all_results = other_query_results
    all_results.extend(result_records)
    all_results.sort(key=lambda item: (item.get("query_id", ""), item.get("provider", ""), item.get("rank", 0)))
    write_jsonl(discovery_dir / "results.jsonl", all_results)

    grouped: dict[str, dict] = {}
    for item in all_results:
        url = item["normalized_url"]
        entry = grouped.setdefault(url, {
            "target_id": target_id(url), "normalized_url": url, "domain": hostname(url),
            "site_key": site_key(url),
            "title": item.get("title"), "status": "pending", "discovered_via": [],
        })
        entry["discovered_via"].append({
            "query_id": item.get("query_id"), "query": item.get("query"), "provider": item.get("provider"),
            "provider_key": item.get("provider_key"), "rank": item.get("rank"),
            "discovery_id": item.get("discovery_id"), "raw_sha256": item.get("raw_sha256"),
        })
        entry["discovered_via"].extend(item.get("additional_discoveries") or [])
        if not entry.get("title") and item.get("title"):
            entry["title"] = item["title"]
    frontier = {
        "schema_version": "2.0", "updated_at": now, "target_count": len(grouped),
        "domain_count": len({item["site_key"] for item in grouped.values() if item.get("site_key")}),
        "site_count": len({item["site_key"] for item in grouped.values() if item.get("site_key")}),
        "items": sorted(grouped.values(), key=lambda item: item["target_id"]),
    }
    (discovery_dir / "url-frontier.json").write_text(json.dumps(frontier, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    successes = [
        item for item in query_log
        if item.get("query_id") == args.query_id and item.get("state") in {"normal", "zero_results"}
    ]
    successful_base_providers = sorted({item.get("provider") for item in successes if item.get("provider")})
    raw_owners: dict[str, set[str]] = {}
    for item in successes:
        if item.get("raw_sha256") and item.get("provider"):
            raw_owners.setdefault(item["raw_sha256"], set()).add(item["provider"])
    duplicate_raw_claims = {
        digest: sorted(values) for digest, values in raw_owners.items() if len(values) > 1
    }
    summary = {
        "schema_version": "2.0", "query_id": args.query_id, "query": args.query,
        "execution_profile": profile,
        "providers_requested": providers, "providers_successful": successful_base_providers,
        "provider_runs": provider_runs, "new_target_urls": len({item["normalized_url"] for item in result_records}),
        "max_results_per_query": max_results,
        "extracted_result_records": len(extracted_result_records),
        "retained_result_records": len(result_records),
        "result_budget_limited": len(result_records) < len(extracted_result_records),
        "all_unique_target_urls": frontier["target_count"], "all_target_domains": frontier["domain_count"],
        "duplicate_raw_claims": duplicate_raw_claims,
        "coverage_status": "complete" if len(successful_base_providers) >= minimum and not duplicate_raw_claims else "insufficient_providers",
        "search_pages_are_formal_evidence": False,
    }
    (discovery_dir / f"{args.query_id}-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if len(successful_base_providers) < minimum or duplicate_raw_claims:
        raise SystemExit(4)
    if not result_records and not args.allow_zero_results:
        raise SystemExit(5)


if __name__ == "__main__":
    main()
