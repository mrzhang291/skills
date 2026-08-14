#!/usr/bin/env python3
"""Run a rate-limited public-search matrix for the trademark and every good."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edge_profile import preferred_chromium_browser
from process_utils import run_bounded
from audit_cherrystudio_run import sha256, valid_image, validate_pdf
from runtime_policy import (
    DEFAULT_PUBLIC_SEARCH_BATCH_PAUSE_MAX_SEC,
    DEFAULT_PUBLIC_SEARCH_BATCH_PAUSE_MIN_SEC,
    DEFAULT_PUBLIC_SEARCH_BATCH_SIZE,
    DEFAULT_PUBLIC_SEARCH_LIMIT,
    DEFAULT_PUBLIC_SEARCH_MAX_DELAY_SEC,
    DEFAULT_PUBLIC_SEARCH_MIN_DELAY_SEC,
    DEFAULT_PUBLIC_SEARCH_RISK_COOLDOWN_MAX_SEC,
    DEFAULT_PUBLIC_SEARCH_RISK_COOLDOWN_MIN_SEC,
    DEFAULT_PUBLIC_SEARCH_TIMEOUT_MS,
    PUBLIC_SEARCH_PROVIDERS,
    MAX_SAME_DOMAIN_REQUESTS,
)


HUMAN_VERIFICATION_STATES = {"captcha", "login_required"}
RISK_BLOCKING_STATES = {"access_denied", "rate_limited"}
BLOCKING_STATES = HUMAN_VERIFICATION_STATES | RISK_BLOCKING_STATES
COMPLETED_STATES = {"normal", "zero_results", "no_extractable_results"}
DELIVERY_STATES = {"normal", "zero_results"}


def normalize_query(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("+", " ").replace("＋", " ")).strip()


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def remaining_interval_seconds(
    target_seconds: float,
    previous_request_at: datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[float, float | None]:
    if previous_request_at is None:
        return max(0.0, float(target_seconds)), None
    current = now or datetime.now(timezone.utc)
    elapsed = max(0.0, (current - previous_request_at).total_seconds())
    return max(0.0, float(target_seconds) - elapsed), elapsed


def cdp_page_targets(endpoint: str | None) -> list[dict]:
    if not endpoint:
        return []
    try:
        with urlopen(endpoint.rstrip("/") + "/json/list", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return []
    return [value for value in payload if isinstance(value, dict) and value.get("type") == "page"]


def public_target_looks_protected(target: dict) -> bool:
    sample = f"{target.get('title') or ''}\n{target.get('url') or ''}"
    return bool(re.search(r"验证码|安全验证|滑块|captcha|qcaptcha|risk_handler|punish|login", sample, re.I))


def close_cdp_target(endpoint: str | None, target_id: object) -> bool:
    if not endpoint or not target_id:
        return False
    try:
        with urlopen(f"{endpoint.rstrip('/')}/json/close/{target_id}", timeout=3):
            return True
    except Exception:
        return False


def build_tasks(config: dict, providers: list[str], include_base: bool = True) -> list[dict]:
    trademark = config.get("trademark") or {}
    mark = normalize_query(trademark.get("name") or "")
    owner = normalize_query(trademark.get("owner") or "")
    registration = normalize_query(trademark.get("registration_number") or "")
    goods = [normalize_query(value) for value in trademark.get("goods_services") or [] if normalize_query(value)]
    if not mark or not goods:
        raise ValueError("run-config.json must contain trademark.name and trademark.goods_services")
    terms = []
    if include_base:
        terms.append({
            "suffix": "BASE", "query_kind": "owner_mark_registration",
            "target_good": None,
            "query": " ".join(value for value in (owner, mark, registration) if value),
        })
    terms.extend({
        "suffix": f"G{index:03d}", "query_kind": "mark_plus_good",
        "target_good": good, "query": f"{mark} {good}",
    } for index, good in enumerate(goods, start=1))
    tasks = []
    # Round-robin providers so one provider's mandatory rest is naturally
    # occupied by the other provider without increasing either domain's rate.
    for term in terms:
        for provider in providers:
            prefix = "SO360" if provider == "so360" else provider.upper()
            tasks.append({
                "query_id": f"PUBLIC-{prefix}-{term['suffix']}",
                "provider": provider,
                "query_kind": term["query_kind"],
                "target_good": term["target_good"],
                "query": term["query"],
            })
    return tasks


def default_browser() -> Path:
    return preferred_chromium_browser()[1]


def validate_cdp_endpoint(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("--cdp-endpoint must use an HTTP(S) loopback address")
    return str(value).strip().rstrip("/")


def probe_cdp_identity(endpoint: str, expected_browser: str) -> dict:
    try:
        with urlopen(endpoint.rstrip("/") + "/json/version", timeout=3) as response:
            value = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"CDP endpoint is unavailable: {exc}") from exc
    product = str(value.get("Browser") or "").casefold()
    matches = (
        expected_browser == "edge" and ("edg/" in product or "microsoft edge" in product)
    ) or (
        expected_browser == "chrome" and "chrome/" in product and "edg/" not in product
    )
    if not value.get("webSocketDebuggerUrl") or not matches:
        raise RuntimeError(f"CDP product mismatch for {expected_browser}: {value.get('Browser')!r}")
    return value


def controlled_wait(seconds: float, provider: str, query_id: str) -> None:
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        chunk = min(30.0, remaining)
        print(json.dumps({
            "state": "rate_limit_wait", "provider": provider, "query_id": query_id,
            "remaining_seconds": round(remaining, 1),
        }, ensure_ascii=False), flush=True)
        time.sleep(chunk)
        remaining -= chunk


def move_to_diagnostics(run_dir: Path, output_dir: Path) -> Path:
    diagnostics = run_dir / "capture-diagnostics" / "public-search" / output_dir.name
    diagnostics.parent.mkdir(parents=True, exist_ok=True)
    if diagnostics.exists():
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        diagnostics = diagnostics.with_name(f"{diagnostics.name}-retry-{suffix}")
    if output_dir.exists():
        shutil.move(str(output_dir), str(diagnostics))
    return diagnostics


def _same_resolved_path(left: object, right: object) -> bool:
    if not left or not right:
        return left == right
    try:
        return os.path.normcase(str(Path(str(left)).resolve())) == os.path.normcase(
            str(Path(str(right)).resolve())
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _cdp_product_matches(browser: object, product: object) -> bool:
    expected = str(browser or "").strip().casefold()
    detected = str(product or "").strip().casefold()
    return (
        (expected == "edge" and ("edg/" in detected or "microsoft edge" in detected))
        or (expected == "chrome" and "chrome/" in detected and "edg/" not in detected)
    )


def browser_attachment_compatible(
    existing: object, current: object, endpoint_recovery: object = None,
) -> bool:
    """Accept an old CDP port only after a validated same-profile recovery."""
    if not isinstance(existing, dict) or not isinstance(current, dict):
        return False
    if existing == current:
        return True
    stable_fields = ("attached_to_existing_browser", "browser_product", "profile_directory")
    if any(existing.get(field) != current.get(field) for field in stable_fields):
        return False
    if existing.get("attached_to_existing_browser") is not True:
        return False
    if current.get("browser_product") not in {"edge", "chrome"} or not current.get("profile_directory"):
        return False
    if not _cdp_product_matches(existing.get("browser_product"), existing.get("cdp_browser")):
        return False
    if not _cdp_product_matches(current.get("browser_product"), current.get("cdp_browser")):
        return False
    if not all(
        existing.get(field) and current.get(field)
        for field in ("browser_executable", "browser_user_data")
    ):
        return False
    if not _same_resolved_path(existing.get("browser_executable"), current.get("browser_executable")):
        return False
    if not _same_resolved_path(existing.get("browser_user_data"), current.get("browser_user_data")):
        return False
    previous_endpoint = str(existing.get("cdp_endpoint") or "").strip().rstrip("/")
    current_endpoint = str(current.get("cdp_endpoint") or "").strip().rstrip("/")
    if not previous_endpoint or not current_endpoint:
        return False
    try:
        validate_cdp_endpoint(previous_endpoint)
        validate_cdp_endpoint(current_endpoint)
    except ValueError:
        return False
    if previous_endpoint == current_endpoint:
        return True
    recovery = endpoint_recovery
    return bool(
        isinstance(recovery, dict)
        and recovery.get("schema_version") == "1.0"
        and recovery.get("record_type") == "validated_cdp_endpoint_recovery"
        and recovery.get("source") == "same_dedicated_profile_loopback_cdp"
        and str(recovery.get("previous_endpoint") or "").strip().rstrip("/") == previous_endpoint
        and str(recovery.get("endpoint") or "").strip().rstrip("/") == current_endpoint
        and str(recovery.get("expected_browser") or "").strip().casefold()
        == str(current.get("browser_product") or "").strip().casefold()
        and _cdp_product_matches(current.get("browser_product"), recovery.get("detected_browser"))
        and str(recovery.get("detected_browser") or "").strip()
        == str(current.get("cdp_browser") or "").strip()
        and recovery.get("same_browser_user_data") is True
        and recovery.get("same_browser_executable") is True
        and recovery.get("same_profile_directory") is True
    )


def reusable_public_capture(
    output_dir: Path, existing: dict, task: dict, attachment: dict,
    endpoint_recovery: object = None,
) -> bool:
    if not (
        existing.get("provider") == task.get("provider")
        and existing.get("query") == task.get("query")
        and existing.get("query_id") == task.get("query_id")
        and existing.get("state") in DELIVERY_STATES
        and browser_attachment_compatible(
            existing.get("browser_attachment"), attachment, endpoint_recovery,
        )
    ):
        return False
    artifacts = existing.get("artifacts") or {}
    integrity = existing.get("artifact_integrity") or {}
    migrated_integrity = not bool(integrity)
    if migrated_integrity:
        integrity = {}
    required = {"html": "serp.html", "screenshot": "serp.png", "pdf": "serp.pdf"}
    for key, filename in required.items():
        path = output_dir / filename
        valid = valid_image(path) if key == "screenshot" else path.is_file() and path.stat().st_size > 0
        if not valid or artifacts.get(key) != filename:
            return False
        record = integrity.get(key) or {}
        if migrated_integrity:
            integrity[key] = {
                "path": filename,
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        elif (
            record.get("path") != filename
            or int(record.get("size_bytes") or -1) != path.stat().st_size
            or str(record.get("sha256") or "").casefold() != sha256(path)
        ):
            return False
    if not validate_pdf(output_dir / "serp.pdf")[0]:
        return False
    if migrated_integrity:
        existing["artifact_integrity"] = integrity
        existing["artifact_integrity_migrated_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output_dir / "results.json", existing)
    return True


def recover_screenshot_from_pdf(output_dir: Path) -> dict | None:
    """Repair a missing SERP preview locally without issuing another search."""
    pdf_path = output_dir / "serp.pdf"
    screenshot_path = output_dir / "serp.png"
    if not validate_pdf(pdf_path)[0]:
        return None
    document = fitz.open(pdf_path)
    try:
        if document.page_count < 1:
            return None
        pixmap = document[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        pixmap.save(screenshot_path)
    finally:
        document.close()
    if not valid_image(screenshot_path):
        return None
    return {
        "method": "render_native_pdf_preview",
        "source": "serp.pdf",
        "output": "serp.png",
        "network_request_performed": False,
        "recovered_at": datetime.now(timezone.utc).isoformat(),
    }


def recover_latest_diagnostic_capture(
    run_dir: Path, output_dir: Path, task: dict, attachment: dict,
    endpoint_recovery: object = None,
) -> dict | None:
    diagnostic_root = run_dir / "capture-diagnostics" / "public-search"
    candidates = sorted(
        diagnostic_root.glob(f"{output_dir.name}*"),
        key=lambda value: value.stat().st_mtime if value.exists() else 0,
        reverse=True,
    )
    for candidate in candidates:
        report = read_json(candidate / "results.json", {}) or {}
        if reusable_public_capture(
            candidate, report, task, attachment, endpoint_recovery,
        ):
            if output_dir.exists():
                move_to_diagnostics(run_dir, output_dir)
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate), str(output_dir))
            report.update({
                "artifact_dir": output_dir.relative_to(run_dir).as_posix(),
                "reused_existing": True,
                "attachment_recovery": {
                    "network_request_performed": False,
                    "reason": "validated_same_profile_cdp_endpoint_recovery",
                    "recovered_at": datetime.now(timezone.utc).isoformat(),
                    "previous_cdp_endpoint": (
                        (report.get("browser_attachment") or {}).get("cdp_endpoint")
                    ),
                    "current_cdp_endpoint": attachment.get("cdp_endpoint"),
                },
            })
            report.pop("diagnostic_dir", None)
            write_json(output_dir / "results.json", report)
            return report
        if not (
            report.get("state") == "artifact_invalid"
            and report.get("provider") == task.get("provider")
            and report.get("query") == task.get("query")
            and report.get("query_id") == task.get("query_id")
            and browser_attachment_compatible(
                report.get("browser_attachment"), attachment, endpoint_recovery,
            )
            and (candidate / "serp.html").is_file()
            and (candidate / "serp.html").stat().st_size > 0
        ):
            continue
        observed_state = str(report.get("observed_page_state") or "").strip()
        if observed_state not in DELIVERY_STATES:
            observed_state = "normal" if int(report.get("raw_result_count") or report.get("result_count") or 0) > 0 else ""
        if observed_state not in DELIVERY_STATES:
            continue
        artifact_recovery = recover_screenshot_from_pdf(candidate)
        if artifact_recovery is None:
            continue
        if output_dir.exists():
            move_to_diagnostics(run_dir, output_dir)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(candidate), str(output_dir))
        report.update({
            "state": observed_state,
            "automatic_retry_allowed": False,
            "artifact_recovery": artifact_recovery,
            "artifacts": {"html": "serp.html", "screenshot": "serp.png", "pdf": "serp.pdf"},
            "artifact_dir": output_dir.relative_to(run_dir).as_posix(),
            "reused_existing": True,
        })
        report.pop("diagnostic_dir", None)
        write_json(output_dir / "results.json", report)
        if reusable_public_capture(
            output_dir, report, task, attachment, endpoint_recovery,
        ):
            return report
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Search the mark plus every specified good on public search providers")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--provider", action="append", choices=("so360", "sogou", "bing", "baidu"), default=[])
    parser.add_argument(
        "--resume-provider", action="append", choices=("so360", "sogou", "bing", "baidu"), default=[],
        help="Explicitly clear a persisted provider circuit breaker before resuming",
    )
    parser.add_argument("--browser-executable")
    parser.add_argument("--cdp-endpoint", help="Attach public searches to the already verified dedicated Edge/Chrome")
    parser.add_argument("--browser-product", choices=("edge", "chrome"))
    parser.add_argument("--browser-user-data")
    parser.add_argument("--profile-directory")
    parser.add_argument("--artifact-lock", help="RUN-scoped cross-channel browser artifact lock")
    parser.add_argument("--goods-only", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_PUBLIC_SEARCH_LIMIT)
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_PUBLIC_SEARCH_TIMEOUT_MS)
    parser.add_argument("--min-delay-sec", type=float, default=DEFAULT_PUBLIC_SEARCH_MIN_DELAY_SEC)
    parser.add_argument("--max-delay-sec", type=float, default=DEFAULT_PUBLIC_SEARCH_MAX_DELAY_SEC)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_PUBLIC_SEARCH_BATCH_SIZE)
    parser.add_argument("--batch-pause-min-sec", type=float, default=DEFAULT_PUBLIC_SEARCH_BATCH_PAUSE_MIN_SEC)
    parser.add_argument("--batch-pause-max-sec", type=float, default=DEFAULT_PUBLIC_SEARCH_BATCH_PAUSE_MAX_SEC)
    parser.add_argument("--risk-cooldown-min-sec", type=float, default=DEFAULT_PUBLIC_SEARCH_RISK_COOLDOWN_MIN_SEC)
    parser.add_argument("--risk-cooldown-max-sec", type=float, default=DEFAULT_PUBLIC_SEARCH_RISK_COOLDOWN_MAX_SEC)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    if args.min_delay_sec < 0 or args.max_delay_sec < args.min_delay_sec:
        raise ValueError("Invalid per-query delay range")
    if args.batch_pause_min_sec < 0 or args.batch_pause_max_sec < args.batch_pause_min_sec:
        raise ValueError("Invalid batch-pause range")
    if args.risk_cooldown_min_sec < 0 or args.risk_cooldown_max_sec < args.risk_cooldown_min_sec:
        raise ValueError("Invalid public-search risk-cooldown range")
    run_dir = Path(args.run_dir).resolve()
    artifact_lock = Path(args.artifact_lock).resolve() if args.artifact_lock else None
    if artifact_lock is not None and not artifact_lock.is_relative_to(run_dir):
        raise ValueError("--artifact-lock must stay inside RUN_DIR")
    config = read_json(run_dir / "run-config.json")
    if not isinstance(config, dict):
        raise FileNotFoundError(f"run-config.json not found: {run_dir}")
    providers = list(dict.fromkeys(args.provider or PUBLIC_SEARCH_PROVIDERS))
    cdp_endpoint = validate_cdp_endpoint(args.cdp_endpoint)
    cdp_version = None
    browser_user_data = Path(args.browser_user_data).resolve() if args.browser_user_data else None
    if cdp_endpoint:
        if not args.browser_product or not browser_user_data or not args.profile_directory or not args.browser_executable:
            raise ValueError(
                "Attached public search requires --browser-product, --browser-executable, "
                "--browser-user-data and --profile-directory"
            )
        if not browser_user_data.is_dir() or browser_user_data == run_dir or browser_user_data.is_relative_to(run_dir):
            raise ValueError("Attached public-search browser user data must be an existing directory outside RUN_DIR")
        cdp_version = probe_cdp_identity(cdp_endpoint, args.browser_product)
    tasks = build_tasks(config, providers, include_base=not args.goods_only)
    browser = Path(args.browser_executable).resolve() if args.browser_executable else (
        None if cdp_endpoint else default_browser()
    )
    if browser is not None and not browser.is_file():
        raise FileNotFoundError(f"Browser executable not found: {browser}")
    browser_attachment = {
        "attached_to_existing_browser": bool(cdp_endpoint),
        "browser_product": args.browser_product,
        "browser_executable": str(browser) if browser is not None else None,
        "browser_user_data": str(browser_user_data) if browser_user_data else None,
        "profile_directory": args.profile_directory,
        "cdp_endpoint": cdp_endpoint,
        "cdp_browser": (cdp_version or {}).get("Browser"),
    }
    workflow_state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
    cdp_endpoint_recovery = workflow_state.get("cdp_endpoint_recovery")
    scripts = Path(__file__).resolve().parent
    discover = scripts / "discover-search-results.mjs"
    providers_root = run_dir / "discovery" / "providers"
    providers_root.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    runs = []
    rate_state_path = run_dir / "discovery" / "public-search-rate-limit-state.json"
    rate_state = read_json(rate_state_path, {}) or {}
    rate_state.setdefault("schema_version", "1.0")
    provider_state = rate_state.setdefault("providers", {})
    resume_providers = set(args.resume_provider)
    for provider in resume_providers:
        current = provider_state.setdefault(provider, {})
        block_kind = str(current.get("block_kind") or "")
        if block_kind == "manual_verification":
            target_id = str(current.get("verification_target_id") or "")
            target = next(
                (value for value in cdp_page_targets(cdp_endpoint) if str(value.get("id") or "") == target_id),
                None,
            )
            if target is not None and public_target_looks_protected(target):
                current.update({
                    "resume_refused_pending_verification": True,
                    "last_resume_check_at": datetime.now(timezone.utc).isoformat(),
                })
                continue
            if target is not None:
                close_cdp_target(cdp_endpoint, target_id)
        elif block_kind == "risk_restriction":
            cooldown_until = parse_utc(current.get("cooldown_until"))
            if cooldown_until and cooldown_until > datetime.now(timezone.utc):
                current.update({
                    "resume_refused_internal_cooldown": True,
                    "last_resume_check_at": datetime.now(timezone.utc).isoformat(),
                })
                continue
        current.update({
            "circuit_open": False,
            "resumed_at": datetime.now(timezone.utc).isoformat(),
            "resume_refused_pending_verification": False,
            "resume_refused_internal_cooldown": False,
            "verification_target_id": None,
            "cooldown_until": None,
        })
    write_json(rate_state_path, rate_state)

    for task_index, task in enumerate(tasks):
        current_state = provider_state.setdefault(task["provider"], {})
        if current_state.get("circuit_open") is True:
            block_kind = str(current_state.get("block_kind") or "")
            deferred_state = (
                "deferred_manual_verification" if block_kind == "manual_verification"
                else "deferred_provider_internal_cooldown" if block_kind == "risk_restriction"
                else "deferred_provider_circuit_breaker"
            )
            runs.append({
                **task, "state": deferred_state, "result_count": 0,
                "automatic_retry_allowed": False, "resume_requires_explicit_provider": True,
                "manual_verification_required": block_kind == "manual_verification",
                "internal_safety_cooldown": block_kind == "risk_restriction",
                "cooldown_until": current_state.get("cooldown_until"),
            })
            continue
        output_dir = providers_root / f'{task["query_id"]}-{task["provider"]}'
        existing = read_json(output_dir / "results.json", {}) or {}
        if not args.replace and reusable_public_capture(
            output_dir, existing, task, browser_attachment, cdp_endpoint_recovery,
        ):
            runs.append({
                **task, **existing, "diagnostic_dir": output_dir.relative_to(run_dir).as_posix(),
                "reused_existing": True, "rate_limit_wait_before_sec": 0,
            })
            continue
        if not args.replace:
            recovered = recover_latest_diagnostic_capture(
                run_dir, output_dir, task, browser_attachment, cdp_endpoint_recovery,
            )
            if recovered:
                runs.append({**task, **recovered, "rate_limit_wait_before_sec": 0})
                continue
        if existing and output_dir.exists():
            move_to_diagnostics(run_dir, output_dir)

        attempt_count = int(current_state.get("attempt_count") or 0)
        wait_seconds = 0.0
        interval_target_seconds = 0.0
        elapsed_since_request_seconds = None
        if attempt_count > 0:
            interval_target_seconds = random.uniform(args.min_delay_sec, args.max_delay_sec)
            if args.batch_size > 0 and attempt_count % args.batch_size == 0:
                interval_target_seconds += random.uniform(args.batch_pause_min_sec, args.batch_pause_max_sec)
            previous_request_at = parse_utc(
                current_state.get("last_request_at") or current_state.get("last_attempt_at")
            )
            wait_seconds, elapsed_since_request_seconds = remaining_interval_seconds(
                interval_target_seconds, previous_request_at,
            )
            if wait_seconds:
                controlled_wait(wait_seconds, task["provider"], task["query_id"])
        current_state["last_request_at"] = datetime.now(timezone.utc).isoformat()
        write_json(rate_state_path, rate_state)
        command = [
            "node", str(discover), "--query", task["query"], "--provider", task["provider"],
            "--query-id", task["query_id"], "--output-dir", str(output_dir),
            "--limit", str(max(1, min(20, args.limit))),
            "--timeout-ms", str(max(10000, args.timeout_ms)),
        ]
        if artifact_lock is not None:
            command.extend(["--artifact-lock", str(artifact_lock)])
        screenshot_backend = str(current_state.get("screenshot_backend") or "auto")
        if screenshot_backend == "cdp":
            command.extend(["--screenshot-backend", "cdp"])
        if browser is not None:
            command.extend(["--browser-executable", str(browser)])
        if cdp_endpoint:
            command.extend(["--cdp-endpoint", cdp_endpoint])
        try:
            completed = run_bounded(
                command,
                timeout=max(30, args.timeout_ms // 1000 + 20),
            )
            if completed.returncode == 124:
                report = {
                    **task, "state": "timeout", "result_count": 0,
                    "errors": [{"stage": "subprocess_timeout", "message": completed.stderr or completed.stdout}],
                    "automatic_retry_allowed": False,
                }
            else:
                report = read_json(output_dir / "results.json", {}) or {
                    "state": "error", "result_count": 0,
                    "errors": [{"stage": "missing_results_json", "message": completed.stderr or completed.stdout}],
                }
            report.update({
                "query_id": task["query_id"], "query_kind": task["query_kind"],
                "target_good": task["target_good"],
                "diagnostic_dir": output_dir.relative_to(run_dir).as_posix(),
                "subprocess_return_code": completed.returncode,
                "rate_limit_wait_before_sec": round(wait_seconds, 3),
                "rate_limit_interval_target_sec": round(interval_target_seconds, 3),
                "rate_limit_elapsed_before_wait_sec": (
                    round(elapsed_since_request_seconds, 3)
                    if elapsed_since_request_seconds is not None else None
                ),
                "reused_existing": False,
                "browser_attachment": browser_attachment,
            })
        except Exception as error:
            report = {
                **task, "state": "subprocess_error", "result_count": 0,
                "diagnostic_dir": output_dir.relative_to(run_dir).as_posix(),
                "rate_limit_wait_before_sec": round(wait_seconds, 3),
                "errors": [{"stage": "subprocess_error", "message": str(error)}],
                "automatic_retry_allowed": False,
            }
        if report.get("state") in DELIVERY_STATES and not reusable_public_capture(
            output_dir, report, task, browser_attachment, cdp_endpoint_recovery,
        ):
            report["observed_page_state"] = report.get("state")
            report["state"] = "artifact_invalid"
            report["automatic_retry_allowed"] = True
            report.setdefault("errors", []).append({
                "stage": "artifact_validation",
                "message": "required SERP HTML/image/PDF artifact is missing, corrupt, or mismatched",
            })
        current_state.update({
            "attempt_count": attempt_count + 1,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            "last_query_id": task["query_id"],
            "last_state": report.get("state"),
        })
        screenshot_capture = report.get("screenshot_capture") or {}
        if (
            screenshot_capture.get("method") == "cdp_page_capture_screenshot"
            and screenshot_capture.get("fallback_used") is True
        ):
            current_state.update({
                "screenshot_backend": "cdp",
                "screenshot_backend_reason": "playwright_failed_cdp_succeeded",
                "screenshot_backend_switched_at": datetime.now(timezone.utc).isoformat(),
            })
        if report.get("state") in BLOCKING_STATES:
            report["circuit_breaker_triggered"] = True
            report["automatic_retry_allowed"] = False
            if report.get("state") in HUMAN_VERIFICATION_STATES:
                verification_page = report.get("verification_page") or {}
                report.update({
                    "manual_verification_required": True,
                    "platform_freeze_observed": False,
                    "platform_reported_wait_seconds": None,
                })
                current_state.update({
                    "circuit_open": True,
                    "block_kind": "manual_verification",
                    "blocked_at": datetime.now(timezone.utc).isoformat(),
                    "blocked_state": report.get("state"),
                    "verification_target_id": verification_page.get("target_id"),
                    "cooldown_until": None,
                })
            else:
                cooldown_seconds = random.uniform(
                    args.risk_cooldown_min_sec, args.risk_cooldown_max_sec,
                )
                cooldown_until = (
                    datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
                ).isoformat()
                report.update({
                    "manual_verification_required": False,
                    "internal_safety_cooldown": True,
                    "cooldown_until": cooldown_until,
                    "platform_freeze_observed": False,
                    "platform_reported_wait_seconds": None,
                })
                current_state.update({
                    "circuit_open": True,
                    "block_kind": "risk_restriction",
                    "blocked_at": datetime.now(timezone.utc).isoformat(),
                    "blocked_state": report.get("state"),
                    "verification_target_id": None,
                    "cooldown_until": cooldown_until,
                })
        if report.get("state") in DELIVERY_STATES:
            artifact_dir = output_dir
            report["artifact_dir"] = artifact_dir.relative_to(run_dir).as_posix()
            report.pop("diagnostic_dir", None)
        else:
            artifact_dir = move_to_diagnostics(run_dir, output_dir)
            report["diagnostic_dir"] = artifact_dir.relative_to(run_dir).as_posix()
            report.pop("artifact_dir", None)
        write_json(artifact_dir / "results.json", report)
        write_json(rate_state_path, rate_state)
        runs.append(report)

    summary = {
        "schema_version": "1.0",
        "record_type": "public_search_goods_matrix",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
        "query_strategy": "owner_mark_registration_plus_mark_plus_each_good",
        "goods_only": bool(args.goods_only),
        "query_count": len(tasks),
        "completed_count": sum(1 for item in runs if item.get("state") in COMPLETED_STATES),
        "normal_count": sum(1 for item in runs if item.get("state") == "normal"),
        "blocked_count": sum(1 for item in runs if item.get("state") in BLOCKING_STATES),
        "manual_verification_count": sum(1 for item in runs if item.get("state") in HUMAN_VERIFICATION_STATES),
        "risk_restriction_count": sum(1 for item in runs if item.get("state") in RISK_BLOCKING_STATES),
        "deferred_count": sum(1 for item in runs if str(item.get("state") or "").startswith("deferred_")),
        "rate_limit_policy": {
            "min_delay_sec": args.min_delay_sec,
            "max_delay_sec": args.max_delay_sec,
            "batch_size": args.batch_size,
            "batch_pause_min_sec": args.batch_pause_min_sec,
            "batch_pause_max_sec": args.batch_pause_max_sec,
            "stop_provider_on_verification": True,
            "state_file": "discovery/public-search-rate-limit-state.json",
            "explicit_resume_required_after_verification": True,
            "captcha_has_no_internal_cooldown": True,
            "risk_cooldown_min_sec": args.risk_cooldown_min_sec,
            "risk_cooldown_max_sec": args.risk_cooldown_max_sec,
            "same_browser_attachment": bool(cdp_endpoint),
            "provider_round_robin": True,
            "same_domain_concurrency": MAX_SAME_DOMAIN_REQUESTS,
        },
        "browser_attachment": browser_attachment,
        "provider_runs": runs,
    }
    output = run_dir / "discovery" / "public-search-matrix.json"
    write_json(output, summary)
    print(json.dumps({
        "output": str(output), "query_count": summary["query_count"],
        "completed_count": summary["completed_count"], "normal_count": summary["normal_count"],
        "blocked_count": summary["blocked_count"], "deferred_count": summary["deferred_count"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
