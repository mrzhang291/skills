#!/usr/bin/env python3

"""Run the bounded, cheap probe stage for a Quick investigation."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from discovery_limits import positive_budget
from url_utils import is_search_result_url, normalize_url, site_key
from process_utils import run_bounded


DEFAULT_MIN_PROBES = 6
DEFAULT_MAX_PROBES = 8


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Required input not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def read_attempts(run_dir: Path) -> list[dict]:
    path = run_dir / "probe-attempts.jsonl"
    if not path.is_file():
        return []
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Expected a JSON object at {path}:{line_number}")
        records.append(value)
    return records


def ranked_items(document: dict) -> list[dict]:
    items = [dict(item) for item in (document.get("items") or []) if isinstance(item, dict)]
    return sorted(items, key=lambda item: (
        int(item.get("rank") or 10**9),
        str(item.get("target_id") or ""),
        str(item.get("normalized_url") or ""),
    ))


def matching_attempt(item: dict, attempts: list[dict]) -> dict | None:
    normalized = normalize_url(str(item.get("normalized_url") or item.get("url") or ""))
    target = str(item.get("target_id") or "")
    return next((
        record for record in attempts
        if record.get("frontier_id") == target
        and normalize_url(str(record.get("url") or "")) == normalized
    ), None)


def valid_attempt(record: dict | None, run_dir: Path) -> bool:
    if not record or record.get("content_valid") is not True or record.get("status") != "content_valid":
        return False
    relative = record.get("probe_dir")
    if not isinstance(relative, str) or not relative:
        return False
    probe_dir = (run_dir / relative).resolve()
    try:
        probe_dir.relative_to(run_dir.resolve())
    except ValueError:
        return False
    return all((probe_dir / name).is_file() for name in ("metadata.json", "body-text.txt", "probe.png"))


def build_shortlist(items: list[dict], attempts: list[dict], run_dir: Path) -> list[dict]:
    shortlist = []
    for item in items:
        record = matching_attempt(item, attempts)
        if not valid_attempt(record, run_dir):
            continue
        enriched = dict(item)
        enriched["probe"] = {
            "probe_id": record.get("probe_id"),
            "probe_dir": record.get("probe_dir"),
            "page_state": record.get("page_state"),
            "final_url": record.get("final_url"),
            "elapsed_ms": record.get("elapsed_ms"),
        }
        shortlist.append(enriched)
    return shortlist


def build_links(
    items: list[dict],
    attempts: list[dict],
    invocations: list[dict] | None = None,
) -> list[dict]:
    """Retain every ranked destination URL, including failed and unprobed ones."""
    invocation_by_url = {
        normalize_url(str(invocation.get("url") or "")): invocation
        for invocation in (invocations or [])
        if normalize_url(str(invocation.get("url") or ""))
    }
    links = []
    for item in items:
        normalized = normalize_url(str(item.get("normalized_url") or item.get("url") or ""))
        record = matching_attempt(item, attempts)
        invocation = invocation_by_url.get(normalized)
        if record:
            probe_status = record.get("status")
        elif invocation and invocation.get("timed_out"):
            probe_status = "wrapper_timed_out"
        elif invocation:
            probe_status = f"wrapper_exit_{invocation.get('returncode')}"
        else:
            probe_status = "not_probed"
        links.append({
            "rank": item.get("rank"),
            "frontier_id": item.get("target_id"),
            "title": item.get("title"),
            "url": normalized,
            "site_key": site_key(normalized or ""),
            "probe_id": record.get("probe_id") if record else None,
            "attempted": bool(record or invocation),
            "probe_status": probe_status,
            "page_state": record.get("page_state") if record else None,
            "content_valid": bool(record and record.get("content_valid") is True),
            "final_url": record.get("final_url") if record else None,
        })
    return links


def expected_anchors(config: dict, item: dict) -> str | None:
    trademark = config.get("trademark") or {}
    signals = item.get("matched_signals") or {}
    values = []
    for value in (
        signals.get("registration_number"), signals.get("owner"), signals.get("trademark"),
        trademark.get("registration_number"), trademark.get("owner"), trademark.get("name"),
    ):
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)
    return "|||".join(values) if values else None


def eligible_for_new_probe(
    item: dict,
    attempts: list[dict],
    site_counts: Counter[str],
    *,
    per_site_limit: int,
    invoked_urls: set[str],
) -> bool:
    normalized = normalize_url(str(item.get("normalized_url") or item.get("url") or ""))
    if not normalized or is_search_result_url(normalized):
        return False
    if not item.get("target_id") or matching_attempt(item, attempts) or normalized in invoked_urls:
        return False
    return site_counts[site_key(normalized)] < per_site_limit


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe the top Quick-profile ranked URLs with fixed low budgets")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--browser-executable")
    parser.add_argument("--timeout-ms", type=int, default=15_000)
    parser.add_argument("--wall-clock-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.timeout_ms < 1:
        parser.error("--timeout-ms must be positive")
    if args.wall_clock_timeout_seconds <= 0:
        parser.error("--wall-clock-timeout-seconds must be positive")
    return args


def positive_requirement(config: dict, name: str, default: int) -> int:
    value = (config.get("coverage_requirements") or {}).get(name, default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"run-config coverage requirement {name} must be a positive integer") from error


def nonnegative_requirement(config: dict, name: str, default: int) -> int:
    value = (config.get("coverage_requirements") or {}).get(name, default)
    try:
        return max(0, int(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"run-config coverage requirement {name} must be a non-negative integer") from error


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    run_dir = Path(args.run_dir).resolve()
    config = read_json(run_dir / "run-config.json")
    if config.get("execution_profile") != "quick":
        raise ValueError("quick-probe.py is available only when execution_profile=quick")
    ranked_document = read_json(run_dir / "discovery" / "ranked-candidates.json")
    items = ranked_items(ranked_document)
    max_attempts = positive_budget(config, "max_probe_attempts", DEFAULT_MAX_PROBES)
    min_attempts = positive_requirement(config, "min_probe_attempts", DEFAULT_MIN_PROBES)
    target_valid_pages = positive_budget(config, "target_full_pages", 5)
    coverage_min_valid_pages = nonnegative_requirement(config, "min_normal_target_pages", 4)
    fallback_site_limit = positive_budget(config, "max_pages_per_domain", 3)
    per_site_limit = positive_budget(config, "max_probe_pages_per_site", fallback_site_limit)
    base_target = min(min_attempts, max_attempts)
    attempts = read_attempts(run_dir)
    probe_script = Path(__file__).resolve().with_name("probe-target-page.py")
    invocation_results = []
    invoked_urls: set[str] = set()
    extra_probe_count = 0

    def attempt_ranked_count() -> int:
        return sum(
            1
            for item in items
            if matching_attempt(item, attempts)
            or normalize_url(str(item.get("normalized_url") or item.get("url") or "")) in invoked_urls
        )

    def valid_ranked_count() -> int:
        return sum(1 for item in items if valid_attempt(matching_attempt(item, attempts), run_dir))

    def global_attempt_count() -> int:
        persisted_urls = {
            normalize_url(str(record.get("url") or ""))
            for record in attempts
            if normalize_url(str(record.get("url") or ""))
        }
        return len(attempts) + len(invoked_urls - persisted_urls)

    def run_one(item: dict) -> bool:
        nonlocal attempts
        normalized = normalize_url(str(item.get("normalized_url") or item.get("url") or ""))
        if not normalized:
            return False
        invoked_urls.add(normalized)
        command = [
            sys.executable, str(probe_script), "--run-dir", str(run_dir),
            "--frontier-id", str(item.get("target_id")), "--url", normalized,
            "--timeout-ms", str(args.timeout_ms),
            "--wall-clock-timeout-seconds", str(args.wall_clock_timeout_seconds),
        ]
        anchors = expected_anchors(config, item)
        if anchors:
            command.extend(["--expected-text-any", anchors])
        if args.browser_executable:
            command.extend(["--browser-executable", args.browser_executable])
        timed_out = False
        completed = run_bounded(command, timeout=args.wall_clock_timeout_seconds + 5.0)
        returncode = completed.returncode
        timed_out = returncode == 124
        stdout_tail = (completed.stdout or "")[-1000:]
        stderr_tail = (completed.stderr or "")[-1000:]
        invocation_results.append({
            "rank": item.get("rank"),
            "frontier_id": item.get("target_id"),
            "url": normalized,
            "returncode": returncode,
            "timed_out": timed_out,
            "wall_clock_timeout_seconds": args.wall_clock_timeout_seconds + 5.0,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        })
        attempts = read_attempts(run_dir)
        return True

    def next_eligible() -> dict | None:
        attempted_urls = {
            normalize_url(str(record.get("url") or ""))
            for record in attempts
            if normalize_url(str(record.get("url") or ""))
        }
        site_counts = Counter(site_key(url) for url in attempted_urls | invoked_urls)
        return next((
            item for item in items
            if eligible_for_new_probe(
                item,
                attempts,
                site_counts,
                per_site_limit=per_site_limit,
                invoked_urls=invoked_urls,
            )
        ), None)

    while global_attempt_count() < max_attempts and attempt_ranked_count() < base_target:
        candidate = next_eligible()
        if not candidate:
            break
        run_one(candidate)

    while global_attempt_count() < max_attempts and valid_ranked_count() < target_valid_pages:
        candidate = next_eligible()
        if not candidate:
            break
        extra_probe_count += 1
        run_one(candidate)

    attempts = read_attempts(run_dir)
    shortlist = build_shortlist(items, attempts, run_dir)
    links = build_links(items, attempts, invocation_results)
    ranked_attempts = [record for item in items if (record := matching_attempt(item, attempts))]
    if len(shortlist) >= target_valid_pages:
        status = "target_met"
    elif len(shortlist) >= coverage_min_valid_pages:
        status = "coverage_met"
    else:
        status = "insufficient_valid_pages"
    summary = {
        "schema_version": "1.0",
        "record_type": "quick_probe_shortlist",
        "generated_at": utc_now(),
        "run_id": config.get("run_id"),
        "execution_profile": "quick",
        "limits": {
            "min_probe_attempts": min_attempts,
            "max_probe_attempts": max_attempts,
            "per_site_limit": per_site_limit,
        },
        "summary": {
            "status": status,
            "ranked_candidate_count": len(items),
            "global_probe_attempt_count": global_attempt_count(),
            "persisted_probe_record_count": len(attempts),
            "ranked_probe_attempt_count": attempt_ranked_count(),
            "persisted_ranked_probe_record_count": len(ranked_attempts),
            "content_valid_count": len(shortlist),
            "target_valid_pages": target_valid_pages,
            "coverage_min_valid_pages": coverage_min_valid_pages,
            "extra_probe_count": extra_probe_count,
            "invalid_count": max(0, attempt_ranked_count() - len(shortlist)),
            "base_target_met": attempt_ranked_count() >= base_target,
            "budget_exhausted": global_attempt_count() >= max_attempts,
        },
        "invocations": invocation_results,
        "links": links,
        "items": shortlist,
    }
    output_path = run_dir / "discovery" / "probe-shortlist.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "probed": True,
        "output": str(output_path),
        **summary["summary"],
    }, ensure_ascii=False, indent=2))
    return 0 if len(shortlist) >= coverage_min_valid_pages else 4


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
