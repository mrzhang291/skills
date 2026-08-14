#!/usr/bin/env python3

"""Run the bounded CherryStudio-compatible quick discovery loop.

The runner deliberately exposes no headed-browser or manual-unblock options.  It
only orchestrates the skill's existing command-line scripts and makes its stop
decision from artifacts in the isolated run directory.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from process_utils import run_bounded


SUCCESS_STATES = {"normal", "zero_results"}
DEFAULT_PROVIDER = "so360"
DEFAULT_PROVIDER_CHAIN = ("so360", "baidu")
ALLOWED_PROVIDERS = {"so360", "bing", "yahoo", "baidu"}
DEFAULT_LIMIT = 6
DEFAULT_TIMEOUT_MS = 20_000
SUMMARY_NAME = "quick-discovery-summary.json"


class QuickDiscoveryError(RuntimeError):
    """A fatal configuration or artifact error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise QuickDiscoveryError(f"Expected JSON object at {path}:{line_number}")
        records.append(value)
    return records


def normalized_query(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def successful_queries(run_dir: Path) -> dict[str, set[str]]:
    successful: dict[str, set[str]] = {}
    for record in read_jsonl(run_dir / "discovery" / "queries.jsonl"):
        if record.get("state") not in SUCCESS_STATES:
            continue
        query_id = str(record.get("query_id") or "").strip()
        if query_id:
            successful.setdefault(query_id, set()).add(normalized_query(record.get("query")))
    return successful


def query_records(run_dir: Path, query_id: str, query: str) -> list[dict]:
    expected = normalized_query(query)
    return [
        record for record in read_jsonl(run_dir / "discovery" / "queries.jsonl")
        if str(record.get("query_id") or "") == query_id
        and normalized_query(record.get("query")) == expected
    ]


def query_result_count(run_dir: Path, query_id: str) -> int:
    return sum(
        1 for record in read_jsonl(run_dir / "discovery" / "results.jsonl")
        if str(record.get("query_id") or "") == query_id
    )


def query_is_terminal(run_dir: Path, query_id: str, query: str, providers: list[str]) -> bool:
    records = query_records(run_dir, query_id, query)
    if any(int(record.get("result_count") or 0) > 0 for record in records):
        return True
    terminal = {
        str(record.get("provider") or "") for record in records
        if record.get("state") in SUCCESS_STATES
    }
    return all(provider in terminal for provider in providers)


def discovery_snapshot(run_dir: Path) -> dict:
    frontier = read_json(run_dir / "discovery" / "url-frontier.json", {}) or {}
    items = frontier.get("items") or []
    results = read_jsonl(run_dir / "discovery" / "results.jsonl")
    urls = {
        str(item.get("normalized_url") or item.get("url") or "").strip()
        for item in items
        if str(item.get("normalized_url") or item.get("url") or "").strip()
    }
    if not urls:
        urls = {
            str(item.get("normalized_url") or item.get("result_url") or "").strip()
            for item in results
            if str(item.get("normalized_url") or item.get("result_url") or "").strip()
        }
    sites = {
        str(item.get("site_key") or item.get("domain") or "").strip().casefold()
        for item in items
        if str(item.get("site_key") or item.get("domain") or "").strip()
    }
    if not sites:
        sites = {
            str(item.get("site_key") or item.get("domain") or "").strip().casefold()
            for item in results
            if str(item.get("site_key") or item.get("domain") or "").strip()
        }
    return {
        "target_urls": int(frontier.get("target_count") or len(urls)),
        "target_domains": int(frontier.get("domain_count") or frontier.get("site_count") or len(sites)),
        "discovery_records": len(results),
    }


def ranked_snapshot(run_dir: Path) -> dict:
    ranked = read_json(run_dir / "discovery" / "ranked-candidates.json", {}) or {}
    return {
        "selected_count": int(ranked.get("selected_count") or 0),
        "selected_site_count": int(ranked.get("selected_site_count") or 0),
        "output": str(run_dir / "discovery" / "ranked-candidates.json"),
    }


def requirements_from(config: dict) -> dict:
    coverage = config.get("coverage_requirements") or {}
    return {
        "min_target_urls": max(0, int(coverage.get("min_target_urls", 4))),
        "min_target_domains": max(0, int(coverage.get("min_target_domains", 2))),
        "min_ranked_candidates": max(0, int(coverage.get("min_probe_attempts", 3))),
    }


def criteria_met(snapshot: dict, ranked: dict, requirements: dict) -> bool:
    return (
        snapshot["target_urls"] >= requirements["min_target_urls"]
        and snapshot["target_domains"] >= requirements["min_target_domains"]
        and ranked["selected_count"] >= requirements["min_ranked_candidates"]
    )


def invoke(
    command: list[str],
    runner: Callable[..., subprocess.CompletedProcess],
    *,
    timeout: float = 45,
) -> subprocess.CompletedProcess:
    return runner(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def bounded_runner(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Production runner with the same keyword contract used by test doubles."""
    return run_bounded(
        command,
        timeout=float(kwargs.get("timeout", 45)),
        cwd=kwargs.get("cwd"),
    )


def ensure_query_plan(
    run_dir: Path,
    scripts_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess],
) -> tuple[Path, dict, dict | None]:
    path = run_dir / "discovery" / "query-plan.json"
    generation = None
    if not path.is_file():
        completed = invoke(
            [sys.executable, str(scripts_dir / "build-query-plan.py"), "--run-dir", str(run_dir)],
            runner, timeout=30,
        )
        generation = {
            "return_code": completed.returncode,
            "stdout": (completed.stdout or "")[-2000:],
            "stderr": (completed.stderr or "")[-2000:],
        }
        if completed.returncode != 0 or not path.is_file():
            raise QuickDiscoveryError("Could not generate discovery/query-plan.json")
    plan = read_json(path)
    if not isinstance(plan, dict):
        raise QuickDiscoveryError(f"Invalid query plan: {path}")
    return path, plan, generation


def run_ranker(
    run_dir: Path,
    scripts_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess],
) -> dict:
    try:
        completed = invoke(
            [sys.executable, str(scripts_dir / "rank-frontier.py"), "--run-dir", str(run_dir)],
            runner, timeout=30,
        )
        record = {
            "return_code": completed.returncode,
            "stdout": (completed.stdout or "")[-2000:],
            "stderr": (completed.stderr or "")[-2000:],
        }
    except Exception as exc:  # The query loop must survive one local rank failure.
        record = {"return_code": None, "stdout": "", "stderr": str(exc)}
    record.update(ranked_snapshot(run_dir))
    return record


def validate_plan(plan: dict, max_queries: int) -> list[dict]:
    if plan.get("execution_profile") != "quick":
        raise QuickDiscoveryError("query-plan.json must use execution_profile=quick")
    items = plan.get("items")
    if not isinstance(items, list):
        raise QuickDiscoveryError("query-plan.json items must be an array")
    output = []
    seen = set()
    for raw in items[:max_queries]:
        if not isinstance(raw, dict):
            raise QuickDiscoveryError("Every query-plan item must be an object")
        query_id = str(raw.get("query_id") or "").strip()
        query = re.sub(r"\s+", " ", str(raw.get("query") or "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", query_id) or ".." in query_id:
            raise QuickDiscoveryError(f"Invalid query_id in query plan: {query_id!r}")
        if not query:
            raise QuickDiscoveryError(f"Empty query in query plan: {query_id}")
        if query_id in seen:
            raise QuickDiscoveryError(f"Duplicate query_id in query plan: {query_id}")
        seen.add(query_id)
        output.append({**raw, "query_id": query_id, "query": query})
    return output


def run_quick_discovery(
    run_dir: Path,
    *,
    provider: str = DEFAULT_PROVIDER,
    providers: list[str] | tuple[str, ...] | None = None,
    limit: int = DEFAULT_LIMIT,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    runner: Callable[..., subprocess.CompletedProcess] = bounded_runner,
) -> tuple[dict, int]:
    provider_chain = list(dict.fromkeys(providers or [provider]))
    limit = min(max(1, int(limit)), DEFAULT_LIMIT)
    timeout_ms = min(max(1, int(timeout_ms)), 25_000)
    started_at = utc_now()
    run_dir = run_dir.resolve()
    scripts_dir = Path(__file__).resolve().parent
    summary_path = run_dir / "discovery" / SUMMARY_NAME
    summary: dict = {
        "schema_version": "1.0",
        "record_type": "trademark_quick_discovery_summary",
        "started_at": started_at,
        "finished_at": None,
        "run_dir": str(run_dir),
        "execution_profile": None,
        "status": "error",
        "stop_reason": None,
        "provider": provider_chain[0] if provider_chain else None,
        "providers": provider_chain,
        "limit_per_query": limit,
        "timeout_ms": timeout_ms,
        "headed_browser_used": False,
        "manual_interaction_used": False,
        "codex_browser_used": False,
        "plan_generated": False,
        "plan_path": str(run_dir / "discovery" / "query-plan.json"),
        "requirements": {},
        "starting_discovery": {},
        "ending_discovery": {},
        "ending_ranked": {},
        "criteria_met": False,
        "attempted_count": 0,
        "provider_attempted_count": 0,
        "attempts": [],
        "skipped_successful_query_ids": [],
        "query_failures": [],
        "fatal_error": None,
    }
    exit_code = 2
    try:
        if not run_dir.is_dir():
            raise QuickDiscoveryError(f"Run directory not found: {run_dir}")
        config = read_json(run_dir / "run-config.json")
        if not isinstance(config, dict):
            raise QuickDiscoveryError(f"Invalid or missing run-config.json: {run_dir}")
        profile = config.get("execution_profile")
        summary["execution_profile"] = profile
        if profile != "quick":
            raise QuickDiscoveryError("quick-discover.py only accepts execution_profile=quick")
        if not provider_chain or len(provider_chain) > 2 or any(item not in ALLOWED_PROVIDERS for item in provider_chain):
            raise QuickDiscoveryError(
                "quick-discover.py permits a fallback chain of one or two headless providers: "
                + ", ".join(sorted(ALLOWED_PROVIDERS))
            )
        requirements = requirements_from(config)
        summary["requirements"] = requirements
        max_queries = max(1, int((config.get("budgets") or {}).get("max_query_count", 7)))
        plan_path, plan, generation = ensure_query_plan(run_dir, scripts_dir, runner)
        summary["plan_path"] = str(plan_path)
        summary["plan_generated"] = generation is not None
        if generation is not None:
            summary["plan_generation"] = generation
        plan_items = validate_plan(plan, max_queries)
        summary["planned_query_count"] = len(plan_items)

        starting = discovery_snapshot(run_dir)
        summary["starting_discovery"] = starting
        rank_record = ranked_snapshot(run_dir)
        if starting["discovery_records"] or starting["target_urls"]:
            rank_record = run_ranker(run_dir, scripts_dir, runner)
        summary["initial_rank"] = rank_record

        stop = criteria_met(starting, rank_record, requirements)
        successful = successful_queries(run_dir)
        if stop:
            summary["stop_reason"] = "coverage_already_satisfied"

        for item in plan_items:
            if stop:
                break
            query_id = item["query_id"]
            query = item["query"]
            existing_texts = successful.get(query_id, set())
            if normalized_query(query) in existing_texts and query_is_terminal(
                run_dir, query_id, query, provider_chain
            ):
                summary["skipped_successful_query_ids"].append(query_id)
                continue
            if existing_texts:
                summary["query_failures"].append({
                    "query_id": query_id,
                    "query": query,
                    "stage": "plan_conflict",
                    "message": "query_id already succeeded with different query text; not repeated",
                })
                continue

            attempt = {
                "query_id": query_id,
                "query": query,
                "started_at": utc_now(),
                "command_options": {
                    "providers": provider_chain,
                    "limit_per_provider": limit,
                    "timeout_ms": timeout_ms,
                    "allow_zero_results": True,
                    "headed": False,
                },
                "provider_attempts": [],
            }
            summary["attempted_count"] += 1
            allowed_domains = [
                str(value).strip() for value in item.get("allowed_domains") or []
                if str(value).strip()
            ]
            for current_provider in provider_chain:
                command = [
                    sys.executable,
                    str(scripts_dir / "discover-web.py"),
                    "--run-dir", str(run_dir),
                    "--query-id", query_id,
                    "--query", query,
                    "--providers", current_provider,
                    "--limit-per-provider", str(limit),
                    "--timeout-ms", str(timeout_ms),
                    "--allow-zero-results",
                ]
                for domain in allowed_domains:
                    command.extend(["--allowed-domain", domain])
                provider_attempt = {"provider": current_provider, "started_at": utc_now()}
                summary["provider_attempted_count"] += 1
                try:
                    completed = invoke(command, runner, timeout=max(30, timeout_ms / 1000 + 20))
                    provider_attempt.update({
                        "return_code": completed.returncode,
                        "stdout": (completed.stdout or "")[-2000:],
                        "stderr": (completed.stderr or "")[-2000:],
                    })
                except Exception as exc:
                    provider_attempt.update({"return_code": None, "stdout": "", "stderr": str(exc)})
                provider_attempt["result_count_after"] = query_result_count(run_dir, query_id)
                provider_attempt["finished_at"] = utc_now()
                attempt["provider_attempts"].append(provider_attempt)
                attempt.update({
                    "return_code": provider_attempt["return_code"],
                    "stdout": provider_attempt["stdout"],
                    "stderr": provider_attempt["stderr"],
                })
                if provider_attempt["result_count_after"] > 0:
                    break

            rank_after = run_ranker(run_dir, scripts_dir, runner)
            attempt["rank_after"] = rank_after
            attempt["discovery_after"] = discovery_snapshot(run_dir)
            successful = successful_queries(run_dir)
            attempt["query_success"] = query_is_terminal(run_dir, query_id, query, provider_chain)
            attempt["finished_at"] = utc_now()
            summary["attempts"].append(attempt)
            if not attempt["query_success"]:
                summary["query_failures"].append({
                    "query_id": query_id,
                    "query": query,
                    "stage": "discover-web",
                    "return_code": attempt["return_code"],
                    "message": attempt["stderr"][-1000:] or "query did not record a successful provider state",
                })

            stop = criteria_met(attempt["discovery_after"], rank_after, requirements)
            if stop:
                summary["stop_reason"] = "coverage_satisfied"
                break

        ending = discovery_snapshot(run_dir)
        ending_ranked = ranked_snapshot(run_dir)
        summary["ending_discovery"] = ending
        summary["ending_ranked"] = ending_ranked
        summary["criteria_met"] = criteria_met(ending, ending_ranked, requirements)
        if not summary["stop_reason"]:
            summary["stop_reason"] = "query_plan_exhausted"
        prior_work = (
            starting["discovery_records"] > 0
            or starting["target_urls"] > 0
            or bool(successful_queries(run_dir))
        )
        has_work = summary["attempted_count"] > 0 or prior_work
        if summary["criteria_met"]:
            summary["status"] = "complete"
            exit_code = 0
        elif has_work:
            summary["status"] = "partial"
            exit_code = 0
        else:
            summary["status"] = "error"
            summary["fatal_error"] = "No query was executed and no prior discovery artifacts exist"
            exit_code = 2
    except Exception as exc:
        summary["fatal_error"] = str(exc)
        summary["stop_reason"] = summary["stop_reason"] or "fatal_error"
        summary["status"] = "error"
        exit_code = 2
    finally:
        summary["finished_at"] = utc_now()
        try:
            if run_dir.is_dir():
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                summary["summary_path"] = str(summary_path)
        except Exception as exc:
            summary["summary_write_error"] = str(exc)
    return summary, exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded quick discovery without Codex browser tools")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--provider", choices=sorted(ALLOWED_PROVIDERS), help="Legacy single-provider mode")
    parser.add_argument(
        "--providers", default=",".join(DEFAULT_PROVIDER_CHAIN),
        help="Comma-separated fallback chain; at most two headless providers",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_MS / 1000)
    args = parser.parse_args()
    providers = [item.strip().lower() for item in args.providers.split(",") if item.strip()]
    if args.provider:
        providers = [args.provider]
    summary, exit_code = run_quick_discovery(
        Path(args.run_dir),
        provider=providers[0] if providers else DEFAULT_PROVIDER,
        providers=providers,
        limit=args.limit,
        timeout_ms=max(1, int(args.timeout_seconds * 1000)),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
