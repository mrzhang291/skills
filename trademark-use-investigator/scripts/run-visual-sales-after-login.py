#!/usr/bin/env python3
"""Run QCC-reference, logged-in platform search, capture, visual retention and PDF."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["PYTHON_EXECUTABLE"] = sys.executable
from edge_profile import (
    browser_profile_process_is_running,
    is_default_browser_user_data,
    resolve_profile_directory,
)
from browser_state_guard import validate_locked_browser_state
from qcc_reference_guard import validate_qcc_reference
from process_utils import run_bounded
from audit_cherrystudio_run import (
    artifact_record_matches,
    assisted_query_binding_valid,
    url_query_binding_for_plan,
    valid_image,
    validate_pdf,
)

from sales_platforms import FILING_PLATFORM_ORDER, QUICK_PLATFORM_ORDER
from runtime_policy import (
    DEFAULT_SALES_PAGES_PER_PLATFORM,
    DEFAULT_SEARCH_PAGES_PER_PROVIDER,
    MAX_PAGES_PER_SECTION,
    MIN_PAGES_PER_SECTION,
    MAX_PARALLEL_SEARCH_CHANNELS,
    MAX_PARALLEL_ARTIFACT_CAPTURES,
    MAX_SAME_DOMAIN_REQUESTS,
    PUBLIC_SEARCH_PROVIDERS,
    public_search_matrix_wall_timeout_seconds,
    sales_channel_wall_timeout_seconds,
)

PUBLIC_DELIVERY_STATES = {"normal", "zero_results"}
PUBLIC_HUMAN_VERIFICATION_STATES = {"captcha", "login_required"}
PUBLIC_RISK_BLOCKING_STATES = {"access_denied", "rate_limited"}
PUBLIC_BLOCKING_STATES = {
    *PUBLIC_HUMAN_VERIFICATION_STATES,
    *PUBLIC_RISK_BLOCKING_STATES,
    "deferred_manual_verification", "deferred_provider_internal_cooldown",
    "deferred_provider_circuit_breaker",
}
SALES_HUMAN_VERIFICATION_STATES = {"captcha", "login_required"}
SALES_RISK_BLOCKING_STATES = {"access_denied", "rate_limited"}
SALES_BLOCKING_STATES = SALES_HUMAN_VERIFICATION_STATES | SALES_RISK_BLOCKING_STATES
RESUMABLE_BROWSER_PHASES = {
    "awaiting_manual_login", "public_search_verification_required",
    "public_search_internal_cooldown",
    "public_search_capture_retry_required", "public_search_technical_failure_resumable",
    "waiting_internal_cooldown", "sales_platform_resume_ready",
    "sales_platform_verification_required", "sales_platform_capture_retry_required",
    "sales_platform_verification_or_cooldown_required", "capture_failed",
}


def run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    return run_bounded(command, timeout=timeout)


def run_search_channels(
    jobs: dict[str, tuple[list[str], int]],
    *,
    runner=run,
    max_workers: int = MAX_PARALLEL_SEARCH_CHANNELS,
) -> dict[str, subprocess.CompletedProcess]:
    """Run the public and sales subprocesses concurrently without cancelling siblings."""
    if not jobs:
        return {}
    worker_count = max(1, min(MAX_PARALLEL_SEARCH_CHANNELS, int(max_workers), len(jobs)))
    if worker_count == 1:
        return {name: runner(command, timeout) for name, (command, timeout) in jobs.items()}
    results: dict[str, subprocess.CompletedProcess] = {}
    failures: dict[str, BaseException] = {}
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="trademark-search") as executor:
        futures = {
            executor.submit(runner, command, timeout): name
            for name, (command, timeout) in jobs.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except BaseException as exc:  # wait for the sibling before failing closed
                failures[name] = exc
    if failures:
        details = "; ".join(f"{name}: {error}" for name, error in failures.items())
        raise RuntimeError(f"parallel search channel runner failed after all channels joined: {details}")
    return results


def activate_cdp_target(endpoint: str | None, target_id: object) -> bool:
    if not endpoint or not target_id:
        return False
    parsed = urlsplit(str(endpoint))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False
    try:
        with urlopen(f"{str(endpoint).rstrip('/')}/json/activate/{quote(str(target_id), safe='')}", timeout=3):
            return True
    except Exception:
        return False


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def quarantine_provisional_deliverables(run_dir: Path) -> list[str]:
    names = (
        "related-web-results.pdf", "related-web-results.manifest.json",
        "all-detected-html-pages.pdf", "all-detected-html-pages.manifest.json",
        "visual-sales-workflow-summary.json",
    )
    existing = [run_dir / name for name in names if (run_dir / name).is_file()]
    if not existing:
        return []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = run_dir / "capture-diagnostics" / "provisional-deliverables" / stamp
    destination.mkdir(parents=True, exist_ok=True)
    moved = []
    for source in existing:
        target = destination / source.name
        source.replace(target)
        moved.append(str(target.relative_to(run_dir)).replace("\\", "/"))
    return moved


def capture_result_is_complete(return_code: int, summary: dict) -> bool:
    return bool(
        return_code == 0
        and isinstance(summary, dict)
        and summary.get("status") == "complete"
        and summary.get("visual_match_complete") is True
    )


def reusable_assisted_run(run_dir: Path, record: dict, planned: dict | None = None) -> bool:
    if not isinstance(planned, dict):
        return False
    if not all((
        str(record.get("query_id") or "") == str(planned.get("task_id") or ""),
        record.get("platform") == planned.get("platform"),
        record.get("search_query") == planned.get("query"),
        record.get("target_good") == planned.get("target_good"),
        record.get("expected_search_url") == planned.get("search_url"),
    )):
        return False
    if record.get("delivery_eligible") is not True:
        return False
    root = (run_dir / "discovery" / "assisted-platforms").resolve()
    eligible_pages = [item for item in record.get("page_runs") or [] if item.get("delivery_eligible") is True]
    if not eligible_pages:
        return False
    if str(record.get("artifact_dir") or "") != str(eligible_pages[0].get("artifact_dir") or ""):
        return False
    required = {
        "rendered_dom": "rendered-dom.html", "search_html": "search.html",
        "fullpage": "search.png", "mhtml": "page.mhtml", "pdf": "page.pdf", "items": "items.json",
    }
    for page in eligible_pages:
        artifact_dir = (run_dir / str(page.get("artifact_dir") or "")).resolve()
        if artifact_dir == root or not artifact_dir.is_relative_to(root):
            return False
        metadata_path = artifact_dir / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if metadata.get("delivery_eligible") is not True:
            return False
        for key in (
            "page_index", "state", "url", "captured_at", "result_count", "delivery_eligible",
            "artifact_dir", "platform", "query_id", "search_query", "target_good",
            "expected_search_url", "submitted_query_verified",
        ):
            if metadata.get(key) != page.get(key):
                return False
        if metadata.get("state") not in {"normal", "zero_results"}:
            return False
        if metadata.get("state") == "normal" and int(metadata.get("result_count") or 0) <= 0:
            return False
        if metadata.get("state") == "zero_results" and metadata.get("explicit_zero_results") is not True:
            return False
        visual = metadata.get("visual_capture") or {}
        if not all((
            visual.get("strategy") == "viewport_tile_stitch_v1",
            visual.get("acceptable") is True,
            visual.get("output_created") is True,
            metadata.get("capture_strategy") == "viewport_tile_stitch_v1",
            metadata.get("image_load_complete") is True,
        )):
            return False
        binding = metadata.get("query_binding") or {}
        if not all((
            assisted_query_binding_valid(binding, planned, str(metadata.get("url") or "")),
            metadata.get("submitted_query_verified") is True,
        )):
            return False
        artifacts = metadata.get("artifacts") or {}
        for key, filename in required.items():
            target = artifact_dir / filename
            valid = valid_image(target) if key == "fullpage" else target.is_file() and target.stat().st_size > 0
            if not valid or not artifact_record_matches(run_dir, artifacts.get(key), target):
                return False
        if not validate_pdf(artifact_dir / "page.pdf")[0]:
            return False
        try:
            items = json.loads((artifact_dir / "items.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(items, list) or len(items) != int(metadata.get("result_count") or 0):
            return False
    return True


def upgrade_legacy_query_bindings(
    run_dir: Path, report: dict, planned_by_id: dict[str, dict], report_path: Path,
) -> dict:
    """Locally bind same-RUN legacy pages when their URL proves the query.

    This performs no network request.  Pages whose canonical route/query cannot
    be proven from the archived URL remain pending and will be searched again.
    """
    if not isinstance(report, dict):
        return {}
    changed = False
    for record in report.get("platform_runs") or []:
        if not isinstance(record, dict):
            continue
        planned = planned_by_id.get(str(record.get("query_id") or ""))
        if not isinstance(planned, dict) or not all((
            record.get("platform") == planned.get("platform"),
            record.get("search_query") == planned.get("query"),
            record.get("target_good") == planned.get("target_good"),
        )):
            continue
        if record.get("expected_search_url") != planned.get("search_url"):
            record["expected_search_url"] = planned.get("search_url")
            changed = True
        for page in record.get("page_runs") or []:
            if not isinstance(page, dict) or page.get("delivery_eligible") is not True:
                continue
            binding = url_query_binding_for_plan(planned, str(page.get("url") or ""))
            if binding is None:
                continue
            artifact_dir = (run_dir / str(page.get("artifact_dir") or "")).resolve()
            try:
                metadata = json.loads((artifact_dir / "metadata.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            fields = {
                "platform": planned.get("platform"),
                "query_id": planned.get("task_id"),
                "search_query": planned.get("query"),
                "target_good": planned.get("target_good"),
                "expected_search_url": planned.get("search_url"),
                "query_binding": binding,
                "submitted_query_verified": True,
            }
            for key, value in fields.items():
                if page.get(key) != value:
                    page[key] = value
                    changed = True
                if metadata.get(key) != value:
                    metadata[key] = value
                    changed = True
            write_json(artifact_dir / "metadata.json", metadata)
    if changed:
        report["legacy_query_bindings_upgraded_locally"] = True
        report["legacy_query_binding_upgrade_network_used"] = False
        write_json(report_path, report)
    return report


def parse_utc(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def classify_incomplete_sales(
    run_dir: Path,
    pending_runs: list[dict],
    pending_platforms: list[str],
    *,
    now: datetime | None = None,
) -> dict:
    """Classify an incomplete sales matrix without calling it a login failure.

    A real blocker starts a persisted safety window.  Missing/invalid artifacts,
    extraction failures and other technical conditions are resumable automation
    failures and must never be surfaced as ``awaiting_manual_login``.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rate_path = run_dir / "discovery" / "sales-rate-limit-state.json"
    try:
        rate_state = json.loads(rate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rate_state = {}
    platform_state = rate_state.get("platforms") if isinstance(rate_state, dict) else {}
    platform_state = platform_state if isinstance(platform_state, dict) else {}
    observed_verification_platforms = list(dict.fromkeys(
        str(record.get("platform") or "")
        for record in pending_runs
        if record.get("platform") and (
            any(str(record.get(key) or "") in SALES_HUMAN_VERIFICATION_STATES for key in (
                "home_state", "initial_state", "final_state",
            ))
            or any(
                str(page.get("state") or "") in SALES_HUMAN_VERIFICATION_STATES
                for page in record.get("page_runs") or []
                if isinstance(page, dict)
            )
        )
    ))
    observed_verification = set(observed_verification_platforms)
    buckets = {"runnable_pending": [], "cooling_pending": [], "verification_pending": []}
    for record in pending_runs:
        platform = str(record.get("platform") or "")
        raw = platform_state.get(platform)
        raw = raw if isinstance(raw, dict) else {}
        trigger_state = str(raw.get("trigger_state") or "")
        deadlines = [
            value for value in (
                parse_utc(raw.get("cooldown_until")),
                parse_utc(raw.get("post_batch_not_before")),
            ) if value is not None
        ]
        active_cooldown = bool(deadlines and max(deadlines) > current)
        if trigger_state in SALES_HUMAN_VERIFICATION_STATES or platform in observed_verification:
            bucket = "verification_pending"
        elif active_cooldown:
            bucket = "cooling_pending"
        else:
            bucket = "runnable_pending"
        buckets[bucket].append(record)
    # Compatibility fallback for a legacy platform-only pending checkpoint.
    represented = {str(record.get("platform") or "") for record in pending_runs}
    for platform in dict.fromkeys(pending_platforms):
        if platform in represented:
            continue
        raw = platform_state.get(platform)
        raw = raw if isinstance(raw, dict) else {}
        trigger_state = str(raw.get("trigger_state") or "")
        deadlines = [
            value for value in (
                parse_utc(raw.get("cooldown_until")), parse_utc(raw.get("post_batch_not_before")),
            ) if value is not None
        ]
        synthetic = {"platform": platform, "query_id": None, "final_state": "missing_task"}
        if trigger_state in SALES_HUMAN_VERIFICATION_STATES:
            buckets["verification_pending"].append(synthetic)
        elif deadlines and max(deadlines) > current:
            buckets["cooling_pending"].append(synthetic)
        else:
            buckets["runnable_pending"].append(synthetic)
    active_platforms = list(dict.fromkeys(
        str(record.get("platform") or "") for record in buckets["cooling_pending"]
        if record.get("platform")
    ))
    verification_platforms = list(dict.fromkeys(
        str(record.get("platform") or "") for record in buckets["verification_pending"]
        if record.get("platform")
    ))
    runnable_platforms = list(dict.fromkeys(
        str(record.get("platform") or "") for record in buckets["runnable_pending"]
        if record.get("platform")
    ))
    common = {
        **buckets,
        "runnable_pending_count": len(buckets["runnable_pending"]),
        "cooling_pending_count": len(buckets["cooling_pending"]),
        "verification_pending_count": len(buckets["verification_pending"]),
        "runnable_platforms": runnable_platforms,
        "active_cooldown_platforms": active_platforms,
        "verification_platforms": verification_platforms,
    }
    if buckets["runnable_pending"]:
        return {
            "exit_code": 9,
            "status": "incomplete",
            "phase": "sales_platform_capture_retry_required",
            "resume_requires": "resume_runnable_sales_tasks_while_deferred_platforms_remain_skipped",
            **common,
        }
    if verification_platforms:
        return {
            "exit_code": 7,
            "status": "awaiting_manual_login",
            "phase": "sales_platform_verification_required",
            "resume_requires": "handle_preserved_sales_platform_verification_then_resume_immediately",
            "active_cooldown_platforms": active_platforms,
            "verification_platforms": verification_platforms,
            **common,
        }
    if active_platforms:
        return {
            "exit_code": 7,
            "status": "incomplete",
            "phase": "waiting_internal_cooldown",
            "resume_requires": "wait_for_recorded_cooldown_then_resume",
            "active_cooldown_platforms": active_platforms,
            "verification_platforms": [],
            **common,
        }
    return {
        "exit_code": 9,
        "status": "incomplete",
        "phase": "sales_platform_capture_retry_required",
        "resume_requires": "resume_sales_platform_technical_failures",
        "active_cooldown_platforms": [],
        "verification_platforms": [],
        **common,
    }


def wait_for_browser_exit(browser: str, user_data: Path, seconds: float = 12.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not browser_profile_process_is_running(browser, user_data):
            return True
        time.sleep(0.5)
    return not browser_profile_process_is_running(browser, user_data)


def cdp_endpoint_available(endpoint: str, browser: str | None = None) -> bool:
    try:
        parsed = urlsplit(str(endpoint).strip())
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return False
        with urlopen(endpoint.rstrip("/") + "/json/version", timeout=2) as response:
            value = json.loads(response.read().decode("utf-8"))
        product = str(value.get("Browser") or "").casefold()
        product_matches = (
            browser is None
            or (browser == "edge" and ("edg/" in product or "microsoft edge" in product))
            or (browser == "chrome" and "chrome/" in product and "edg/" not in product)
        )
        return bool(value.get("webSocketDebuggerUrl")) and product_matches
    except Exception:
        return False


def record_step(name: str, completed: subprocess.CompletedProcess) -> dict:
    return {
        "step": name,
        "return_code": completed.returncode,
        "stdout": (completed.stdout or "")[-3000:],
        "stderr": (completed.stderr or "")[-3000:],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run visual-first sales capture after manual Chrome/Edge login")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--browser", choices=("chrome", "edge"))
    parser.add_argument("--browser-user-data")
    parser.add_argument("--browser-executable")
    parser.add_argument("--edge-user-data")
    parser.add_argument("--edge-executable")
    parser.add_argument("--cdp-endpoint")
    parser.add_argument("--profile-directory", default="auto")
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument(
        "--deferred-platform", action="append", default=[],
        help="Planned platform whose remaining tasks stay deferred for cooldown/verification in this invocation",
    )
    parser.add_argument(
        "--skip-sales-search", action="store_true",
        help="Run only locally-runnable public work while preserving the incomplete sales matrix",
    )
    parser.add_argument(
        "--capture-limit", type=int, default=1,
        help="Coarse product-detail review limit; defaults to one candidate to keep search evidence dominant",
    )
    parser.add_argument("--search-limit", type=int, default=40)
    parser.add_argument("--visual-threshold", type=float, default=0.46)
    parser.add_argument(
        "--public-search-provider", action="append", choices=("so360", "sogou", "bing", "baidu"), default=[],
        help="Public search provider for owner/mark/registration plus mark + each good (default: so360 and Baidu)",
    )
    parser.add_argument(
        "--resume-public-provider", action="append", choices=("so360", "sogou", "bing", "baidu"), default=[],
        help="Explicitly resume a provider after the employee has handled its verification page",
    )
    parser.add_argument("--skip-public-search", action="store_true")
    parser.add_argument(
        "--skip-search", action="store_true",
        help="Reuse the existing assisted-sales-results.json and continue with candidate capture/reporting",
    )
    parser.add_argument("--menu-only", action="store_true", help="Stop after platform search pages and build a menu-only lead PDF")
    parser.add_argument(
        "--all-html-pdf", action="store_true",
        help="Also build one self-contained PDF containing every accepted HTML capture and embed the raw HTML files",
    )
    parser.add_argument(
        "--package-existing", action="store_true",
        help="Package an existing completed RUN locally without checking or accessing the browser",
    )
    parser.add_argument(
        "--max-pages-per-platform", type=int,
        help="Legacy global PDF section cap; overrides both sales and public-search caps",
    )
    parser.add_argument(
        "--max-sales-pages-per-platform", type=int, default=DEFAULT_SALES_PAGES_PER_PLATFORM,
        help=f"For --all-html-pdf, cap each sales-platform section (default: {DEFAULT_SALES_PAGES_PER_PLATFORM})",
    )
    parser.add_argument(
        "--max-search-pages-per-provider", type=int, default=DEFAULT_SEARCH_PAGES_PER_PROVIDER,
        help=f"For --all-html-pdf, cap each public-search section (default: {DEFAULT_SEARCH_PAGES_PER_PROVIDER})",
    )
    parser.add_argument("--min-success-platforms", type=int, default=2)
    parser.add_argument("--filing-mode", action="store_true", help="Require continuous result-page capture suitable for a cancellation filing packet")
    parser.add_argument("--pages-per-query", type=int, default=1)
    parser.add_argument("--allow-zero-results", action="store_true", help="Allow an explicit page-1 zero-result page instead of five nonexistent pages")
    parser.add_argument("--per-query-timeout-ms", type=int, default=20000)
    args = parser.parse_args()
    if args.max_pages_per_platform is not None:
        args.max_sales_pages_per_platform = args.max_pages_per_platform
        args.max_search_pages_per_provider = args.max_pages_per_platform
    if not MIN_PAGES_PER_SECTION <= args.max_sales_pages_per_platform <= MAX_PAGES_PER_SECTION:
        raise ValueError(
            f"--max-sales-pages-per-platform must be between {MIN_PAGES_PER_SECTION} and {MAX_PAGES_PER_SECTION}"
        )
    if not MIN_PAGES_PER_SECTION <= args.max_search_pages_per_provider <= MAX_PAGES_PER_SECTION:
        raise ValueError(
            f"--max-search-pages-per-provider must be between {MIN_PAGES_PER_SECTION} and {MAX_PAGES_PER_SECTION}"
        )

    run_dir = Path(args.run_dir).resolve()
    state_path = run_dir / "discovery" / "sales-workflow-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    config_path = run_dir / "run-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    scripts = Path(__file__).resolve().parent
    if args.package_existing:
        started_at = datetime.now(timezone.utc).isoformat()
        package_steps = []
        related = run([
            sys.executable, str(scripts / "build-related-results-pdf.py"),
            "--run-dir", str(run_dir),
        ], 300)
        package_steps.append(record_step("build_related_results_pdf", related))
        if related.returncode != 0 or not (run_dir / "related-web-results.pdf").is_file():
            raise RuntimeError(f"Related-results PDF failed: {related.stderr or related.stdout}")
        manifest = {}
        if args.all_html_pdf:
            all_html = run([
                sys.executable, str(scripts / "build-all-detected-html-pdf.py"),
                "--run-dir", str(run_dir),
                "--max-sales-pages-per-platform", str(args.max_sales_pages_per_platform),
                "--max-search-pages-per-provider", str(args.max_search_pages_per_provider),
            ], max(600, args.capture_limit * 90))
            package_steps.append(record_step("build_all_detected_html_pdf", all_html))
            if all_html.returncode != 0:
                raise RuntimeError(f"All-detected-HTML PDF failed: {all_html.stderr or all_html.stdout}")
            manifest_path = run_dir / "all-detected-html-pages.manifest.json"
            if not manifest_path.is_file():
                raise RuntimeError("All-detected-HTML PDF manifest was not created")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (manifest.get("validation") or {}).get("ok") is not True:
                raise RuntimeError("All-detected-HTML PDF validation did not pass")
        summary_path = run_dir / "visual-sales-workflow-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {
            "schema_version": "1.0",
            "record_type": "visual_first_sales_workflow",
            "steps": [],
        }
        assisted_path = run_dir / "discovery" / "assisted-sales-results.json"
        assisted_report = json.loads(assisted_path.read_text(encoding="utf-8")) if assisted_path.is_file() else {}
        locked_browser = str(state.get("default_browser") or "").strip().casefold()
        locked_user_data = state.get("browser_user_data") or state.get("edge_user_data")
        summary.update({
            "started_at": summary.get("started_at") or started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "workflow_mode": "package_existing",
            "packaging_mode": "package_existing",
            "package_existing": True,
            "browser": locked_browser or summary.get("browser"),
            "browser_executable": state.get("browser_executable") or summary.get("browser_executable"),
            "browser_selection_policy": state.get("browser_selection_policy") or "edge_then_chrome",
            "browser_fallback_used": state.get("browser_fallback_used") is True,
            "browser_handoff_mode": state.get("handoff_mode") or "attach",
            "cdp_endpoint_loopback": state.get("cdp_loopback_only") is True,
            "profile_directory": state.get("profile_directory") or summary.get("profile_directory"),
            "profile_kind": state.get("profile_kind") or "dedicated_non_default",
            "browser_user_data": locked_user_data or summary.get("browser_user_data"),
            "edge_user_data": locked_user_data if locked_browser == "edge" else None,
            "system_default_profile_reused": False,
            "computer_use_used": False,
            "browser_plugin_used": False,
            "platforms": assisted_report.get("platforms_requested") or summary.get("platforms") or [],
            "query_strategy": assisted_report.get("query_strategy") or summary.get("query_strategy"),
            "planned_query_count": int(assisted_report.get("query_count") or summary.get("planned_query_count") or 0),
            "delivery_eligible_query_count": int(
                assisted_report.get("delivery_eligible_query_count")
                or summary.get("delivery_eligible_query_count") or 0
            ),
            "delivery_platform_count": int(
                assisted_report.get("delivery_platform_count") or summary.get("delivery_platform_count") or 0
            ),
            "delivery_platforms": assisted_report.get("delivery_platforms") or summary.get("delivery_platforms") or [],
            "failed_query_count": int(assisted_report.get("failed_query_count") or 0),
            "related_results_pdf": "related-web-results.pdf",
        })
        if args.all_html_pdf:
            summary.update({
                "all_detected_html_pdf": "all-detected-html-pages.pdf",
                "all_detected_html_manifest": "all-detected-html-pages.manifest.json",
                "all_detected_html_source_count": int(manifest.get("visual_source_count") or 0),
                "all_detected_html_attachment_count": int(manifest.get("embedded_html_count") or 0),
                "all_detected_html_max_sales_pages_per_platform": int(manifest.get("max_sales_pages_per_platform") or 0),
                "all_detected_html_max_search_pages_per_provider": int(manifest.get("max_search_pages_per_provider") or 0),
                "all_detected_html_platforms": manifest.get("platforms") or {},
            })
        capture_summary_path = run_dir / "capture" / "sales-after-login" / "capture-summary.json"
        if capture_summary_path.is_file():
            capture_summary = json.loads(capture_summary_path.read_text(encoding="utf-8"))
            visual_match = capture_summary.get("visual_match") or {}
            summary.update({
                "capture_status": capture_summary.get("status"),
                "captured_count": int(capture_summary.get("accepted_count") or 0),
                "visual_retained_count": int(visual_match.get("retained_count") or 0),
                "visual_near_match_count": int(visual_match.get("visual_near_match_count") or 0),
                "text_review_count": int(visual_match.get("text_review_count") or 0),
                "capture_summary": str(capture_summary_path.relative_to(run_dir)).replace("\\", "/"),
            })
        summary.setdefault("steps", []).extend(package_steps)
        write_json(summary_path, summary)
        print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
        return
    if not state_path.is_file():
        raise FileNotFoundError(f"Sales workflow state not found: {state_path}")
    if state.get("phase") not in RESUMABLE_BROWSER_PHASES:
        raise ValueError(f"Workflow is not in a resumable browser phase: {state.get('phase')!r}")
    browser_lock = validate_locked_browser_state(run_dir, config, state, require_baidu=True)
    if not browser_lock["ok"]:
        raise ValueError("Locked browser state is invalid: " + ", ".join(browser_lock["errors"]))
    state_browser = str(state.get("default_browser") or "").strip().casefold()
    if state_browser not in {"edge", "chrome"}:
        raise ValueError("Workflow state must record default_browser as edge or chrome")
    requested_browser = "edge" if args.edge_user_data or args.edge_executable else args.browser
    if requested_browser and requested_browser != state_browser:
        raise ValueError(
            f"Browser override {requested_browser!r} conflicts with workflow state {state_browser!r}"
        )
    browser = state_browser
    state_user_data = state.get("browser_user_data") or state.get("edge_user_data")
    if not state_user_data:
        raise ValueError("Workflow state does not contain browser_user_data")
    explicit_user_data = args.browser_user_data or args.edge_user_data
    if explicit_user_data and Path(explicit_user_data).resolve() != Path(state_user_data).resolve():
        raise ValueError("Browser user-data override conflicts with workflow state")
    configured_user_data = state_user_data
    user_data = Path(configured_user_data).resolve()
    state_executable = state.get("browser_executable")
    if not state_executable:
        raise ValueError("Workflow state does not contain browser_executable")
    explicit_executable = args.browser_executable or args.edge_executable
    if explicit_executable and Path(explicit_executable).resolve() != Path(state_executable).resolve():
        raise ValueError("Browser executable override conflicts with workflow state")
    executable = Path(state_executable).resolve()
    browser_label = "Google Chrome" if browser == "chrome" else "Microsoft Edge"
    handoff_mode = state.get("handoff_mode") or ("attach" if state.get("cdp_endpoint") else "restart")
    state_cdp_endpoint = state.get("cdp_endpoint")
    if args.cdp_endpoint and state_cdp_endpoint and args.cdp_endpoint.rstrip("/") != str(state_cdp_endpoint).rstrip("/"):
        raise ValueError("CDP endpoint override conflicts with workflow state")
    cdp_endpoint = state_cdp_endpoint or args.cdp_endpoint
    plan = json.loads((run_dir / "discovery" / "query-plan.json").read_text(encoding="utf-8"))
    planned_platforms = [
        str(item.get("target_platform") or "") for item in plan.get("items") or []
        if str(item.get("target_platform") or "") in QUICK_PLATFORM_ORDER
    ]
    deferred_platforms = list(dict.fromkeys(
        value for value in args.deferred_platform if value in QUICK_PLATFORM_ORDER
    ))
    if args.platform:
        platforms = list(dict.fromkeys(args.platform))
    elif args.skip_sales_search or deferred_platforms:
        platforms = []
    elif args.filing_mode:
        platforms = [item for item in FILING_PLATFORM_ORDER if item in planned_platforms]
        if len(platforms) < 3:
            platforms = list(dict.fromkeys(planned_platforms))[:3]
    else:
        platforms = list(dict.fromkeys(planned_platforms or list(QUICK_PLATFORM_ORDER)))
    coverage_platforms = list(dict.fromkeys([*platforms, *deferred_platforms]))
    if not coverage_platforms:
        coverage_platforms = list(dict.fromkeys(planned_platforms or list(QUICK_PLATFORM_ORDER)))
    if args.filing_mode and len(coverage_platforms) < 3:
        raise ValueError("Filing mode requires at least three planned sales platforms")
    pages_per_query = max(1, min(20, 5 if args.filing_mode and args.pages_per_query == 1 else args.pages_per_query))
    allow_zero_results = bool(args.allow_zero_results or args.filing_mode)
    required_success_platforms = min(
        max(3 if args.filing_mode else 1, args.min_success_platforms), max(1, len(coverage_platforms))
    )
    if user_data == run_dir or user_data.is_relative_to(run_dir):
        raise ValueError("Browser user data must stay outside RUN_DIR")
    if is_default_browser_user_data(browser, user_data):
        raise ValueError(f"The system default {browser_label} profile cannot be automated; use the dedicated trademark profile")
    if not user_data.is_dir():
        raise FileNotFoundError(f"Browser user data directory not found: {user_data}")
    if not executable.is_file():
        raise FileNotFoundError(f"{browser_label} executable not found: {executable}")
    state_profile_directory = str(state.get("profile_directory") or "").strip()
    if args.profile_directory != "auto" and state_profile_directory and args.profile_directory != state_profile_directory:
        raise ValueError("Profile-directory override conflicts with workflow state")
    profile_directory = resolve_profile_directory(
        user_data, state_profile_directory or args.profile_directory,
    )
    profile_running = browser_profile_process_is_running(browser, user_data)
    if handoff_mode == "attach":
        if not profile_running:
            raise RuntimeError(f"The dedicated trademark {browser_label} profile is not running; log in and keep it open")
        if not cdp_endpoint or not cdp_endpoint_available(cdp_endpoint, browser):
            raise RuntimeError("The loopback CDP endpoint is unavailable; relaunch the dedicated login profile in attach mode")
    elif profile_running:
        raise RuntimeError(f"The dedicated trademark {browser_label} profile is still running. Close it before resuming.")
    steps = []
    started_at = datetime.now(timezone.utc).isoformat()
    state.update({
        "phase": "stage_b_reference_running",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "public_search_status": "running",
        "sales_search_status": "not_started" if not (run_dir / "discovery" / "assisted-sales-results.json").is_file() else "existing_partial",
    })
    write_json(state_path, state)

    if not (run_dir / "reference" / "qcc-reference.json").is_file():
        qcc_command = [
            sys.executable, str(scripts / "fetch-qcc-trademark-reference.py"),
            "--run-dir", str(run_dir),
        ]
        qcc_hint = str((config.get("cherrystudio_orchestration") or {}).get("qcc_brand_url_hint") or "").strip()
        if qcc_hint:
            qcc_command.extend(["--brand-url", qcc_hint])
        completed = run(qcc_command, 60)
        steps.append(record_step("qcc_reference", completed))
        if completed.returncode != 0:
            raise RuntimeError(f"QCC reference step failed: {completed.stderr or completed.stdout}")
    qcc_guard = validate_qcc_reference(run_dir)
    if qcc_guard.get("ok") is not True:
        raise RuntimeError(f"QCC reference guard failed before search channels: {qcc_guard.get('errors')}")

    public_providers = list(dict.fromkeys(args.public_search_provider or PUBLIC_SEARCH_PROVIDERS))
    public_command = [
        sys.executable, str(scripts / "run-public-search-matrix.py"),
        "--run-dir", str(run_dir), "--browser-executable", str(executable),
        "--browser-product", browser, "--browser-user-data", str(user_data),
        "--profile-directory", profile_directory,
    ]
    for provider in public_providers:
        public_command.extend(["--provider", provider])
    for provider in list(dict.fromkeys(args.resume_public_provider)):
        public_command.extend(["--resume-provider", provider])
    if handoff_mode == "attach" and cdp_endpoint:
        public_command.extend(["--cdp-endpoint", str(cdp_endpoint)])
    artifact_lock_path = run_dir / ".locks" / "browser-evidence-capture.lock"
    public_command.extend(["--artifact-lock", str(artifact_lock_path)])

    assisted_command = [
        "node", str(scripts / "sales-platform-assisted-discover.mjs"),
        "--run-dir", str(run_dir), "--browser", browser,
        "--browser-executable", str(executable), "--user-data-dir", str(user_data),
        "--profile-directory", profile_directory, "--limit", str(max(1, min(50, args.search_limit))),
        "--python", sys.executable,
        "--timeout-ms", str(max(10000, args.per_query_timeout_ms)),
        "--pages-per-query", str(pages_per_query),
        "--task-source", "manual-capture-queue",
        "--merge-existing", "--artifact-lock", str(artifact_lock_path),
    ]
    if handoff_mode == "attach":
        assisted_command.extend(["--cdp-endpoint", str(cdp_endpoint)])
    if state.get("browser_fallback_used") is True:
        assisted_command.append("--browser-fallback-used")
    if allow_zero_results:
        assisted_command.append("--include-zero-results")
    for platform in platforms:
        assisted_command.extend(["--platform", platform])
    manual_queue_path = run_dir / "discovery" / "manual-capture-queue.json"
    manual_queue = json.loads(manual_queue_path.read_text(encoding="utf-8")) if manual_queue_path.is_file() else {}
    planned_task_ids = [
        str(item.get("task_id") or "") for item in manual_queue.get("items") or []
        if item.get("platform") in coverage_platforms and item.get("task_id")
    ]
    planned_query_count = sum(
        1 for item in manual_queue.get("items") or [] if item.get("platform") in coverage_platforms
    )
    planned_by_id = {
        str(item.get("task_id") or ""): item for item in manual_queue.get("items") or []
        if item.get("platform") in coverage_platforms and item.get("task_id")
    }
    assisted_report_path = run_dir / "discovery" / "assisted-sales-results.json"
    existing_assisted = json.loads(assisted_report_path.read_text(encoding="utf-8")) if assisted_report_path.is_file() else {}
    existing_assisted = upgrade_legacy_query_bindings(
        run_dir, existing_assisted, planned_by_id, assisted_report_path,
    )
    existing_success_ids = {
        str(item.get("query_id") or "") for item in existing_assisted.get("platform_runs") or []
        if item.get("query_id") and reusable_assisted_run(
            run_dir, item, planned_by_id.get(str(item.get("query_id") or "")),
        )
    }
    pending_task_ids = [task_id for task_id in planned_task_ids if task_id not in existing_success_ids]
    runnable_pending_task_ids = [
        task_id for task_id in pending_task_ids
        if (planned_by_id.get(task_id) or {}).get("platform") in platforms
    ]
    if existing_assisted and runnable_pending_task_ids and len(runnable_pending_task_ids) < len(planned_task_ids):
        for query_id in runnable_pending_task_ids:
            assisted_command.extend(["--query-id", query_id])
    reuse_complete_assisted = bool(planned_task_ids) and not pending_task_ids and assisted_report_path.is_file()

    channel_results: dict[str, subprocess.CompletedProcess] = {}
    channel_jobs: dict[str, tuple[list[str], int]] = {}
    if args.skip_public_search or args.skip_search:
        channel_results["public"] = subprocess.CompletedProcess(
            public_command, 0, stdout="public search skipped; reusing existing provider archives", stderr="",
        )
    else:
        public_plan_for_timeout = json.loads(
            (run_dir / "discovery" / "public-search-plan.json").read_text(encoding="utf-8")
        )
        planned_public_task_count = sum(
            1 for item in public_plan_for_timeout.get("items") or []
            if item.get("provider") in public_providers
        )
        channel_jobs["public"] = (
            public_command, public_search_matrix_wall_timeout_seconds(
                planned_public_task_count, len(public_providers),
            ),
        )
    if args.skip_search or args.skip_sales_search or reuse_complete_assisted:
        if not assisted_report_path.is_file() and not args.skip_sales_search:
            raise FileNotFoundError(f"Existing assisted sales report not found: {assisted_report_path}")
        channel_results["sales"] = subprocess.CompletedProcess(
            assisted_command, 0,
            stdout=(
                "sales search deferred; no sales website request was made"
                if args.skip_sales_search else
                f"search skipped or already complete; reusing {assisted_report_path}"
            ), stderr="",
        )
    else:
        channel_jobs["sales"] = (
            assisted_command,
            sales_channel_wall_timeout_seconds(
                max(1, len(runnable_pending_task_ids)), pages_per_query,
            ),
        )
    state.update({
        "phase": "stage_b_parallel_search_running",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "public_search_status": "running" if "public" in channel_jobs else "reused",
        "sales_search_status": "running" if "sales" in channel_jobs else "deferred" if args.skip_sales_search else "reused",
        "sales_search_runnable_platforms": platforms,
        "sales_search_deferred_platforms": deferred_platforms,
        "search_concurrency": {
            "max_channels": MAX_PARALLEL_SEARCH_CHANNELS,
            "same_domain_concurrency": MAX_SAME_DOMAIN_REQUESTS,
            "artifact_capture_concurrency": MAX_PARALLEL_ARTIFACT_CAPTURES,
            "artifact_lock": str(artifact_lock_path.relative_to(run_dir)).replace("\\", "/"),
        },
    })
    write_json(state_path, state)
    channel_results.update(run_search_channels(channel_jobs))
    public_search = channel_results["public"]
    assisted = channel_results["sales"]
    steps.append(record_step("public_search_goods_matrix", public_search))
    steps.append(record_step("logged_in_platform_search", assisted))
    if public_search.returncode != 0:
        raise RuntimeError(f"Public-search matrix failed: {public_search.stderr or public_search.stdout}")
    public_matrix_path = run_dir / "discovery" / "public-search-matrix.json"
    public_matrix = json.loads(public_matrix_path.read_text(encoding="utf-8")) if public_matrix_path.is_file() else {}
    public_runs = public_matrix.get("provider_runs") or []
    public_plan_path = run_dir / "discovery" / "public-search-plan.json"
    public_plan = json.loads(public_plan_path.read_text(encoding="utf-8")) if public_plan_path.is_file() else {}
    expected_public_ids = {
        (str(item.get("provider") or ""), str(item.get("query_id") or ""))
        for item in public_plan.get("items") or []
        if item.get("provider") in public_providers
    }
    actual_public_ids = {
        (str(item.get("provider") or ""), str(item.get("query_id") or ""))
        for item in public_runs
    }
    pending_public = [
        item for item in public_runs
        if item.get("state") not in PUBLIC_DELIVERY_STATES
    ]
    public_identity_complete = bool(expected_public_ids) and actual_public_ids == expected_public_ids
    public_pending_outcome = None
    if not public_runs or pending_public or not public_identity_complete:
        blocked_public = [
            item for item in pending_public if str(item.get("state") or "") in PUBLIC_BLOCKING_STATES
        ]
        direct_human_blockers = [
            item for item in blocked_public
            if str(item.get("state") or "") in PUBLIC_HUMAN_VERIFICATION_STATES
        ]
        direct_risk_blockers = [
            item for item in blocked_public
            if str(item.get("state") or "") in PUBLIC_RISK_BLOCKING_STATES
            or str(item.get("state") or "") == "deferred_provider_internal_cooldown"
        ]
        pending_providers = sorted(set(
            str(item.get("provider") or "") for item in pending_public if item.get("provider")
        )) or public_providers
        pending_tasks = [{
            "provider": str(item.get("provider") or ""),
            "query_id": str(item.get("query_id") or ""),
            "query": str(item.get("query") or ""),
            "state": str(item.get("state") or "missing_task"),
            "automatic_retry_allowed": item.get("automatic_retry_allowed") is True,
            "url": item.get("final_url") or item.get("search_url"),
            "title": item.get("title"),
            "manual_verification_tab_kept_open": item.get("manual_verification_tab_kept_open") is True,
            "manual_verification_required": item.get("manual_verification_required") is True,
            "verification_target_id": (item.get("verification_page") or {}).get("target_id"),
            "internal_safety_cooldown": item.get("internal_safety_cooldown") is True,
            "cooldown_until": item.get("cooldown_until"),
        } for item in pending_public]
        verification_tab_ready = bool(direct_human_blockers) and all(
            item.get("manual_verification_tab_kept_open") is True for item in direct_human_blockers
        )
        verification_required = bool(direct_human_blockers) and verification_tab_ready
        risk_cooldown_required = bool(direct_risk_blockers)
        phase = (
            "public_search_verification_required" if verification_required
            else "public_search_internal_cooldown" if risk_cooldown_required
            else "public_search_capture_retry_required"
        )
        public_pending_outcome = {
            "status": "awaiting_manual_login" if verification_required else "incomplete",
            "phase": phase,
            "pending_providers": pending_providers,
            "pending_tasks": pending_tasks,
            "exit_code": 6 if verification_required else 10 if risk_cooldown_required else 8,
        }
        state.update({
            "phase": phase,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "public_search_pending_providers": pending_providers,
            "public_search_pending_count": len(pending_public) if public_runs else len(public_providers),
            "public_search_pending_tasks": pending_tasks,
            "public_search_identity_complete": public_identity_complete,
            "public_search_verification_tab_ready": verification_tab_ready,
            "public_search_status": "verification_required" if verification_required else "capture_retry_required",
            "sales_search_status": "completed_pending_evaluation",
            "resume_requires": (
                "handle_public_search_verification_and_keep_browser_open"
                if verification_required else
                "wait_for_internal_public_search_safety_timer"
                if risk_cooldown_required else
                "resume_exact_command_for_bounded_technical_recovery"
            ),
        })
        write_json(state_path, state)
    else:
        state.pop("public_search_pending_providers", None)
        state.pop("public_search_pending_count", None)
        state.pop("public_search_pending_tasks", None)
    state.update({
        "phase": "stage_b_parallel_search_evaluating",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "public_search_status": "complete" if public_pending_outcome is None else "incomplete",
        "sales_search_status": (
            "completed_pending_evaluation" if assisted.returncode == 0 else "technical_failure"
        ),
    })
    write_json(state_path, state)
    if assisted.returncode != 0:
        live_progress_path = run_dir / "discovery" / "sales-live-progress.json"
        live_progress = (
            json.loads(live_progress_path.read_text(encoding="utf-8"))
            if live_progress_path.is_file() else {}
        )
        failure_detail = (assisted.stderr or assisted.stdout or "").strip()
        if not failure_detail:
            failure_detail = str(live_progress.get("last_error") or "child process returned no diagnostic text")
        state.update({
            "phase": "sales_platform_capture_retry_required",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sales_search_status": "technical_failure",
            "sales_search_child_return_code": assisted.returncode,
            "sales_search_child_stdout_tail": (assisted.stdout or "")[-4000:],
            "sales_search_child_stderr_tail": (assisted.stderr or "")[-4000:],
            "sales_search_live_progress": live_progress or None,
            "resume_requires": "resume_exact_command_for_bounded_technical_recovery",
        })
        write_json(state_path, state)
        raise RuntimeError(
            f"Logged-in platform search failed (return code {assisted.returncode}): {failure_detail}"
        )
    assisted_report = json.loads(assisted_report_path.read_text(encoding="utf-8")) if assisted_report_path.is_file() else {}
    assisted_runs = assisted_report.get("platform_runs") or []
    latest_run_by_id = {
        str(item.get("query_id") or ""): item
        for item in assisted_runs if isinstance(item, dict) and item.get("query_id")
    }
    pending_sales = []
    for task_id in planned_task_ids:
        planned = planned_by_id[task_id]
        observed = latest_run_by_id.get(task_id)
        if observed is not None and reusable_assisted_run(run_dir, observed, planned):
            continue
        pending_sales.append(observed if observed is not None else {
            "platform": planned.get("platform"),
            "query_id": task_id,
            "search_query": planned.get("query"),
            "target_good": planned.get("target_good"),
            "expected_search_url": planned.get("search_url"),
            "final_state": "missing_task",
            "delivery_eligible": False,
            "page_runs": [],
        })
    if len(assisted_runs) != planned_query_count or pending_sales:
        pending_platforms = sorted(set(
            str(item.get("platform") or "") for item in pending_sales if item.get("platform")
        )) or platforms
        outcome = classify_incomplete_sales(run_dir, pending_sales, pending_platforms)
        verification_target = next((
            (item.get("verification_page") or {}).get("target_id")
            for item in pending_sales
            if (item.get("verification_page") or {}).get("target_id")
        ), None)
        if not verification_target and public_pending_outcome is not None:
            verification_target = next((
                item.get("verification_target_id")
                for item in public_pending_outcome.get("pending_tasks") or []
                if item.get("verification_target_id")
            ), None)
        verification_target_activated = activate_cdp_target(cdp_endpoint, verification_target)
        state.update({
            "phase": outcome["phase"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sales_search_pending_platforms": pending_platforms,
            "sales_search_pending_count": len(pending_sales),
            "sales_search_status": "incomplete",
            "active_sales_cooldown_platforms": outcome["active_cooldown_platforms"],
            "sales_verification_platforms": outcome["verification_platforms"],
            "sales_runnable_pending": outcome["runnable_pending"],
            "sales_cooling_pending": outcome["cooling_pending"],
            "sales_verification_pending": outcome["verification_pending"],
            "sales_runnable_pending_count": outcome["runnable_pending_count"],
            "sales_cooling_pending_count": outcome["cooling_pending_count"],
            "sales_verification_pending_count": outcome["verification_pending_count"],
            "resume_requires": outcome["resume_requires"],
            "verification_target_activated": verification_target_activated,
        })
        selected_outcome = outcome
        if (
            outcome["runnable_pending_count"] == 0
            and public_pending_outcome is not None
            and public_pending_outcome["exit_code"] in {6, 8}
        ):
            selected_outcome = public_pending_outcome
            state.update({
                "phase": public_pending_outcome["phase"],
                "public_search_status": (
                    "verification_required" if public_pending_outcome["exit_code"] == 6
                    else "capture_retry_required"
                ),
                "resume_requires": (
                    "handle_public_search_verification_and_keep_browser_open"
                    if public_pending_outcome["exit_code"] == 6 else
                    "resume_runnable_public_search_tasks_while_sales_platforms_stay_deferred"
                ),
            })
        write_json(state_path, state)
        print(json.dumps({
            "status": selected_outcome["status"],
            "phase": selected_outcome["phase"],
            "pending_platforms": pending_platforms,
            "runnable_platforms": outcome["runnable_platforms"],
            "cooling_platforms": outcome["active_cooldown_platforms"],
            "verification_platforms": outcome["verification_platforms"],
        }, ensure_ascii=False, indent=2))
        raise SystemExit(selected_outcome["exit_code"])
    state.pop("sales_search_pending_platforms", None)
    state.pop("sales_search_pending_count", None)
    state.pop("active_sales_cooldown_platforms", None)
    state.pop("sales_verification_platforms", None)
    state.pop("sales_runnable_pending", None)
    state.pop("sales_cooling_pending", None)
    state.pop("sales_verification_pending", None)
    state.pop("sales_runnable_pending_count", None)
    state.pop("sales_cooling_pending_count", None)
    state.pop("sales_verification_pending_count", None)
    state.pop("resume_requires", None)
    state.update({
        "phase": "stage_b_capture_and_packaging_running",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sales_search_status": "complete",
    })
    write_json(state_path, state)
    if public_pending_outcome is not None:
        verification_target = next((
            item.get("verification_target_id")
            for item in public_pending_outcome.get("pending_tasks") or []
            if item.get("verification_target_id")
        ), None)
        verification_target_activated = activate_cdp_target(cdp_endpoint, verification_target)
        state.update({
            "phase": public_pending_outcome["phase"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "public_search_status": "verification_required" if public_pending_outcome["exit_code"] == 6
            else "internal_cooldown" if public_pending_outcome["exit_code"] == 10
            else "capture_retry_required",
            "sales_search_status": "complete",
            "verification_target_activated": verification_target_activated,
        })
        write_json(state_path, state)
        print(json.dumps({
            **public_pending_outcome,
            "sales_search_status": "complete",
            "independent_sales_channel_completed": True,
        }, ensure_ascii=False, indent=2))
        raise SystemExit(public_pending_outcome["exit_code"])
    if not args.skip_search and handoff_mode != "attach" and not wait_for_browser_exit(browser, user_data):
        raise RuntimeError(f"The {browser_label} process launched for platform search did not exit cleanly")

    pagination_validation = {}
    if pages_per_query > 1:
        validation_command = [
            sys.executable, str(scripts / "validate-sales-pagination.py"),
            "--run-dir", str(run_dir),
            "--min-platforms", str(required_success_platforms),
            "--required-pages-per-query", str(pages_per_query),
            "--require-all-planned-queries",
        ]
        if allow_zero_results:
            validation_command.append("--allow-zero-results")
        validation = run(validation_command, 180)
        steps.append(record_step("validate_continuous_pagination", validation))
        validation_path = run_dir / "sales-pagination-validation.json"
        if validation_path.is_file():
            pagination_validation = json.loads(validation_path.read_text(encoding="utf-8"))
        if validation.returncode != 0:
            raise RuntimeError(f"Continuous pagination validation failed: {validation.stderr or validation.stdout}")

    build = run([
        sys.executable, str(scripts / "build-sales-platform-results.py"),
        "--run-dir", str(run_dir), "--limit", "50",
    ], 60)
    steps.append(record_step("build_candidates", build))
    if build.returncode != 0:
        raise RuntimeError(f"Candidate build failed: {build.stderr or build.stdout}")

    if args.menu_only:
        capture = subprocess.CompletedProcess([], 0, stdout="menu_only: direct product capture skipped", stderr="")
        steps.append(record_step("capture_and_visual_match", capture))
    else:
        capture_command = [
            sys.executable, str(scripts / "capture-sales-after-login.py"),
            "--run-dir", str(run_dir), "--browser", browser,
            "--browser-user-data", str(user_data),
            "--browser-executable", str(executable), "--profile-directory", profile_directory,
            "--limit", str(max(1, min(20, args.capture_limit))),
            "--visual-threshold", str(args.visual_threshold),
            "--allow-diagnostic-detail-skips",
        ]
        if handoff_mode == "attach":
            capture_command.extend(["--cdp-endpoint", str(cdp_endpoint)])
        for platform in platforms:
            capture_command.extend(["--platform", platform])
        capture = run(capture_command, max(300, args.capture_limit * 90))
        steps.append(record_step("capture_and_visual_match", capture))

    capture_summary_path = run_dir / "capture" / "sales-after-login" / "capture-summary.json"
    if not args.menu_only:
        try:
            capture_summary = json.loads(capture_summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            capture_summary = {}
        capture_ok = capture_result_is_complete(capture.returncode, capture_summary)
        if not capture_ok:
            quarantined = quarantine_provisional_deliverables(run_dir)
            latest_state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else state
            latest_state.update({
                "phase": "capture_failed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "capture_status": capture_summary.get("status") or "failed",
                "capture_return_code": capture.returncode,
                "capture_summary": (
                    str(capture_summary_path.relative_to(run_dir)).replace("\\", "/")
                    if capture_summary_path.is_file() else None
                ),
                "quarantined_provisional_deliverables": quarantined,
            })
            write_json(state_path, latest_state)
            print(json.dumps({
                "status": "incomplete", "phase": "capture_failed",
                "capture_return_code": capture.returncode,
                "capture_status": latest_state["capture_status"],
                "quarantined_provisional_deliverables": quarantined,
            }, ensure_ascii=False, indent=2))
            raise SystemExit(4)

    related_command = [
        sys.executable, str(scripts / "build-related-results-pdf.py"),
        "--run-dir", str(run_dir),
    ]
    if args.menu_only:
        related_command.extend(["--menu-only", "--min-platforms", str(required_success_platforms)])
        if pages_per_query > 1:
            related_command.extend(["--required-pages-per-query", str(pages_per_query)])
    related = run(related_command, 180)
    steps.append(record_step("build_related_results_pdf", related))
    if related.returncode != 0:
        raise RuntimeError(f"Related-results PDF failed: {related.stderr or related.stdout}")

    all_html_manifest = {}
    if args.all_html_pdf:
        all_html = run([
            sys.executable, str(scripts / "build-all-detected-html-pdf.py"),
            "--run-dir", str(run_dir),
            "--max-sales-pages-per-platform", str(args.max_sales_pages_per_platform),
            "--max-search-pages-per-provider", str(args.max_search_pages_per_provider),
        ], max(600, args.capture_limit * 90))
        steps.append(record_step("build_all_detected_html_pdf", all_html))
        if all_html.returncode != 0:
            raise RuntimeError(f"All-detected-HTML PDF failed: {all_html.stderr or all_html.stdout}")
        all_html_manifest_path = run_dir / "all-detected-html-pages.manifest.json"
        if not all_html_manifest_path.is_file():
            raise RuntimeError("All-detected-HTML PDF manifest was not created")
        all_html_manifest = json.loads(all_html_manifest_path.read_text(encoding="utf-8"))
        if (all_html_manifest.get("validation") or {}).get("ok") is not True:
            raise RuntimeError("All-detected-HTML PDF validation did not pass")

    visual = {}
    visual_path = run_dir / "visual-match-results.json"
    if visual_path.is_file():
        visual = json.loads(visual_path.read_text(encoding="utf-8"))
    capture_summary = {}
    if not args.menu_only and capture_summary_path.is_file():
        capture_summary = json.loads(capture_summary_path.read_text(encoding="utf-8"))
    related_pdf = run_dir / ("sales-menu-results.pdf" if args.menu_only else "related-web-results.pdf")
    assisted_report = json.loads((run_dir / "discovery" / "assisted-sales-results.json").read_text(encoding="utf-8"))
    summary = {
        "schema_version": "1.0",
        "record_type": "visual_first_sales_workflow",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "browser": browser,
        "browser_executable": str(executable),
        "browser_selection_policy": "edge_then_chrome",
        "browser_fallback_used": state.get("browser_fallback_used") is True,
        "browser_handoff_mode": handoff_mode,
        "cdp_endpoint_loopback": bool(cdp_endpoint and handoff_mode == "attach"),
        "profile_directory": profile_directory,
        "profile_kind": "dedicated_non_default",
        "browser_user_data": str(user_data),
        "edge_user_data": str(user_data) if browser == "edge" else None,
        "system_default_profile_reused": False,
        "workflow_mode": "menu_only" if args.menu_only else "direct_page_capture",
        "computer_use_used": False,
        "browser_plugin_used": False,
        "platforms": platforms,
        "query_strategy": assisted_report.get("query_strategy"),
        "filing_mode": bool(args.filing_mode),
        "pages_per_query": pages_per_query,
        "allow_zero_results": allow_zero_results,
        "planned_query_count": int(assisted_report.get("query_count") or 0),
        "delivery_eligible_query_count": int(assisted_report.get("delivery_eligible_query_count") or 0),
        "delivery_platform_count": int(assisted_report.get("delivery_platform_count") or 0),
        "delivery_platforms": assisted_report.get("delivery_platforms") or [],
        "failed_query_count": int(assisted_report.get("failed_query_count") or 0),
        "minimum_success_platforms": required_success_platforms if args.menu_only else None,
        "pagination_validation_ok": pagination_validation.get("ok") if pages_per_query > 1 else None,
        "pagination_qualified_platforms": pagination_validation.get("qualified_platforms") or [],
        "capture_status": capture_summary.get("status"),
        "captured_count": int(capture_summary.get("accepted_count") or 0),
        "visual_retained_count": 0 if args.menu_only else int(visual.get("retained_count") or 0),
        "visual_near_match_count": 0 if args.menu_only else int(visual.get("visual_near_match_count") or 0),
        "text_review_count": 0 if args.menu_only else int(visual.get("text_review_count") or 0),
        "related_results_pdf": str(related_pdf.relative_to(run_dir)).replace("\\", "/") if related_pdf.is_file() else None,
        "all_detected_html_pdf": (
            "all-detected-html-pages.pdf" if args.all_html_pdf and (run_dir / "all-detected-html-pages.pdf").is_file()
            else None
        ),
        "all_detected_html_manifest": (
            "all-detected-html-pages.manifest.json" if args.all_html_pdf and all_html_manifest else None
        ),
        "all_detected_html_source_count": int(all_html_manifest.get("visual_source_count") or 0),
        "all_detected_html_attachment_count": int(all_html_manifest.get("embedded_html_count") or 0),
        "all_detected_html_max_pages_per_platform": int(all_html_manifest.get("max_pages_per_platform") or 0),
        "all_detected_html_max_sales_pages_per_platform": int(all_html_manifest.get("max_sales_pages_per_platform") or 0),
        "all_detected_html_max_search_pages_per_provider": int(all_html_manifest.get("max_search_pages_per_provider") or 0),
        "all_detected_html_platforms": all_html_manifest.get("platforms") or {},
        "steps": steps,
    }
    summary_path = run_dir / "visual-sales-workflow-summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else state
    state.update({
        "phase": "capture_complete",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "public_search_status": "complete",
        "sales_search_status": "complete",
        "capture_summary": str(summary_path.relative_to(run_dir)).replace("\\", "/"),
    })
    write_json(state_path, state)
    print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
