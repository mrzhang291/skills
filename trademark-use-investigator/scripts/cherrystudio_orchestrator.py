#!/usr/bin/env python3
"""Single deterministic CherryStudio entry for trademark-use investigations."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
from uuid import uuid4


sys.path.insert(0, str(Path(__file__).resolve().parent))
# Every Node helper that shells back into Python must use this exact runtime.
# This prevents CherryStudio from starting with one interpreter while screenshot
# stitching silently falls back to an unrelated ``python`` on PATH.
os.environ["PYTHON_EXECUTABLE"] = sys.executable
from audit_cherrystudio_run import sha256 as file_sha256
from browser_state_guard import validate_locked_browser_state
from edge_profile import preferred_chromium_browser, running_browser_cdp_sessions
from process_utils import run_bounded, run_persistent_launcher
from partial_materials_contract import (
    PARTIAL_MANIFEST_NAME,
    PARTIAL_MANIFEST_RECORD_TYPE,
    PARTIAL_PDF_NAME,
    PARTIAL_PUBLISHED_RECORD_NAME,
    PARTIAL_PUBLISHED_RECORD_TYPE,
    PARTIAL_RECEIPT_NAME,
    PARTIAL_RECEIPT_RECORD_TYPE,
    PARTIAL_REQUEST_RECORD_NAME,
    PARTIAL_REQUEST_RECORD_TYPE,
    PARTIAL_STATUS,
    PARTIAL_PDF_PROHIBITED_DISCLOSURES,
)
from qcc_reference_guard import (
    diagnostic_qcc_navigation_candidate,
    is_exact_qcc_brand_url,
    validate_qcc_reference,
)
from run_lock import RunFileLock
from runtime_policy import (
    DEFAULT_SALES_PAGES_PER_PLATFORM,
    DEFAULT_SEARCH_PAGES_PER_PROVIDER,
    MAX_PAGES_PER_SECTION,
    MIN_PAGES_PER_SECTION,
    PUBLIC_SEARCH_PROVIDER_LABELS,
    PUBLIC_SEARCH_PROVIDERS,
    PROGRESS_STALL_GRACE_SEC,
    SALES_PLATFORM_LABELS,
    SALES_PLATFORMS,
    sales_channel_wall_timeout_seconds,
    PACKAGE_EXISTING_HARD_SEC,
    RESUME_STAGE_B_HARD_SEC,
    TERMINAL_AUDIT_HARD_SEC,
    RUNTIME_POLICY,
)


PLATFORMS = SALES_PLATFORMS
PUBLIC_PROVIDERS = PUBLIC_SEARCH_PROVIDERS
SALES_HUMAN_VERIFICATION_STATES = {"captcha", "login_required"}
SALES_RISK_COOLDOWN_STATES = {"access_denied", "rate_limited"}
CHINA_STANDARD_TIME = timezone(timedelta(hours=8), name="Asia/Shanghai")
SALES_VERIFICATION_DOMAINS = {
    "taobao": ("taobao.com", "tmall.com"),
    "jd": ("jd.com",),
    "1688": ("1688.com",),
}
LIVE_VERIFICATION_RE = re.compile(
    r"login|signin|passport|auth|verify|captcha|risk_handler|punish|security|_____tmd_____|"
    r"登录|安全验证|验证码|滑块|风控|访问受限",
    re.I,
)
QCC_BRAND_URL_IN_TEXT_RE = re.compile(
    r"https://www\.qcc\.com/brandDetail/[a-fA-F0-9]{32}\.html"
)
_ACTIVE_EXECUTION = None
EXECUTION_HEARTBEAT_INTERVAL_SECONDS = 15.0
PROGRESS_FILE_POLL_SECONDS = 1.0
PROGRESS_STATIC_REPEAT_SECONDS = 30.0
MANAGED_TASK_OUTPUT_MAX_WAIT_SECONDS = 60
MANAGED_TASK_OUTPUT_MAX_WAIT_CALLS_PER_TURN = 1
TRANSIENT_MACHINE_STATE_FIELDS = (
    "errors",
    "next_action",
    "required_user_action",
    "resume_command",
    "resume_argv",
    "resume_contract",
    "reopen_browser_command",
    "reopen_browser_argv",
    "publish_command",
    "publish_argv",
    "steps",
    "browser_attachment_validation",
    "pending_public_providers",
    "pending_public_tasks",
    "pending_sales_platforms",
    "sales_cooldown",
    "blocking_conditions",
)


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def locked_pdf_page_budgets(orchestration: dict) -> tuple[int, int]:
    """Return the PDF section budgets frozen when a CherryStudio RUN starts."""
    raw = orchestration.get("pdf_page_budgets") or {}
    try:
        sales = int(raw.get("max_sales_pages_per_platform", DEFAULT_SALES_PAGES_PER_PLATFORM))
        search = int(raw.get("max_search_pages_per_provider", DEFAULT_SEARCH_PAGES_PER_PROVIDER))
    except (TypeError, ValueError) as error:
        raise ValueError("Locked PDF page budgets are invalid") from error
    if not MIN_PAGES_PER_SECTION <= sales <= MAX_PAGES_PER_SECTION:
        raise ValueError("Locked sales-platform PDF page budget is outside the supported range")
    if not MIN_PAGES_PER_SECTION <= search <= MAX_PAGES_PER_SECTION:
        raise ValueError("Locked public-search PDF page budget is outside the supported range")
    return sales, search


def qcc_brand_url_from_request(value) -> str:
    """Return an exact QCC detail URL explicitly present in the raw request.

    CherryStudio callers are instructed to populate ``qcc_brand_url``, but the
    deterministic entry must not silently lose a URL merely because an agent
    omitted that optional structured field while preserving it in
    ``request_text``.
    """
    match = QCC_BRAND_URL_IN_TEXT_RE.search(str(value or ""))
    return match.group(0) if match else ""


def resolve_external_path(value: str | Path, field: str) -> Path:
    """Resolve CherryStudio input paths without creating a literal ``C:\\c`` tree.

    Some CherryStudio shells emit MSYS/WSL-style drive paths such as
    ``/c/Users/...`` or ``/mnt/c/Users/...``.  On native Windows ``pathlib``
    treats those as drive-root-relative and silently resolves them below the
    current drive.  Translate the two recognized spellings before ``resolve``
    and reject every other non-native root spelling.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field} path is required")
    if os.name == "nt":
        slash = raw.replace("\\", "/")
        match = re.fullmatch(r"/(?:mnt/)?([A-Za-z])(?:/(.*))?", slash)
        if match:
            raw = f"{match.group(1).upper()}:/{match.group(2) or ''}"
        elif slash.startswith("/") and not slash.startswith("//"):
            raise ValueError(
                f"{field} must be a native Windows absolute path or /<drive>/...; got {value!r}"
            )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute path: {value!r}")
    return path.resolve()


def evidence_root_for_workspace(workspace: Path) -> Path:
    """Accept either an Agent workspace or its conventional evidence root.

    CherryStudio agents sometimes pass ``<workspace>/trademark-evidence`` as
    ``workspace`` even though the lower-level initializer historically expects
    the Agent directory.  Normalizing the two spellings here prevents the
    user-visible ``trademark-evidence/trademark-evidence/RUN-*`` path.
    """
    workspace = resolve_external_path(workspace, "workspace")
    if workspace.name.casefold() == "trademark-evidence":
        return workspace
    return workspace / "trademark-evidence"


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_external_json(path: Path, default=None):
    """Read user-created UTF-8 JSON, accepting the common Windows UTF-8 BOM."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def parse_utc_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sanitized_observation_url(value: object) -> str | None:
    raw = clean(value)
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def normalized_observation(value: dict) -> dict:
    return {
        "state": clean(value.get("state")) or None,
        "page_state": clean(value.get("page_state")) or clean(value.get("state")) or None,
        "observed_at_utc": clean(value.get("observed_at_utc")) or None,
        "url": sanitized_observation_url(value.get("url")),
        "title": clean(value.get("title")) or None,
        "evidence_dir": clean(value.get("evidence_dir")) or None,
        "query_id": clean(value.get("query_id")) or None,
    }


def last_sales_automation_observation(
    run_dir: Path, platform: str, raw_state: dict,
) -> dict | None:
    embedded = raw_state.get("last_automation_observation")
    if isinstance(embedded, dict) and embedded.get("state"):
        return normalized_observation(embedded)
    query_id = clean(raw_state.get("trigger_query_id"))
    if query_id:
        diagnostic_root = (
            run_dir / "capture-diagnostics" / "sales-platform-search" / platform / query_id
        )
        for metadata_path in sorted(
            diagnostic_root.glob("page-*/metadata.json"), reverse=True,
        ):
            page = read_json(metadata_path, {}) or {}
            state = clean(page.get("state"))
            if state in {"captcha", "login_required", "access_denied", "rate_limited", "search_not_submitted"}:
                return {
                    "state": clean(raw_state.get("trigger_state")) or state,
                    "page_state": state,
                    "observed_at_utc": clean(page.get("captured_at")) or clean(raw_state.get("triggered_at_utc")) or None,
                    "url": sanitized_observation_url(page.get("url")),
                    "title": clean(page.get("title")) or None,
                    "evidence_dir": metadata_path.parent.relative_to(run_dir).as_posix(),
                    "query_id": query_id,
                }
    assisted = read_json(run_dir / "discovery" / "assisted-sales-results.json", {}) or {}
    matching_runs = [
        value for value in assisted.get("platform_runs") or []
        if isinstance(value, dict)
        and clean(value.get("platform")) == platform
        and (not query_id or clean(value.get("query_id")) == query_id)
    ]
    for run in reversed(matching_runs):
        pages = [value for value in run.get("page_runs") or [] if isinstance(value, dict)]
        for page in reversed(pages):
            state = clean(page.get("state"))
            if state in {"captcha", "login_required", "access_denied", "rate_limited", "search_not_submitted"}:
                return {
                    "state": state,
                    "page_state": state,
                    "observed_at_utc": clean(page.get("captured_at")) or clean(raw_state.get("triggered_at_utc")) or None,
                    "url": sanitized_observation_url(page.get("url")),
                    "title": clean(page.get("title")) or None,
                    "evidence_dir": clean(page.get("artifact_dir")) or None,
                    "query_id": clean(run.get("query_id")) or query_id or None,
                }
    triggered_at = clean(raw_state.get("triggered_at_utc") or raw_state.get("last_request_at"))
    if clean(raw_state.get("trigger_state")):
        return {
            "state": clean(raw_state.get("trigger_state")),
            "page_state": None,
            "observed_at_utc": triggered_at or None,
            "url": None,
            "title": None,
            "evidence_dir": clean(raw_state.get("trigger_evidence_dir")) or None,
            "query_id": query_id or None,
        }
    return None


def sales_cooldown_snapshot(
    run_dir: Path, pending_platforms: list[str], *, now: datetime | None = None,
) -> dict:
    """Describe the internal circuit breaker without implying a platform timer."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    rate_state = read_json(run_dir / "discovery" / "sales-rate-limit-state.json", {}) or {}
    platform_state = rate_state.get("platforms") if isinstance(rate_state, dict) else {}
    platform_state = platform_state if isinstance(platform_state, dict) else {}
    requested = list(dict.fromkeys(value for value in pending_platforms if value in PLATFORMS))
    for platform, raw in platform_state.items():
        if platform not in PLATFORMS or not isinstance(raw, dict):
            continue
        trigger_state = clean(raw.get("trigger_state"))
        if trigger_state in SALES_HUMAN_VERIFICATION_STATES:
            if platform not in requested:
                requested.append(platform)
            continue
        deadlines = [
            value for value in (
                parse_utc_timestamp(raw.get("cooldown_until")),
                parse_utc_timestamp(raw.get("post_batch_not_before")),
            ) if value is not None
        ]
        if deadlines and max(deadlines) > current and platform not in requested:
            requested.append(platform)
    entries = []
    for platform in requested:
        raw = platform_state.get(platform)
        raw = raw if isinstance(raw, dict) else {}
        trigger_state = clean(raw.get("trigger_state"))
        human_verification_required = trigger_state in SALES_HUMAN_VERIFICATION_STATES
        risk_until = None if human_verification_required else parse_utc_timestamp(raw.get("cooldown_until"))
        post_batch_until = None if human_verification_required else parse_utc_timestamp(raw.get("post_batch_not_before"))
        candidates = [value for value in (risk_until, post_batch_until) if value is not None]
        effective_until = max(candidates) if candidates else None
        remaining_seconds = max(
            0, math.ceil((effective_until - current).total_seconds())
        ) if effective_until else 0
        risk_active = bool(risk_until and risk_until > current)
        post_batch_active = bool(post_batch_until and post_batch_until > current)
        effective_reason = (
            "risk_circuit_breaker" if risk_active and effective_until == risk_until
            else "post_batch_rest" if post_batch_active and effective_until == post_batch_until
            else None
        )
        entries.append({
            "platform": platform,
            "platform_label": SALES_PLATFORM_LABELS.get(platform, platform),
            "internal_cooldown_active": remaining_seconds > 0,
            "human_verification_required": human_verification_required,
            "cooldown_reason": effective_reason,
            "trigger_state": trigger_state or None,
            "trigger_query_id": clean(raw.get("trigger_query_id")) or None,
            "effective_until_utc": effective_until.isoformat().replace("+00:00", "Z") if effective_until else None,
            "effective_until_local": effective_until.astimezone(CHINA_STANDARD_TIME).isoformat() if effective_until else None,
            "effective_until_local_display": effective_until.astimezone(CHINA_STANDARD_TIME).strftime("%Y-%m-%d %H:%M:%S") if effective_until else None,
            "remaining_seconds": remaining_seconds,
            "remaining_minutes_ceiling": math.ceil(remaining_seconds / 60) if remaining_seconds else 0,
            "platform_freeze_observed": False,
            "platform_reported_wait_seconds": None,
            "last_automation_observation": last_sales_automation_observation(
                run_dir, platform, raw,
            ),
        })
    active_entries = [value for value in entries if value["internal_cooldown_active"]]
    verification_entries = [value for value in entries if value["human_verification_required"]]
    return {
        "schema_version": "1.0",
        "record_type": "sales_internal_safety_cooldown",
        "policy_kind": "internal_safety_circuit_breaker",
        "not_platform_reported_cooldown": True,
        "current_visible_state": "not_observed",
        "browser_page_may_appear_normal": True,
        "manual_verification_has_no_internal_cooldown": True,
        "automatic_resume": False,
        "display_timezone": "Asia/Shanghai",
        "current_time_utc": current.isoformat().replace("+00:00", "Z"),
        "current_time_local": current.astimezone(CHINA_STANDARD_TIME).isoformat(),
        "active": bool(active_entries),
        "active_platforms": [value["platform"] for value in active_entries],
        "verification_required": bool(verification_entries),
        "verification_platforms": [value["platform"] for value in verification_entries],
        "platform_freeze_observed": False,
        "platform_account_status": "not_determined",
        "platforms": entries,
    }


def migrate_legacy_first_strike_cooldowns(
    run_dir: Path, *, now: datetime | None = None,
) -> dict:
    """Clamp legacy fixed cooldowns to the configured first risk tier once.

    This is a local state migration, never a browser probe.  It deliberately
    refuses records carrying any evidence of a second/subsequent strike.
    """
    path = run_dir / "discovery" / "sales-rate-limit-state.json"
    state = read_json(path, {}) or {}
    platforms = state.get("platforms") if isinstance(state, dict) else {}
    platforms = platforms if isinstance(platforms, dict) else {}
    ladder = RUNTIME_POLICY.get("sales_risk_circuit_breaker") or {}
    tiers = [row for row in ladder.get("tiers") or [] if isinstance(row, dict)]
    first = next((row for row in tiers if int(row.get("min_strike_count") or 0) == 1), None)
    if not first:
        return {"changed": False, "platforms": [], "reason": "first_tier_not_configured"}
    first_min = int(first.get("min_minutes") or 0)
    first_max = int(first.get("max_minutes") or first_min)
    tolerance = float(ladder.get("legacy_match_tolerance_seconds") or 5)
    rate_policy = RUNTIME_POLICY.get("sales_rate_limit") or {}
    report = read_json(run_dir / "discovery" / "assisted-sales-results.json", {}) or {}
    event_counts = {}
    for event in report.get("rate_limit_events") or []:
        if isinstance(event, dict) and clean(event.get("platform")):
            platform = clean(event.get("platform"))
            event_counts[platform] = event_counts.get(platform, 0) + 1
    changed = []
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for platform, raw in platforms.items():
        if platform not in PLATFORMS or not isinstance(raw, dict):
            continue
        if clean(raw.get("trigger_state")) not in SALES_RISK_COOLDOWN_STATES:
            continue
        try:
            strike_count = int(raw.get("strike_count") or 0)
        except (TypeError, ValueError):
            strike_count = 2
        if raw.get("legacy_cooldown_migration") or strike_count >= 2:
            continue
        if event_counts.get(platform, 0) >= 2:
            continue
        repeated_evidence_keys = (
            "strike_history", "last_risk_triggered_at", "risk_window_started_at",
            "cooldown_triggered_at", "cooldown_minutes_applied",
            "cooldown_tier_min_strike_count", "last_probe_resolution",
        )
        if any(raw.get(key) not in (None, "", [], {}) for key in repeated_evidence_keys):
            continue
        triggered = parse_utc_timestamp(raw.get("triggered_at_utc"))
        old_until = parse_utc_timestamp(raw.get("cooldown_until"))
        if triggered is None or old_until is None:
            continue
        row_policy = rate_policy.get(platform) or rate_policy.get("default") or {}
        legacy_minutes = row_policy.get("legacy_fixed_cooldown_minutes", row_policy.get("cooldown_minutes"))
        try:
            legacy_seconds = float(legacy_minutes) * 60
        except (TypeError, ValueError):
            continue
        observed_seconds = (old_until - triggered).total_seconds()
        if abs(observed_seconds - legacy_seconds) > tolerance:
            continue
        span = max(0, first_max - first_min)
        digest = hashlib.sha256(f"{platform}|{triggered.isoformat()}".encode("utf-8")).digest()
        selected_minutes = first_min + (int.from_bytes(digest[:4], "big") % (span + 1))
        new_until = triggered + timedelta(minutes=selected_minutes)
        if new_until >= old_until:
            continue
        raw.update({
            "strike_count": 1,
            "risk_window_started_at": triggered.isoformat().replace("+00:00", "Z"),
            "last_risk_triggered_at": triggered.isoformat().replace("+00:00", "Z"),
            "cooldown_triggered_at": triggered.isoformat().replace("+00:00", "Z"),
            "cooldown_until": new_until.isoformat().replace("+00:00", "Z"),
            "cooldown_minutes_applied": selected_minutes,
            "cooldown_tier_min_strike_count": 1,
            "probe_required": True,
            "probe_attempted_at": None,
            "probe_attempted_for_cooldown_until": None,
            "probe_query_id": None,
            "legacy_cooldown_migration": {
                "schema_version": "1.0",
                "migrated_at": current.isoformat().replace("+00:00", "Z"),
                "network_used": False,
                "from_cooldown_until": old_until.isoformat().replace("+00:00", "Z"),
                "to_cooldown_until": new_until.isoformat().replace("+00:00", "Z"),
                "selected_first_tier_minutes": selected_minutes,
                "deterministic": True,
            },
        })
        changed.append(platform)
    if changed:
        state["platforms"] = platforms
        state["updated_at"] = current.isoformat().replace("+00:00", "Z")
        write_json(path, state)
    return {"changed": bool(changed), "platforms": changed, "reason": "legacy_first_strike_clamp"}


def physical_sales_artifact_task_ids(run_dir: Path) -> set[str]:
    """Return task ids backed by a complete, size-checked on-disk artifact bundle."""
    run_root = run_dir.resolve()
    artifact_root = run_root / "discovery" / "assisted-platforms"
    queue = read_json(run_root / "discovery" / "manual-capture-queue.json", {}) or {}
    authoritative = {
        clean(item.get("task_id") or item.get("query_id")): item
        for item in queue.get("items") or []
        if isinstance(item, dict) and clean(item.get("task_id") or item.get("query_id"))
    }
    required_artifacts = ("rendered_dom", "search_html", "fullpage", "mhtml", "pdf", "items")
    completed_ids: set[str] = set()
    if not artifact_root.is_dir():
        return completed_ids
    for metadata_path in artifact_root.glob("*/*/page-*/metadata.json"):
        metadata = read_json(metadata_path, {}) or {}
        query_id = clean(metadata.get("query_id"))
        if metadata.get("delivery_eligible") is not True or not query_id:
            continue
        expected = authoritative.get(query_id)
        if authoritative and expected is None:
            continue
        if expected is not None and (
            (clean(expected.get("platform")) and clean(metadata.get("platform")) != clean(expected.get("platform")))
            or (clean(expected.get("query")) and clean(metadata.get("search_query")) != clean(expected.get("query")))
            or clean(metadata.get("target_good")) != clean(expected.get("target_good"))
        ):
            continue
        bundle_ok = True
        for key in required_artifacts:
            record = (metadata.get("artifacts") or {}).get(key) or {}
            minimum = 2 if key == "items" else 1000
            relative = clean(record.get("path"))
            candidate = (run_root / relative).resolve() if relative else None
            try:
                inside_run = bool(candidate and (candidate == run_root or run_root in candidate.parents))
                bundle_ok = bool(
                    inside_run and candidate.is_file()
                    and candidate.stat().st_size == int(record.get("size_bytes") or 0)
                    and candidate.stat().st_size >= minimum
                    and re.fullmatch(r"[a-fA-F0-9]{64}", clean(record.get("sha256")))
                    and file_sha256(candidate) == clean(record.get("sha256")).casefold()
                )
            except (OSError, TypeError, ValueError):
                bundle_ok = False
            if not bundle_ok:
                break
        if bundle_ok:
            completed_ids.add(query_id)
    return completed_ids


def physical_public_artifact_valid(run_dir: Path, row: dict, expected: dict | None = None) -> bool:
    """Require a query-bound, hash-verified public SERP bundle before reuse."""
    run_root = run_dir.resolve()
    relative = clean(row.get("artifact_dir"))
    artifact_dir = (run_root / relative).resolve() if relative else None
    try:
        if not artifact_dir or not artifact_dir.is_relative_to(run_root) or not artifact_dir.is_dir():
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    stored = read_json(artifact_dir / "results.json", {}) or {}
    expected = expected or row
    if not (
        clean(stored.get("provider")) == clean(expected.get("provider"))
        and clean(stored.get("query_id")) == clean(expected.get("query_id"))
        and clean(stored.get("query")) == clean(expected.get("query"))
        and clean(stored.get("state")) in {"normal", "zero_results"}
    ):
        return False
    required = {"html": "serp.html", "screenshot": "serp.png", "pdf": "serp.pdf"}
    artifacts = stored.get("artifacts") or {}
    integrity = stored.get("artifact_integrity") or {}
    if not integrity:
        migrated = {}
        try:
            for key, filename in required.items():
                path = artifact_dir / filename
                if artifacts.get(key) != filename or not path.is_file():
                    return False
                sample = path.read_bytes()[:8]
                if key == "html" and path.stat().st_size < 100:
                    return False
                if key == "screenshot" and not (
                    sample.startswith(b"\x89PNG\r\n\x1a\n") or sample.startswith(b"\xff\xd8\xff")
                ):
                    return False
                if key == "pdf" and not sample.startswith(b"%PDF-"):
                    return False
                migrated[key] = {
                    "path": filename,
                    "size_bytes": path.stat().st_size,
                    "sha256": file_sha256(path),
                }
        except OSError:
            return False
        stored["artifact_integrity"] = migrated
        stored["artifact_integrity_migrated_at"] = datetime.now(timezone.utc).isoformat()
        stored["artifact_integrity_migration_network_used"] = False
        write_json(artifact_dir / "results.json", stored)
        integrity = migrated
    for key, filename in required.items():
        path = artifact_dir / filename
        record = integrity.get(key) or {}
        try:
            if (
                artifacts.get(key) != filename
                or record.get("path") != filename
                or not path.is_file()
                or path.stat().st_size < (100 if key == "html" else 1000)
                or path.stat().st_size != int(record.get("size_bytes") or -1)
                or file_sha256(path) != clean(record.get("sha256")).casefold()
            ):
                return False
        except (OSError, TypeError, ValueError):
            return False
    return True


def _sales_pending_task_rows(run_dir: Path, pending_platforms: list[str] | None = None) -> list[dict]:
    """Return locally-known incomplete sales tasks without touching the browser.

    The manual queue is authoritative for task identity.  ``pending_platforms``
    is only a compatibility fallback for older RUNs that predate that queue.
    """
    queue = read_json(run_dir / "discovery" / "manual-capture-queue.json", {}) or {}
    # A report row is only a summary.  Reuse authority comes from the complete
    # metadata-bound physical bundle so a deleted or same-size-tampered file is
    # scheduled again instead of trapping the RUN at terminal audit.
    completed_ids = physical_sales_artifact_task_ids(run_dir)
    rows = []
    for item in queue.get("items") or []:
        if not isinstance(item, dict):
            continue
        platform = clean(item.get("platform"))
        task_id = clean(item.get("task_id") or item.get("query_id"))
        if platform not in PLATFORMS or not task_id or task_id in completed_ids:
            continue
        rows.append({
            "task_id": task_id,
            "query_id": task_id,
            "platform": platform,
            "query": clean(item.get("query")),
            "target_good": clean(item.get("target_good")) or None,
        })
    if rows or queue.get("items"):
        return rows
    return [{
        "task_id": f"legacy-pending-{platform}",
        "query_id": None,
        "platform": platform,
        "query": None,
        "target_good": None,
        "legacy_platform_only": True,
    } for platform in dict.fromkeys(pending_platforms or []) if platform in PLATFORMS]


def sales_pending_partition(
    run_dir: Path,
    pending_platforms: list[str] | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Split remaining sales tasks into runnable, cooling and verification sets."""
    tasks = _sales_pending_task_rows(run_dir, pending_platforms)
    task_platforms = list(dict.fromkeys(
        clean(item.get("platform")) for item in tasks if clean(item.get("platform")) in PLATFORMS
    ))
    cooldown = sales_cooldown_snapshot(run_dir, task_platforms, now=now) if tasks else {
        "schema_version": "1.0",
        "record_type": "sales_internal_safety_cooldown",
        "active": False,
        "active_platforms": [],
        "verification_required": False,
        "verification_platforms": [],
        "platforms": [],
        "status_network_used": False,
    }
    by_platform = {
        clean(item.get("platform")): item
        for item in cooldown.get("platforms") or []
        if isinstance(item, dict) and clean(item.get("platform"))
    }
    buckets = {"runnable_pending": [], "cooling_pending": [], "verification_pending": []}
    for task in tasks:
        platform_state = by_platform.get(clean(task.get("platform")), {})
        if platform_state.get("human_verification_required") is True:
            bucket = "verification_pending"
        elif platform_state.get("internal_cooldown_active") is True:
            bucket = "cooling_pending"
        else:
            bucket = "runnable_pending"
        buckets[bucket].append(dict(task))
    result = {
        "record_type": "sales_pending_task_partition",
        "local_state_only": True,
        "status_network_used": False,
        "pending_count": len(tasks),
        **buckets,
        "runnable_pending_count": len(buckets["runnable_pending"]),
        "cooling_pending_count": len(buckets["cooling_pending"]),
        "verification_pending_count": len(buckets["verification_pending"]),
        "runnable_platforms": list(dict.fromkeys(
            item["platform"] for item in buckets["runnable_pending"]
        )),
        "cooling_platforms": list(dict.fromkeys(
            item["platform"] for item in buckets["cooling_pending"]
        )),
        "verification_platforms": list(dict.fromkeys(
            item["platform"] for item in buckets["verification_pending"]
        )),
        "cooldown": cooldown,
    }
    result["all_remaining_deferred"] = bool(tasks) and not result["runnable_pending"]
    return result


def public_pending_partition(run_dir: Path, *, now: datetime | None = None) -> dict:
    """Classify public-search work locally so it cannot be masked by sales cooldowns."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    plan = read_json(run_dir / "discovery" / "public-search-plan.json", {}) or {}
    matrix = read_json(run_dir / "discovery" / "public-search-matrix.json", {}) or {}
    latest_by_key = {}
    for row in matrix.get("provider_runs") or []:
        if not isinstance(row, dict):
            continue
        key = (clean(row.get("provider")), clean(row.get("query_id")))
        if all(key):
            latest_by_key[key] = row
    rate = read_json(run_dir / "discovery" / "public-search-rate-limit-state.json", {}) or {}
    provider_state = rate.get("providers") if isinstance(rate, dict) else {}
    provider_state = provider_state if isinstance(provider_state, dict) else {}
    buckets = {"runnable_pending": [], "cooling_pending": [], "verification_pending": []}
    for item in plan.get("items") or []:
        if not isinstance(item, dict):
            continue
        provider = clean(item.get("provider"))
        query_id = clean(item.get("query_id"))
        if provider not in PUBLIC_PROVIDERS or not query_id:
            continue
        observed = latest_by_key.get((provider, query_id), {})
        if (
            clean(observed.get("state")) in {"normal", "zero_results"}
            and physical_public_artifact_valid(run_dir, observed, item)
        ):
            continue
        raw = provider_state.get(provider)
        raw = raw if isinstance(raw, dict) else {}
        block_kind = clean(raw.get("block_kind")) if raw.get("circuit_open") is True else ""
        until = parse_utc_timestamp(raw.get("cooldown_until"))
        task = {
            "query_id": query_id,
            "provider": provider,
            "query": clean(item.get("query")),
            "target_good": clean(item.get("target_good")) or None,
            "state": clean(observed.get("state")) or "missing_task",
        }
        observed_state = task["state"]
        if block_kind == "manual_verification" or observed_state in {"captcha", "login_required", "deferred_manual_verification"}:
            bucket = "verification_pending"
        elif block_kind == "risk_restriction" and until is not None and until > current:
            bucket = "cooling_pending"
        else:
            bucket = "runnable_pending"
        buckets[bucket].append(task)
    result = {
        "record_type": "public_search_pending_task_partition",
        "local_state_only": True,
        "status_network_used": False,
        "pending_count": sum(len(value) for value in buckets.values()),
        **buckets,
        "runnable_pending_count": len(buckets["runnable_pending"]),
        "cooling_pending_count": len(buckets["cooling_pending"]),
        "verification_pending_count": len(buckets["verification_pending"]),
        "runnable_providers": list(dict.fromkeys(
            item["provider"] for item in buckets["runnable_pending"]
        )),
        "cooling_providers": list(dict.fromkeys(
            item["provider"] for item in buckets["cooling_pending"]
        )),
        "verification_providers": list(dict.fromkeys(
            item["provider"] for item in buckets["verification_pending"]
        )),
    }
    result["all_remaining_deferred"] = bool(result["pending_count"]) and not result["runnable_pending"]
    return result


def resume_channel_decision(sales: dict, public: dict) -> dict:
    """Choose whether an explicit resume has useful bounded work to execute."""
    sales_runnable = list(sales.get("runnable_pending") or [])
    public_runnable = list(public.get("runnable_pending") or [])
    runnable_count = len(sales_runnable) + len(public_runnable)
    if runnable_count:
        phase = "sales_platform_resume_ready"
        should_execute = True
    elif public.get("verification_pending"):
        phase = "public_search_verification_required"
        should_execute = False
    elif sales.get("verification_pending"):
        phase = "sales_platform_verification_required"
        should_execute = False
    elif sales.get("cooling_pending") or public.get("cooling_pending"):
        phase = "waiting_internal_cooldown"
        should_execute = False
    else:
        phase = "sales_platform_resume_ready"
        should_execute = True
    return {
        "should_execute": should_execute,
        "phase": phase,
        "runnable_pending_count": runnable_count,
        "runnable_sales_platforms": list(sales.get("runnable_platforms") or []),
        "runnable_public_providers": list(public.get("runnable_providers") or []),
        "cooling_sales_platforms": list(sales.get("cooling_platforms") or []),
        "verification_sales_platforms": list(sales.get("verification_platforms") or []),
        "cooling_public_providers": list(public.get("cooling_providers") or []),
        "verification_public_providers": list(public.get("verification_providers") or []),
    }


def promote_manual_verification_for_explicit_resume(sales: dict, public: dict) -> tuple[dict, dict, dict]:
    """Allow one explicit resume to verify pages the operator says were handled.

    Local rate-state deliberately keeps CAPTCHA work in ``verification_pending``
    until the browser worker sees the preserved page again.  Returning before
    launching that worker creates a permanent loop: the worker that can clear the
    state never runs.  An explicit resume is the operator's bounded permission to
    perform that check.  Cooling work is never promoted.
    """
    promoted_sales = dict(sales)
    promoted_public = dict(public)
    sales_rows = [dict(value) for value in sales.get("verification_pending") or []]
    public_rows = [dict(value) for value in public.get("verification_pending") or []]
    if sales_rows:
        promoted_sales["runnable_pending"] = [
            *(sales.get("runnable_pending") or []), *sales_rows,
        ]
        promoted_sales["verification_pending"] = []
        promoted_sales["runnable_pending_count"] = len(promoted_sales["runnable_pending"])
        promoted_sales["verification_pending_count"] = 0
        promoted_sales["runnable_platforms"] = list(dict.fromkeys([
            *(sales.get("runnable_platforms") or []),
            *(value.get("platform") for value in sales_rows if value.get("platform")),
        ]))
        promoted_sales["verification_platforms"] = []
        promoted_sales["all_remaining_deferred"] = bool(
            promoted_sales.get("cooling_pending") and not promoted_sales.get("runnable_pending")
        )
    if public_rows:
        promoted_public["runnable_pending"] = [
            *(public.get("runnable_pending") or []), *public_rows,
        ]
        promoted_public["verification_pending"] = []
        promoted_public["runnable_pending_count"] = len(promoted_public["runnable_pending"])
        promoted_public["verification_pending_count"] = 0
        promoted_public["runnable_providers"] = list(dict.fromkeys([
            *(public.get("runnable_providers") or []),
            *(value.get("provider") for value in public_rows if value.get("provider")),
        ]))
        promoted_public["verification_providers"] = []
        promoted_public["all_remaining_deferred"] = bool(
            promoted_public.get("cooling_pending") and not promoted_public.get("runnable_pending")
        )
    probe = {
        "schema_version": "1.0",
        "record_type": "explicit_resume_verification_probe",
        "sales_platforms": list(dict.fromkeys(
            value.get("platform") for value in sales_rows if value.get("platform")
        )),
        "public_providers": list(dict.fromkeys(
            value.get("provider") for value in public_rows if value.get("provider")
        )),
        "cooling_work_promoted": False,
        "network_scope": "first_pending_task_per_promoted_channel_then_normal_checkpointing",
    }
    return promoted_sales, promoted_public, probe


def sales_resume_cli_args(sales: dict) -> list[str]:
    """Render the exact runner arguments for a local sales-task partition."""
    runnable = list(sales.get("runnable_platforms") or [])
    deferred = list(dict.fromkeys([
        *(sales.get("cooling_platforms") or []),
        *(sales.get("verification_platforms") or []),
    ]))
    args = [*sum((["--platform", value] for value in runnable), [])]
    args.extend(sum((["--deferred-platform", value] for value in deferred), []))
    if not runnable:
        args.append("--skip-sales-search")
    return args


def public_search_cooldown_snapshot(run_dir: Path, *, now: datetime | None = None) -> dict:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = read_json(run_dir / "discovery" / "public-search-rate-limit-state.json", {}) or {}
    entries = []
    for provider, raw in (state.get("providers") or {}).items():
        if provider not in PUBLIC_PROVIDERS or not isinstance(raw, dict):
            continue
        if raw.get("circuit_open") is not True or clean(raw.get("block_kind")) != "risk_restriction":
            continue
        until = parse_utc_timestamp(raw.get("cooldown_until"))
        remaining = max(0, math.ceil((until - current).total_seconds())) if until else 0
        entries.append({
            "provider": provider,
            "provider_label": PUBLIC_SEARCH_PROVIDER_LABELS.get(provider, provider),
            "effective_until_utc": until.isoformat().replace("+00:00", "Z") if until else None,
            "effective_until_local": until.astimezone(CHINA_STANDARD_TIME).isoformat() if until else None,
            "remaining_seconds": remaining,
            "active": remaining > 0,
            "platform_freeze_observed": False,
            "platform_reported_wait_seconds": None,
        })
    return {
        "schema_version": "1.0",
        "record_type": "public_search_internal_safety_cooldown",
        "not_platform_reported_cooldown": True,
        "active": any(value["active"] for value in entries),
        "active_providers": [value["provider"] for value in entries if value["active"]],
        "platform_freeze_observed": False,
        "platform_account_status": "not_determined",
        "providers": entries,
    }


def public_search_cooldown_guidance(snapshot: dict) -> str:
    active = [value for value in snapshot.get("providers") or [] if value.get("active")]
    if not active:
        return "内部安全计时已结束；现在可原样执行resume_command，只恢复未完成的公开搜索任务。"
    details = []
    for value in active:
        remaining = max(0, int(value.get("remaining_seconds") or 0))
        minutes, seconds = divmod(remaining, 60)
        remaining_label = f"约{minutes}分{seconds}秒"
        provider = clean(value.get("provider"))
        details.append(
            f"{PUBLIC_SEARCH_PROVIDER_LABELS.get(provider, provider)}剩余{remaining_label}"
        )
    return (
        f"公开搜索处于本工具内部安全计时（{'；'.join(details)}）；"
        "这不是验证码等待，也不是平台宣告账号被冻结。计时结束后原样执行resume_command，"
        "已完成渠道不会重跑。"
    )


def sales_cooldown_guidance(snapshot: dict, browser_label: str, pending_platforms: list[str]) -> tuple[str, str]:
    entries = snapshot.get("platforms") if isinstance(snapshot, dict) else []
    active = [value for value in entries or [] if value.get("internal_cooldown_active")]
    verification = [value for value in entries or [] if value.get("human_verification_required")]
    if verification:
        verification_labels = "、".join(str(value.get("platform_label") or value.get("platform")) for value in verification)
        action = (
            f"专用{browser_label}中已保留{verification_labels}的验证码或登录确认页，请人工完成并保持窗口打开。"
            "验证码不代表账号被冻结，平台没有提供等待倒计时，本工具也不为验证码设置冷却。"
        )
        if active:
            deadlines = "；".join(
                f'{value.get("platform_label")}至{value.get("effective_until_local_display")}（北京时间）'
                for value in active
            )
            action += f"另有真实访问限制触发本工具内部安全保护：{deadlines}；这也不是平台冻结声明。"
            return (
                action,
                "验证码处理完成后可立即执行resume_command；已验证平台会恢复，仍在内部保护的平台继续跳过，系统不会自动发起查询",
            )
        return (
            action,
            "验证码处理完成后可立即执行resume_command，只恢复未完成任务；系统不会自动发起查询",
        )
    if active:
        deadlines = "；".join(
            f'{value.get("platform_label")}至{value.get("effective_until_local_display")}（北京时间）'
            for value in active
        )
        return (
            f"此前自动任务捕获到访问限制页，已触发本工具内部安全保护：{deadlines}。"
            f"状态刷新未读取当前{browser_label}页面（current_visible_state=not_observed）；"
            "该计时器不是平台冻结或平台倒计时；保持窗口打开",
            "内部保护到期后由用户明确执行resume_command；系统不会自动恢复",
        )
    labels = "、".join(SALES_PLATFORM_LABELS.get(value, value) for value in pending_platforms) or "销售平台"
    return (
        f"当前没有仍生效的内部安全冷却；状态刷新未读取当前页面，"
        f"请确认专用{browser_label}中的{labels}页面正常并保持窗口打开",
        "用户明确执行resume_command后只恢复未完成任务；系统不会自动恢复",
    )


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def process_is_alive(pid) -> bool:
    """Return whether a PID is currently alive without signalling it on Windows."""
    try:
        numeric_pid = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric_pid <= 0:
        return False
    if numeric_pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, numeric_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                and exit_code.value == 259  # STILL_ACTIVE
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(numeric_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    proc_stat = Path(f"/proc/{numeric_pid}/stat")
    if proc_stat.is_file():
        try:
            fields = proc_stat.read_text(encoding="utf-8", errors="replace").split()
            if len(fields) > 2 and fields[2] == "Z":
                return False
        except OSError:
            pass
    return True


def reconcile_execution_liveness(
    run_dir: Path, execution: dict, *, now_utc: datetime | None = None,
) -> tuple[dict, dict]:
    """Require both a fresh heartbeat and a live owner PID before reporting running."""
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    observed = dict(execution) if isinstance(execution, dict) else {}
    heartbeat_at = parse_utc_timestamp(observed.get("heartbeat_at"))
    heartbeat_age_seconds = (
        max(0, math.ceil((current - heartbeat_at).total_seconds()))
        if heartbeat_at else None
    )
    heartbeat_fresh = bool(
        clean(observed.get("state")) == "running"
        and heartbeat_age_seconds is not None
        and heartbeat_age_seconds <= 45
    )
    owner_pid = observed.get("orchestrator_pid")
    pid_alive = bool(
        clean(observed.get("state")) == "running" and process_is_alive(owner_pid)
    )
    interrupted_persisted = False
    if clean(observed.get("state")) == "running" and not pid_alive:
        observed.update({
            "state": "interrupted",
            "interrupted_at": current.isoformat(),
            "termination_observed_at": current.isoformat(),
            "termination_reason": (
                "orchestrator_pid_not_alive" if owner_pid is not None
                else "orchestrator_pid_missing"
            ),
        })
        write_json(run_dir / "cherrystudio-execution-state.json", observed)
        interrupted_persisted = True
        heartbeat_fresh = False
    liveness = {
        "schema_version": "1.0",
        "record_type": "cherrystudio_execution_liveness",
        "execution_id": observed.get("execution_id"),
        "orchestrator_pid": owner_pid,
        "execution_state": clean(observed.get("state")) or None,
        "pid_alive": pid_alive,
        "heartbeat_at": observed.get("heartbeat_at"),
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "heartbeat_fresh": heartbeat_fresh,
        "considered_running": bool(heartbeat_fresh and pid_alive),
        "interrupted_persisted": interrupted_persisted,
        "observed_at": current.isoformat(),
    }
    return observed, liveness


class ExecutionHeartbeat:
    """Persist liveness and stream compact, local-only progress during long steps."""

    def __init__(
        self,
        run_dir: Path | None,
        *,
        action: str = "resume",
        emit_progress: bool = False,
        output_stream=None,
        heartbeat_interval_seconds: float = EXECUTION_HEARTBEAT_INTERVAL_SECONDS,
        progress_poll_seconds: float = PROGRESS_FILE_POLL_SECONDS,
        progress_repeat_seconds: float = PROGRESS_STATIC_REPEAT_SECONDS,
    ):
        self.run_dir = run_dir.resolve() if run_dir is not None else None
        self.path = (
            self.run_dir / "cherrystudio-execution-state.json"
            if self.run_dir is not None else None
        )
        self.action = clean(action) or "unknown"
        self.emit_progress = bool(emit_progress)
        self.output_stream = output_stream if output_stream is not None else sys.stderr
        self.heartbeat_interval_seconds = max(1.0, float(heartbeat_interval_seconds))
        self.progress_poll_seconds = max(0.1, float(progress_poll_seconds))
        self.progress_repeat_seconds = max(1.0, float(progress_repeat_seconds))
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.progress_lock = threading.Lock()
        self.last_progress_signature = None
        self.last_progress_emitted_monotonic = None
        self.progress_output_failed = False
        self.last_heartbeat_write_monotonic = 0.0
        now = datetime.now(timezone.utc).isoformat()
        self.payload = {
            "schema_version": "1.0",
            "record_type": "cherrystudio_execution_state",
            "execution_id": uuid4().hex,
            "orchestrator_pid": os.getpid(),
            "action": self.action,
            "state": "running",
            "current_step": f"{self.action}_starting",
            "started_at": now,
            "heartbeat_at": now,
        }
        self.thread = threading.Thread(target=self._run, name="trademark-resume-heartbeat", daemon=True)

    def attach_run_dir(self, run_dir: Path) -> None:
        """Attach a newly-created start RUN without restarting the progress stream."""
        resolved = run_dir.resolve()
        with self.lock:
            if self.run_dir is not None and self.run_dir != resolved:
                raise ValueError("execution heartbeat cannot switch RUN directories")
            self.run_dir = resolved
            self.path = resolved / "cherrystudio-execution-state.json"
        self._write()

    def _progress_event(self, snapshot: dict) -> dict:
        workflow = {}
        live = {}
        if self.run_dir is not None:
            workflow = read_json(
                self.run_dir / "discovery" / "sales-workflow-state.json", {},
            ) or {}
            live = read_json(
                self.run_dir / "discovery" / "sales-live-progress.json", {},
            ) or {}
        execution_started_at = parse_utc_timestamp(snapshot.get("started_at"))
        live_updated_at = parse_utc_timestamp(live.get("updated_at"))
        live_belongs_to_execution = bool(
            live_updated_at
            and (
                execution_started_at is None
                or live_updated_at >= execution_started_at - timedelta(seconds=2)
            )
        )
        active_task = (
            live.get("active_task")
            if live_belongs_to_execution and isinstance(live.get("active_task"), dict)
            else None
        )
        live_stage = clean(live.get("stage")) if live_belongs_to_execution else ""
        current_step = clean(snapshot.get("current_step")) or "unknown"
        workflow_phase = clean(workflow.get("phase")) or None

        def optional_int(value):
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return {
            "schema_version": "1.0",
            "record_type": "cherrystudio_progress",
            "monitoring_contract": {
                "managed_task_output_contract": managed_task_output_monitoring_contract(),
            },
            "managed_task_output_contract": managed_task_output_monitoring_contract(),
            "action": self.action,
            "execution_id": snapshot.get("execution_id"),
            "orchestrator_pid": snapshot.get("orchestrator_pid"),
            "run_dir": str(self.run_dir) if self.run_dir is not None else None,
            "state": clean(snapshot.get("state")) or "unknown",
            "phase": live_stage or workflow_phase or current_step,
            "current_step": current_step,
            "workflow_phase": workflow_phase,
            "task": ({
                "platform": clean(active_task.get("platform")) or None,
                "query_id": clean(active_task.get("query_id")) or None,
                "query": clean(active_task.get("query")) or None,
                "target_good": clean(active_task.get("target_good")) or None,
            } if active_task else None),
            "processed": optional_int(live.get("processed_task_count")),
            "accepted": optional_int(live.get("completed_task_count")),
            "planned": optional_int(live.get("planned_task_count")),
            "deadline_at": (
                clean(live.get("stage_deadline_at")) or None
                if live_belongs_to_execution else None
            ),
            "heartbeat_at": snapshot.get("heartbeat_at"),
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _progress_signature(event: dict) -> tuple:
        task = event.get("task") if isinstance(event.get("task"), dict) else {}
        return (
            event.get("action"), event.get("run_dir"), event.get("state"),
            event.get("phase"), event.get("current_step"), event.get("workflow_phase"),
            task.get("platform"), task.get("query_id"), task.get("query"),
            event.get("processed"), event.get("accepted"), event.get("planned"),
            event.get("deadline_at"),
        )

    def _emit_progress(self, *, force: bool = False, now_monotonic: float | None = None) -> None:
        if not self.emit_progress or self.progress_output_failed:
            return
        with self.lock:
            snapshot = dict(self.payload)
        event = self._progress_event(snapshot)
        signature = self._progress_signature(event)
        current_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        with self.progress_lock:
            repeat_due = bool(
                self.last_progress_emitted_monotonic is not None
                and current_monotonic - self.last_progress_emitted_monotonic
                >= self.progress_repeat_seconds
            )
            if not force and signature == self.last_progress_signature and not repeat_due:
                return
            try:
                self.output_stream.write(
                    json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                self.output_stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                # Progress visibility must never become a new workflow failure.
                self.progress_output_failed = True
                return
            self.last_progress_signature = signature
            self.last_progress_emitted_monotonic = current_monotonic

    def _write(self) -> None:
        with self.lock:
            self.payload["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
            snapshot = dict(self.payload)
        # Never hold the state lock across filesystem I/O.  A stalled disk write
        # must not make resume teardown wait forever on this lock.
        if self.path is not None:
            write_json(self.path, snapshot)
        self.last_heartbeat_write_monotonic = time.monotonic()
        self._emit_progress()
        if self.stop_event.is_set() and snapshot.get("state") == "running":
            with self.lock:
                latest = dict(self.payload)
            if latest.get("state") != "running" and self.path is not None:
                write_json(self.path, latest)

    def _run(self) -> None:
        wait_seconds = self.progress_poll_seconds if self.emit_progress else self.heartbeat_interval_seconds
        while not self.stop_event.wait(wait_seconds):
            if self.emit_progress:
                self._emit_progress()
            if time.monotonic() - self.last_heartbeat_write_monotonic >= self.heartbeat_interval_seconds:
                self._write()

    def set_step(self, step: str) -> None:
        with self.lock:
            self.payload["current_step"] = clean(step) or "unknown"
        self._write()

    def __enter__(self):
        self._write()
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop_event.set()
        self.thread.join(timeout=2)
        with self.lock:
            self.payload.update({
                "state": "failed" if exc_type else "finished",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_type": exc_type.__name__ if exc_type else None,
            })
            self.payload["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
            snapshot = dict(self.payload)
        if self.path is not None:
            write_json(self.path, snapshot)
        self._emit_progress(force=True)


def run_step(command: list[str], timeout: int = 180, *, persistent_launcher: bool = False) -> dict:
    if _ACTIVE_EXECUTION is not None:
        _ACTIVE_EXECUTION.set_step(Path(command[1]).name if len(command) > 1 else str(command[0]))
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    completed = (
        run_persistent_launcher(command, timeout=timeout)
        if persistent_launcher else run_bounded(command, timeout=timeout)
    )
    finished_at = datetime.now(timezone.utc)
    return {
        "command_script": Path(command[1]).name if len(command) > 1 else command[0],
        "command_argv": [str(value) for value in command],
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(time.monotonic() - started_monotonic, 3),
        "timeout_seconds": timeout,
        "return_code": completed.returncode,
        # Preflight JSON grows as required production files are added. Keep a
        # bounded but large enough tail to retain the skill path and the final
        # dependency verdict without silently corrupting non-ASCII paths.
        "stdout_tail": (completed.stdout or "")[-6000:],
        "stderr_tail": (completed.stderr or "")[-6000:],
        **({"error": "timeout"} if completed.returncode == 124 else {}),
    }


def run_terminal_audit_step(run_dir: Path, steps: list[dict] | None = None) -> dict:
    """Run the expensive physical audit in a killable child process."""
    audit_path = run_dir / "cherrystudio-terminal-audit.json"
    previous_mtime = audit_path.stat().st_mtime_ns if audit_path.is_file() else None
    step = run_step([
        sys.executable, str(Path(__file__).resolve().parent / "audit_cherrystudio_run.py"),
        "--run-dir", str(run_dir),
    ], timeout=TERMINAL_AUDIT_HARD_SEC)
    if steps is not None:
        steps.append(step)
    fresh = bool(
        audit_path.is_file()
        and (previous_mtime is None or audit_path.stat().st_mtime_ns != previous_mtime)
    )
    audit = read_json(audit_path, {}) if fresh else {}
    if (
        isinstance(audit, dict)
        and audit.get("record_type") == "cherrystudio_terminal_audit"
        and clean(audit.get("run_dir")) == str(run_dir.resolve())
        and step.get("return_code") in {0, 3}
    ):
        return audit
    reason = (
        "terminal_audit_timeout" if step.get("return_code") == 124
        else "terminal_audit_process_failed"
    )
    return {
        "schema_version": "1.0",
        "record_type": "cherrystudio_terminal_audit",
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "run_id": (read_json(run_dir / "run-config.json", {}) or {}).get("run_id"),
        "run_dir": str(run_dir.resolve()),
        "workflow_mode": "free_assisted_browser",
        "status": "incomplete",
        "validation": {"ok": False, "errors": [reason]},
        "checks": {"audit_subprocess": step},
    }


def process_id_alive(pid: int) -> bool:
    """Backward-compatible doctor alias for the shared safe PID probe."""
    return process_is_alive(pid)


def run_cherrystudio_doctor(run_root: Path) -> tuple[dict, int]:
    """Offline production acceptance probe; never opens a website or browser."""
    started = time.monotonic()
    run_root = run_root.resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    scripts = Path(__file__).resolve().parent
    checks: dict[str, dict] = {}
    preflight = run_bounded([
        sys.executable, str(scripts / "preflight.py"),
        "--run-root", str(run_root), "--profile", "assisted",
    ], timeout=120)
    try:
        preflight_payload = json.loads(preflight.stdout or "{}")
    except json.JSONDecodeError:
        preflight_payload = {}
    checks["assisted_preflight"] = {
        "ok": preflight.returncode == 0 and preflight_payload.get("ok") is True,
        "return_code": preflight.returncode,
        "selected_browser": preflight_payload.get("selected_browser"),
        "blocking": preflight_payload.get("blocking") or [],
    }

    node_checks = []
    for filename in (
        "atomic-text-write.mjs",
        "sales-platform-assisted-discover.mjs",
        "sales-request-pacing.mjs",
        "discover-search-results.mjs",
    ):
        result = run_step(["node", "--check", str(scripts / filename)], timeout=15)
        node_checks.append({"file": filename, "ok": result.get("return_code") == 0})
    checks["node_syntax"] = {"ok": all(value["ok"] for value in node_checks), "files": node_checks}

    child_pid = None
    with tempfile.TemporaryDirectory(prefix="trademark-doctor-") as temp:
        marker = Path(temp) / "child-pid.txt"
        probe_source = (
            "import pathlib,subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            f"pathlib.Path({str(marker)!r}).write_text(str(p.pid),encoding='utf-8');"
            "time.sleep(60)"
        )
        probe_started = time.monotonic()
        probe = run_bounded([sys.executable, "-c", probe_source], timeout=1.0)
        probe_elapsed = time.monotonic() - probe_started
        try:
            child_pid = int(marker.read_text(encoding="utf-8").strip())
        except (OSError, TypeError, ValueError):
            child_pid = None
        time.sleep(0.2)
        child_alive = process_id_alive(child_pid or 0)
    checks["process_tree_timeout"] = {
        "ok": probe.returncode == 124 and probe_elapsed < 6 and not child_alive,
        "return_code": probe.returncode,
        "elapsed_seconds": round(probe_elapsed, 3),
        "descendant_pid": child_pid,
        "descendant_alive_after_timeout": child_alive,
    }

    budget = RUNTIME_POLICY.get("workflow_time_budget") or {}
    checks["time_budget"] = {
        "ok": (
            int(budget.get("resume_stage_b_hard_sec") or 0) == RESUME_STAGE_B_HARD_SEC
            and int(budget.get("terminal_audit_hard_sec") or 0) == TERMINAL_AUDIT_HARD_SEC
            and RESUME_STAGE_B_HARD_SEC <= 75 * 60
        ),
        **budget,
        "sales_timeout_11_tasks_sec": sales_channel_wall_timeout_seconds(11),
        "sales_timeout_33_tasks_sec": sales_channel_wall_timeout_seconds(33),
        "manual_login_time_excluded": True,
    }
    ok = all(value.get("ok") is True for value in checks.values())
    result = {
        "schema_version": "1.0",
        "record_type": "cherrystudio_offline_doctor",
        "status": "ready" if ok else "not_ready",
        "ok": ok,
        "network_request_performed": False,
        "browser_launched": False,
        "run_root": str(run_root),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "checks": checks,
    }
    return result, 0 if ok else 3


def parse_goods(intake: dict) -> list[str]:
    goods = intake.get("goods") or intake.get("goods_services") or []
    if isinstance(goods, str):
        goods = re.split(r"[;；]", goods)
    if not isinstance(goods, list):
        raise ValueError("intake goods must be a JSON array or a semicolon-separated string")
    result = [clean(value) for value in goods if clean(value)]
    return list(dict.fromkeys(result))


def normalized_goods_token(value) -> str:
    """Normalize a goods label only for intake/request consistency checks."""
    token = clean(value).replace("（", "(").replace("）", ")")
    token = re.sub(r"\s+", "", token)
    return re.sub(r"\(一\)$", "", token)


def enumerated_goods_from_request(value) -> list[str]:
    """Extract an explicit semicolon-delimited goods list from user prose.

    The guard intentionally activates only for an unmistakable enumeration,
    preferring quoted spans.  Free prose such as "指定商品" remains untouched.
    """
    request = str(value or "").replace("\r", " ").replace("\n", " ")
    spans = re.findall(r"[“\"]([^”\"]*[；;][^”\"]*)[”\"]", request)
    if not spans:
        spans = re.findall(r"在(.{1,600}?[；;].{1,600}?)这些商品", request)
    candidates = []
    for span in spans:
        items = [clean(item).strip("，,。；;：:‘’'\"") for item in re.split(r"[；;]", span)]
        items = [item for item in items if item]
        if len(items) >= 2:
            candidates.append(items)
    return max(candidates, key=len) if candidates else []


def validate_goods_against_request(intake: dict, goods: list[str]) -> None:
    """Reject a likely structured-extraction typo before creating a RUN.

    CherryStudio may derive ``goods`` from the user's prose.  When most of the
    structured labels occur verbatim in that prose, one missing label is much
    more likely to be an extraction typo than an intentional extra scope item.
    If the prose does not enumerate most goods (for example, it only says
    "指定商品"), this guard deliberately stays silent.
    """
    request = clean(intake.get("request_text") or intake.get("user_request"))
    if not request or len(goods) < 2:
        return
    enumerated = enumerated_goods_from_request(request)
    if enumerated:
        structured_tokens = {normalized_goods_token(value) for value in goods}
        omitted = [
            value for value in enumerated
            if normalized_goods_token(value) not in structured_tokens
        ]
        if omitted:
            raise ValueError(
                "structured goods omitted items explicitly enumerated in request_text: "
                f"{omitted!r}; structured={len(goods)}, enumerated={len(enumerated)}"
            )
    request_token = normalized_goods_token(request)
    normalized = [(good, normalized_goods_token(good)) for good in goods]
    matched = [good for good, token in normalized if token and token in request_token]
    missing = [good for good, token in normalized if token and token not in request_token]
    threshold = max(2, (len(normalized) * 3 + 4) // 5)  # ceil(60%)
    if missing and len(matched) >= threshold:
        raise ValueError(
            "structured goods conflict with request_text; likely extraction typo: "
            f"missing from request_text={missing!r}; matched={len(matched)}/{len(normalized)}"
        )


def explicit_profile(intake: dict) -> str | None:
    request = clean(intake.get("request_text") or intake.get("user_request"))
    profile = clean(intake.get("profile")).casefold()
    explicit_flag = intake.get("profile_explicit") is True or intake.get("explicit_mode") is True
    quick_in_request = bool(re.search(r"(?:^|[\s，。；：])(?:quick|快速模式|快速筛查)(?:$|[\s，。；：])", request, re.I))
    quick_negated = bool(re.search(r"(?:不要|不使用|别用|禁止使用?)\s*(?:quick|快速模式|快速筛查)", request, re.I))
    forensic_in_request = bool(re.search(r"forensic|法证级|正式取证|完整调查|全量调查", request, re.I))
    forensic_negated = bool(re.search(r"(?:不要|不使用|别用|禁止使用?)\s*(?:forensic|法证级|正式取证|完整调查|全量调查)", request, re.I))
    quick_in_request = quick_in_request and not quick_negated
    forensic_in_request = forensic_in_request and not forensic_negated
    if profile == "quick" and (explicit_flag or quick_in_request):
        return "quick"
    if profile == "forensic" and (explicit_flag or forensic_in_request):
        return "forensic"
    if quick_in_request:
        return "quick"
    if forensic_in_request:
        return "forensic"
    return None


def select_workflow(intake: dict, goods: list[str]) -> str:
    explicit = explicit_profile(intake)
    if explicit:
        return explicit
    request = clean(intake.get("request_text") or intake.get("user_request"))
    actual_use = bool(re.search(r"实际使用|撤三|使用线索|有没有.*使用", request)) or clean(intake.get("intent")) == "actual_use"
    no_cache = intake.get("no_cache") is True or bool(re.search(r"不要用?缓存|不用缓存|重新查|自己找", request))
    # CherryStudio sometimes omits request_text after extracting the structured
    # fields.  A complete owner/registration/goods intake must still fail safe
    # into the free assisted-browser matrix, never the seven-query quick route.
    structured_actual_use = bool(goods and clean(intake.get("owner")) and clean(intake.get("registration_number")))
    if structured_actual_use:
        return "free_assisted_browser"
    if goods and actual_use:
        return "free_assisted_browser"
    return "quick"


def no_cache_requested(intake: dict) -> bool:
    request = clean(intake.get("request_text") or intake.get("user_request"))
    return intake.get("no_cache") is True or bool(re.search(r"不要用?缓存|不用缓存|重新查|自己找", request))


def china_calendar_date(now: datetime | None = None) -> date:
    """Return the investigation calendar date in the user's local jurisdiction."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(CHINA_STANDARD_TIME).date()


def normalize_period(intake: dict, workflow: str) -> tuple[str | None, str | None]:
    raw = clean(intake.get("period"))
    if raw:
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})", raw)
        if not match:
            raise ValueError("period must use YYYY-MM-DD/YYYY-MM-DD")
        start_date, end_date = (date.fromisoformat(value) for value in match.groups())
        if start_date >= end_date:
            raise ValueError("period start must be earlier than period end")
        return raw, "provided"
    request = clean(intake.get("request_text") or intake.get("user_request"))
    if workflow == "free_assisted_browser" and re.search(r"近三年|最近三年|三年内", request):
        end_date = china_calendar_date()
        try:
            start_date = end_date.replace(year=end_date.year - 3)
        except ValueError:  # February 29
            start_date = end_date.replace(year=end_date.year - 3, day=28)
        return f"{start_date.isoformat()}/{end_date.isoformat()}", "derived_three_year_lookback_asia_shanghai"
    if workflow == "free_assisted_browser":
        raise ValueError("free assisted-browser investigation requires an explicit period or a near-three-years request")
    return None, None


def new_run_id(intake: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    registration = re.sub(r"[^A-Za-z0-9_-]", "", clean(intake.get("registration_number"))) or "unregistered"
    return f"RUN-{now}-{registration}-fresh"


def build_public_plan(run_dir: Path, config: dict) -> dict:
    trademark = config.get("trademark") or {}
    mark = clean(trademark.get("name"))
    owner = clean(trademark.get("owner"))
    registration = clean(trademark.get("registration_number"))
    goods = [clean(value) for value in trademark.get("goods_services") or [] if clean(value)]
    tasks = []
    for provider in PUBLIC_PROVIDERS:
        prefix = "SO360" if provider == "so360" else provider.upper()
        tasks.append({
            "query_id": f"PUBLIC-{prefix}-BASE", "provider": provider,
            "query_kind": "owner_mark_registration", "target_good": None,
            "query": " ".join(value for value in (owner, mark, registration) if value),
        })
        for index, good in enumerate(goods, start=1):
            tasks.append({
                "query_id": f"PUBLIC-{prefix}-G{index:03d}", "provider": provider,
                "query_kind": "mark_plus_good", "target_good": good,
                "query": f"{mark} {good}",
            })
    plan = {
        "schema_version": "1.0",
        "record_type": "public_search_task_plan",
        "run_id": config.get("run_id"),
        "network_used": False,
        "cache_policy": "new_run_no_reuse",
        "providers": list(PUBLIC_PROVIDERS),
        "task_count": len(tasks),
        "items": tasks,
    }
    write_json(run_dir / "discovery" / "public-search-plan.json", plan)
    return plan


def workflow_progress(run_dir: Path) -> dict:
    """Return factual coverage counts so narrative layers cannot infer completion."""
    run_root = run_dir.resolve()
    public_plan = read_json(run_dir / "discovery" / "public-search-plan.json", {}) or {}
    public_matrix = read_json(run_dir / "discovery" / "public-search-matrix.json", {}) or {}
    public_runs = [value for value in public_matrix.get("provider_runs") or [] if isinstance(value, dict)]
    public_expected = int(public_plan.get("task_count") or len(public_plan.get("items") or []) or 0)
    public_plan_by_key = {
        (clean(value.get("provider")), clean(value.get("query_id"))): value
        for value in public_plan.get("items") or [] if isinstance(value, dict)
    }
    public_latest_by_key = {}
    for value in public_runs:
        key = (clean(value.get("provider")), clean(value.get("query_id")))
        if key in public_plan_by_key:
            public_latest_by_key[key] = value
    public_runs = list(public_latest_by_key.values())
    public_physical = {
        id(value): physical_public_artifact_valid(
            run_dir, value, public_plan_by_key[(clean(value.get("provider")), clean(value.get("query_id")))],
        )
        for value in public_runs
        if clean(value.get("state")) in {"normal", "zero_results"}
    }
    public_completed = sum(
        1 for value in public_runs
        if clean(value.get("state")) in {"normal", "zero_results"}
        and public_physical.get(id(value)) is True
    )
    public_blocked = sum(
        1 for value in public_runs
        if clean(value.get("state")) in {"captcha", "access_denied", "login_required", "deferred_provider_circuit_breaker"}
    )
    public_retryable = sum(
        1 for value in public_runs
        if (
            clean(value.get("state")) == "artifact_invalid"
            or value.get("automatic_retry_allowed") is True
            or (
                clean(value.get("state")) in {"normal", "zero_results"}
                and public_physical.get(id(value)) is not True
            )
        )
    )
    if not public_runs:
        public_status = "not_started"
    elif public_expected and public_completed == public_expected:
        public_status = "complete"
    elif public_blocked:
        public_status = "verification_required"
    elif public_retryable:
        public_status = "capture_retry_required"
    else:
        public_status = "in_progress_or_incomplete"

    sales_plan = read_json(run_dir / "discovery" / "manual-capture-queue.json", {}) or {}
    sales_report = read_json(run_dir / "discovery" / "assisted-sales-results.json", {}) or {}
    sales_runs = [value for value in sales_report.get("platform_runs") or [] if isinstance(value, dict)]
    sales_expected = int(sales_plan.get("task_count") or len(sales_plan.get("items") or []) or 0)
    report_completed_ids = {
        clean(value.get("query_id")) for value in sales_runs
        if value.get("delivery_eligible") is True and clean(value.get("query_id"))
    }
    artifact_completed_ids = physical_sales_artifact_task_ids(run_dir)
    sales_completed_ids = artifact_completed_ids
    sales_completed = len(sales_completed_ids)
    sales_live_progress = read_json(run_dir / "discovery" / "sales-live-progress.json", {}) or {}
    if not sales_runs and not artifact_completed_ids:
        sales_status = "not_started"
    elif (
        sales_expected and sales_completed == sales_expected
        and sales_report.get("execution_finished") is True
        and sales_report.get("matrix_complete") is True
    ):
        sales_status = "complete"
    else:
        sales_status = "in_progress_or_incomplete"
    return {
        "public_search_status": public_status,
        "public_search_expected_count": public_expected,
        "public_search_completed_count": public_completed,
        "public_search_blocked_count": public_blocked,
        "public_search_retryable_count": public_retryable,
        "sales_search_status": sales_status,
        "sales_search_expected_count": sales_expected,
        "sales_search_completed_count": sales_completed,
        "sales_search_report_completed_count": len(report_completed_ids),
        "sales_search_artifact_checkpoint_count": len(artifact_completed_ids),
        "sales_search_artifact_checkpoint_query_ids": sorted(artifact_completed_ids),
        "sales_search_live_progress": sales_live_progress or None,
    }


def reconcile_interrupted_running_phase(run_dir: Path, state: dict) -> dict:
    """Convert a stale in-flight checkpoint into a bounded resumable phase."""
    phase = clean(state.get("phase"))
    if phase not in {
        "stage_b_public_search_running",
        "stage_b_sales_search_running",
        "stage_b_parallel_search_running",
        "stage_b_parallel_search_evaluating",
        "stage_b_capture_and_packaging_running",
    }:
        return state
    progress = workflow_progress(run_dir)
    sales_work = sales_pending_partition(
        run_dir,
        [] if progress.get("sales_search_status") == "complete"
        else state.get("sales_search_pending_platforms") or [],
    )
    public_work = public_pending_partition(run_dir)
    channel_decision = resume_channel_decision(sales_work, public_work)
    if phase in {"stage_b_parallel_search_running", "stage_b_parallel_search_evaluating"}:
        public_status = progress.get("public_search_status")
        sales_status = progress.get("sales_search_status")
        if public_status == "complete" and sales_status == "complete":
            recovered_phase = "sales_platform_resume_ready"
        elif public_status == "verification_required" and not sales_work.get("runnable_pending"):
            recovered_phase = "public_search_verification_required"
        else:
            recovered_phase = channel_decision["phase"]
    elif phase == "stage_b_public_search_running":
        public_status = progress.get("public_search_status")
        if public_status == "complete":
            recovered_phase = "sales_platform_resume_ready"
        elif public_status == "verification_required" and not sales_work.get("runnable_pending"):
            recovered_phase = "public_search_verification_required"
        else:
            recovered_phase = channel_decision["phase"]
    elif phase == "stage_b_sales_search_running":
        if progress.get("sales_search_status") == "complete":
            recovered_phase = "sales_platform_resume_ready"
        else:
            recovered_phase = channel_decision["phase"]
    else:
        capture = read_json(run_dir / "capture" / "sales-after-login" / "capture-summary.json", {}) or {}
        recovered_phase = (
            "capture_complete"
            if capture.get("status") == "complete" and capture.get("visual_match_complete") is True
            else "capture_failed"
        )
    state.update({
        "phase": recovered_phase,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "interrupted_phase_recovery": {
            "schema_version": "1.0",
            "previous_phase": phase,
            "recovered_phase": recovered_phase,
            "network_request_performed": False,
            "recovered_at": datetime.now(timezone.utc).isoformat(),
            "factual_progress": progress,
        },
        "public_search_status": progress.get("public_search_status"),
        "sales_search_status": progress.get("sales_search_status"),
        "sales_pending_partition": sales_work,
        "public_search_pending_partition": public_work,
        "resume_channel_decision": channel_decision,
    })
    write_json(run_dir / "discovery" / "sales-workflow-state.json", state)
    return state


def powershell_argv_command(argv: list[str]) -> str:
    """Render argv for PowerShell without exposing path metacharacters to the shell."""
    return "& " + " ".join(
        "'" + str(value).replace("'", "''") + "'" for value in argv
    )


def posix_argv_command(argv: list[str]) -> str:
    """Render argv for CherryStudio's Bash-compatible command runner."""
    return " ".join(shlex.quote(str(value)) for value in argv)


def command_variants(argv: list[str]) -> dict[str, str]:
    return {
        "posix_bash": posix_argv_command(argv),
        "powershell": powershell_argv_command(argv),
    }


def managed_task_output_monitoring_contract() -> dict:
    """Return the compact policy embedded in every progress JSONL event."""
    return {
        "max_wait_seconds": MANAGED_TASK_OUTPUT_MAX_WAIT_SECONDS,
        "max_wait_calls_per_turn": MANAGED_TASK_OUTPUT_MAX_WAIT_CALLS_PER_TURN,
        "repeat_forbidden": True,
        "on_timeout": "status_once_then_return",
        "timeout_semantics": "wait_window_ended_only",
        "auto_resume": False,
    }


def managed_task_output_contract() -> dict:
    """Return the stable CherryStudio managed-task waiting policy."""
    monitoring = managed_task_output_monitoring_contract()
    return {
        "managed_task_output_contract": monitoring,
        "managed_task_output_contract_state": "available_once",
        "managed_task_output_wait_allowed": True,
        "managed_task_output_max_wait_seconds": monitoring["max_wait_seconds"],
        "max_wait_calls_per_turn": monitoring["max_wait_calls_per_turn"],
        "remaining_wait_calls_this_turn": monitoring["max_wait_calls_per_turn"],
        "managed_task_output_retry_allowed": False,
        "retry": False,
        "second_managed_task_output_forbidden": True,
        "managed_task_output_timeout_semantics": "wait_window_ended_only",
        "timeout_requires_single_status_then_return": True,
        "status_calls_after_timeout": 1,
        "timeout_requires_user_report": True,
        "timeout_requires_turn_end": True,
        "status_result_requires_turn_end": False,
        "return_now": False,
        "automatic_resume_allowed": False,
    }


def exhausted_managed_task_output_contract() -> dict:
    """Return a status-result contract with this turn's wait allowance consumed."""
    contract = managed_task_output_contract()
    contract.update({
        "managed_task_output_contract": {
            **contract["managed_task_output_contract"],
            "wait_allowed": False,
            "remaining_wait_calls_this_turn": 0,
            "retry": False,
            "status_result_requires_turn_end": True,
            "return_now": True,
        },
        "managed_task_output_contract_state": "exhausted_return_now",
        "managed_task_output_wait_allowed": False,
        "remaining_wait_calls_this_turn": 0,
        "managed_task_output_retry_allowed": False,
        "retry": False,
        "timeout_requires_turn_end": True,
        "status_result_requires_turn_end": True,
        "return_now": True,
        "automatic_resume_allowed": False,
    })
    return contract


def apply_status_return_now_contract(state: dict, instruction: str | None = None) -> dict:
    """Mark one local status result as report-only for the remainder of this turn."""
    result = dict(state)
    turn_instruction = clean(instruction) or (
        "本次status已是该回合唯一的本地状态检查；不得再次TaskOutput或resume，"
        "只向用户报告当前状态并立即结束本回合"
    )
    previous_contract = (
        result.get("execution_contract")
        if isinstance(result.get("execution_contract"), dict) else {}
    )
    result["execution_contract"] = {
        **previous_contract,
        **exhausted_managed_task_output_contract(),
        "instruction": turn_instruction,
    }
    if isinstance(result.get("resume_contract"), dict):
        result["resume_contract"] = {
            **result["resume_contract"],
            **exhausted_managed_task_output_contract(),
            "instruction": turn_instruction,
        }
    result["turn_control"] = {
        "state": "return_now",
        "remaining_wait_calls_this_turn": 0,
        "next_action_execution_allowed_this_turn": False,
        "status_result_requires_turn_end": True,
        "instruction": turn_instruction,
    }
    result["next_action_execution_allowed_this_turn"] = False
    if instruction:
        result["next_action"] = turn_instruction
        result["operator_message"] = turn_instruction
    else:
        existing_operator_message = clean(result.get("operator_message"))
        result["operator_message"] = " ".join(
            value for value in (existing_operator_message, turn_instruction) if value
        )
    return result


def canonical_resume_spec(run_dir: Path) -> tuple[list[str], str]:
    argv = [sys.executable, str(Path(__file__).resolve()), "resume", "--run-dir", str(run_dir.resolve())]
    command = posix_argv_command(argv)
    return argv, command


def canonical_publish_spec(run_dir: Path) -> tuple[list[str], str]:
    argv = [sys.executable, str(Path(__file__).resolve()), "publish", "--run-dir", str(run_dir.resolve())]
    return argv, posix_argv_command(argv)


def canonical_partial_publish_spec(
    run_dir: Path, request_json: Path, output_dir: Path | None = None,
) -> tuple[list[str], str]:
    argv = [
        sys.executable, str(Path(__file__).resolve()), "publish-partial",
        "--run-dir", str(run_dir.resolve()), "--request-json", str(request_json.resolve()),
    ]
    if output_dir is not None:
        argv.extend(["--output-dir", str(output_dir.resolve())])
    return argv, posix_argv_command(argv)


def canonical_reopen_browser_spec(run_dir: Path, browser: str) -> tuple[list[str], str]:
    scripts = Path(__file__).resolve().parent
    state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
    config = read_json(run_dir / "run-config.json", {}) or {}
    orchestration = config.get("cherrystudio_orchestration") or {}
    argv = [
        sys.executable, str(scripts / "open-sales-login-profile.py"),
        "--run-dir", str(run_dir.resolve()), "--browser", browser,
    ]
    executable = clean(state.get("browser_executable") or orchestration.get("browser_executable"))
    user_data = clean(
        state.get("browser_user_data") or state.get("edge_user_data")
        or orchestration.get("browser_user_data")
    )
    profile = clean(state.get("profile_directory") or orchestration.get("profile_directory"))
    if executable:
        argv.extend(["--browser-executable", executable])
    if user_data:
        argv.extend(["--browser-user-data", user_data])
    if profile:
        argv.extend(["--profile-directory", profile])
    for platform in PLATFORMS:
        argv.extend(["--platform", platform])
    return argv, posix_argv_command(argv)


def probe_loopback_cdp(endpoint: object, expected_browser: str) -> dict:
    """Probe only the local browser debugger; never follow an external URL."""
    value = clean(endpoint)
    result = {
        "schema_version": "1.0",
        "record_type": "loopback_cdp_liveness",
        "ok": False,
        "endpoint": value or None,
        "expected_browser": expected_browser or None,
        "browser": None,
        "error": None,
    }
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("cdp_endpoint_is_not_loopback_origin")
        with urlopen(f"{value.rstrip('/')}/json/version", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        product = clean(payload.get("Browser"))
        folded = product.casefold()
        matches = (
            expected_browser == "edge" and ("edg/" in folded or "microsoft edge" in folded)
        ) or (
            expected_browser == "chrome" and "chrome/" in folded and "edg/" not in folded
        )
        if not matches:
            raise ValueError("cdp_browser_product_mismatch")
        result.update({"ok": True, "browser": product})
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def live_exact_qcc_tab(endpoint: object, expected_url: object) -> dict:
    endpoint_value = clean(endpoint).rstrip("/")
    url_value = clean(expected_url)
    result = {
        "confirmed": False,
        "expected_url": url_value or None,
        "target_id": None,
        "actual_url": None,
    }
    if not endpoint_value or not is_exact_qcc_brand_url(url_value):
        return result
    try:
        with urlopen(f"{endpoint_value}/json/list", timeout=2) as response:
            targets = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        return result
    normalized_expected = url_value.rstrip("/")
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        actual = clean(target.get("url")).rstrip("/")
        if actual == normalized_expected:
            result.update({
                "confirmed": True,
                "target_id": clean(target.get("id")) or None,
                "actual_url": clean(target.get("url")) or None,
            })
            break
    return result


def live_sales_verification_tabs(endpoint: object, pending_platforms: list[str]) -> dict:
    endpoint_value = clean(endpoint).rstrip("/")
    requested = [value for value in dict.fromkeys(pending_platforms) if value in PLATFORMS]
    found = []
    if endpoint_value:
        try:
            with urlopen(f"{endpoint_value}/json/list", timeout=2) as response:
                targets = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception:
            targets = []
        for target in targets if isinstance(targets, list) else []:
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            sample = f"{target.get('title') or ''}\n{target.get('url') or ''}"
            if not LIVE_VERIFICATION_RE.search(sample):
                continue
            try:
                host = (urlsplit(clean(target.get("url"))).hostname or "").casefold()
            except ValueError:
                continue
            platform = next((
                value for value in requested
                if any(host == domain or host.endswith(f".{domain}") for domain in SALES_VERIFICATION_DOMAINS[value])
            ), None)
            if platform:
                found.append({
                    "platform": platform,
                    "target_id": clean(target.get("id")) or None,
                    "url": clean(target.get("url")) or None,
                    "title": clean(target.get("title")) or None,
                })
    visible = list(dict.fromkeys(value["platform"] for value in found))
    return {
        "confirmed": bool(found),
        "visible_platforms": visible,
        "missing_platforms": [value for value in requested if value not in visible],
        "tabs": found,
    }


def resolve_live_browser_attachment(state: dict, expected_browser: str) -> dict:
    """Validate the saved CDP origin or recover one from the exact dedicated profile."""
    saved_endpoint = clean(state.get("cdp_endpoint"))
    saved = probe_loopback_cdp(saved_endpoint, expected_browser)
    saved["endpoint_recovered"] = False
    if saved.get("ok") is True:
        return saved
    user_data = clean(state.get("browser_user_data") or state.get("edge_user_data"))
    if not user_data:
        return saved
    try:
        candidates = running_browser_cdp_sessions(expected_browser, Path(user_data))
    except Exception as exc:
        saved["recovery_error"] = f"{type(exc).__name__}: {exc}"
        return saved
    live = []
    for candidate in candidates:
        validation = probe_loopback_cdp(candidate.get("endpoint"), expected_browser)
        if validation.get("ok") is True:
            live.append((candidate, validation))
    if len(live) != 1:
        saved["recovery_error"] = (
            "no_validated_running_profile_cdp" if not live
            else "multiple_validated_running_profile_cdp_endpoints"
        )
        return saved
    session, validation = live[0]
    validation.update({
        "endpoint_recovered": True,
        "previous_endpoint": saved_endpoint or None,
        "process_id": session.get("process_id"),
    })
    return validation


def stamp_validated_cdp_recovery(config: dict, state: dict, attachment: dict) -> bool:
    """Bind a changed CDP port to the otherwise immutable browser identity."""
    orchestration = config.get("cherrystudio_orchestration") or {}
    previous = clean(orchestration.get("cdp_endpoint")).rstrip("/")
    endpoint = clean(attachment.get("endpoint") or state.get("cdp_endpoint")).rstrip("/")
    if attachment.get("ok") is not True or not previous or not endpoint or previous == endpoint:
        return False

    def same_path(left, right) -> bool:
        try:
            return os.path.normcase(str(Path(str(left)).resolve())) == os.path.normcase(
                str(Path(str(right)).resolve())
            )
        except (OSError, RuntimeError, ValueError):
            return False

    record = {
        "schema_version": "1.0",
        "record_type": "validated_cdp_endpoint_recovery",
        "source": "same_dedicated_profile_loopback_cdp",
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "previous_endpoint": previous,
        "endpoint": endpoint,
        "expected_browser": clean(orchestration.get("selected_browser")).casefold(),
        "detected_browser": clean(attachment.get("browser")),
        "process_id": attachment.get("process_id") or state.get("login_browser_pid"),
        "same_browser_user_data": same_path(
            orchestration.get("browser_user_data"),
            state.get("browser_user_data") or state.get("edge_user_data"),
        ),
        "same_browser_executable": same_path(
            orchestration.get("browser_executable"), state.get("browser_executable"),
        ),
        "same_profile_directory": clean(orchestration.get("profile_directory")) == clean(
            state.get("profile_directory")
        ),
    }
    state["cdp_endpoint_recovery"] = record
    return True


def qcc_fetch_command(scripts: Path, run_dir: Path, brand_url: str | None = None) -> list[str]:
    command = [
        sys.executable, str(scripts / "fetch-qcc-trademark-reference.py"),
        "--run-dir", str(run_dir),
    ]
    if clean(brand_url):
        command.extend(["--brand-url", clean(brand_url)])
    return command


def validate_receipt_bound_bundle(run_dir: Path, receipt: dict, config: dict | None = None) -> tuple[list[str], dict]:
    """Recheck audited bundle hashes without reopening every PDF page."""
    errors: list[str] = []
    deliverable = receipt.get("deliverable") if isinstance(receipt.get("deliverable"), dict) else {}
    if receipt.get("record_type") != "cherrystudio_completion_receipt":
        errors.append("completion_receipt_record_type_invalid")
    if receipt.get("status") != "completed" or receipt.get("validation", {}).get("ok") is not True:
        errors.append("completion_receipt_not_completed")
    if config and clean(receipt.get("run_id")) != clean(config.get("run_id")):
        errors.append("completion_receipt_run_id_mismatch")
    if deliverable.get("pdf") != "all-detected-html-pages.pdf":
        errors.append("completion_receipt_pdf_name_invalid")
    if deliverable.get("manifest") != "all-detected-html-pages.manifest.json":
        errors.append("completion_receipt_manifest_name_invalid")
    pdf_path = run_dir / "all-detected-html-pages.pdf"
    manifest_path = run_dir / "all-detected-html-pages.manifest.json"
    if not pdf_path.is_file():
        errors.append("all_detected_html_pdf_missing")
    elif file_sha256(pdf_path) != clean(deliverable.get("pdf_sha256")):
        errors.append("all_detected_html_pdf_receipt_hash_mismatch")
    if not manifest_path.is_file():
        errors.append("all_detected_html_manifest_missing")
    elif file_sha256(manifest_path) != clean(deliverable.get("manifest_sha256")):
        errors.append("all_detected_html_manifest_receipt_hash_mismatch")
    manifest = read_json(manifest_path, {}) or {}
    if clean(manifest.get("output_sha256")) != clean(deliverable.get("pdf_sha256")):
        errors.append("all_detected_manifest_pdf_hash_not_bound_to_receipt")
    if config and clean(manifest.get("run_id")) != clean(config.get("run_id")):
        errors.append("all_detected_manifest_run_id_mismatch")
    for field in ("page_count", "embedded_html_count"):
        try:
            matches = int(manifest.get(field)) == int(deliverable.get(field))
        except (TypeError, ValueError):
            matches = False
        if not matches:
            errors.append(f"all_detected_manifest_{field}_not_bound_to_receipt")
    bundle = {
        "ok": not errors,
        "validation_level": "receipt_bound_hash_recheck",
        "pdf_sha256": deliverable.get("pdf_sha256"),
        "manifest_sha256": deliverable.get("manifest_sha256"),
        "page_count": deliverable.get("page_count"),
        "embedded_html_count": deliverable.get("embedded_html_count"),
        "timestamp_footer_pages": deliverable.get("page_count"),
        "source_url_footer_pages": deliverable.get("page_count"),
        "uri_link_count": deliverable.get("uri_link_count"),
    }
    return list(dict.fromkeys(errors)), bundle


def validate_formal_published_record(run_dir: Path, config: dict, bundle: dict) -> tuple[list[str], dict]:
    record = read_json(run_dir / "cherrystudio-published-delivery.json", {}) or {}
    if not record:
        return ["published_delivery_record_missing"], {}
    errors: list[str] = []
    expected_fields = {
        "record_type": "cherrystudio_published_delivery",
        "delivery_authorized": True,
        "page_count": bundle.get("page_count"),
        "embedded_html_count": bundle.get("embedded_html_count"),
        "timestamp_footer_pages": bundle.get("timestamp_footer_pages"),
        "source_url_footer_pages": bundle.get("source_url_footer_pages"),
        "uri_link_count": bundle.get("uri_link_count"),
    }
    for field, expected_value in expected_fields.items():
        if record.get(field) != expected_value:
            errors.append(f"published_delivery_{field}_mismatch")
    if clean(record.get("run_id")) != clean(config.get("run_id")):
        errors.append("published_delivery_run_id_mismatch")
    expected = {
        "pdf": ("pdf_sha256", clean(bundle.get("pdf_sha256"))),
        "manifest": ("manifest_sha256", clean(bundle.get("manifest_sha256"))),
        "completion_receipt": (
            "completion_receipt_sha256",
            file_sha256(run_dir / "cherrystudio-completion-receipt.json")
            if (run_dir / "cherrystudio-completion-receipt.json").is_file() else "",
        ),
    }
    for field, (hash_field, bound_hash) in expected.items():
        path_value = clean(record.get(field))
        path = Path(path_value) if path_value else None
        if path is None or not path.is_absolute() or not path.is_file():
            errors.append(f"published_delivery_{field}_missing")
            continue
        actual = file_sha256(path)
        if actual != clean(record.get(hash_field)).casefold():
            errors.append(f"published_delivery_{field}_hash_mismatch")
        if bound_hash and actual != bound_hash:
            errors.append(f"published_delivery_{field}_not_bound_to_run_bundle")
    return list(dict.fromkeys(errors)), record


def machine_state(run_dir: Path, **updates) -> dict:
    path = run_dir / "cherrystudio-machine-state.json"
    state = read_json(path, {}) or {}
    # A state transition must not carry a resolved error or an obsolete manual
    # action into the next phase.  Persistent browser/run identity fields are
    # deliberately retained, while phase-local guidance is replaced.
    for key in TRANSIENT_MACHINE_STATE_FIELDS:
        state.pop(key, None)
    state.update({
        "schema_version": "1.0",
        "record_type": "cherrystudio_trademark_machine_state",
        "run_dir": str(run_dir),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **updates,
    })
    resolved_qcc_url = clean(state.get("resolved_qcc_url"))
    if resolved_qcc_url and is_exact_qcc_brand_url(resolved_qcc_url):
        previous_qcc_url = clean(state.get("qcc_url"))
        if previous_qcc_url:
            state.setdefault("qcc_initial_url", previous_qcc_url)
        state["qcc_url"] = resolved_qcc_url
        state["qcc_target_kind"] = "exact_brand_detail"
        state["qcc_exact_detail_resolved"] = True
    progress = workflow_progress(run_dir)
    state.update(progress)
    workflow_state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
    pending_platform_hint = (
        updates.get("pending_sales_platforms")
        or workflow_state.get("sales_search_pending_platforms")
        or []
    )
    sales_work = sales_pending_partition(run_dir, pending_platform_hint)
    public_work = public_pending_partition(run_dir)
    channel_decision = resume_channel_decision(sales_work, public_work)
    state.update({
        "sales_pending_partition": sales_work,
        "public_search_pending_partition": public_work,
        "resume_channel_decision": channel_decision,
        "runnable_sales_pending_count": sales_work["runnable_pending_count"],
        "cooling_sales_pending_count": sales_work["cooling_pending_count"],
        "verification_sales_pending_count": sales_work["verification_pending_count"],
        "runnable_public_pending_count": public_work["runnable_pending_count"],
        "cooling_public_pending_count": public_work["cooling_pending_count"],
        "verification_public_pending_count": public_work["verification_pending_count"],
    })
    validation = state.get("validation") if isinstance(state.get("validation"), dict) else {}
    receipt = read_json(run_dir / "cherrystudio-completion-receipt.json", {}) or {}
    receipt_delivery = receipt.get("deliverable") if isinstance(receipt.get("deliverable"), dict) else {}
    bundle_errors: list[str] = ["terminal_state_not_completed"]
    bundle: dict = {}
    if state.get("status") == "completed" and validation.get("ok") is True:
        bundle_errors, bundle = validate_receipt_bound_bundle(
            run_dir, receipt, read_json(run_dir / "run-config.json", {}) or {},
        )
    delivery_authorized = bool(
        state.get("status") == "completed"
        and validation.get("ok") is True
        and not bundle_errors
        and bundle.get("ok") is True
        and receipt.get("validation", {}).get("ok") is True
        and receipt_delivery.get("pdf") == "all-detected-html-pages.pdf"
        and receipt_delivery.get("manifest") == "all-detected-html-pages.manifest.json"
        and receipt_delivery.get("pdf_sha256") == bundle.get("pdf_sha256")
        and receipt_delivery.get("manifest_sha256") == bundle.get("manifest_sha256")
        and receipt_delivery.get("page_count") == bundle.get("page_count")
        and receipt_delivery.get("embedded_html_count") == bundle.get("embedded_html_count")
    )
    state["delivery_authorized"] = delivery_authorized
    state["delivery_physical_validation"] = {
        "ok": delivery_authorized,
        "errors": [] if delivery_authorized else bundle_errors,
        **({"bundle": bundle} if bundle else {}),
    }
    state["provisional_pdf_present"] = bool(
        not delivery_authorized and (run_dir / "all-detected-html-pages.pdf").is_file()
    )
    state["deliverables"] = [
        name for name in (
            "all-detected-html-pages.pdf",
            "all-detected-html-pages.manifest.json",
            "cherrystudio-completion-receipt.json",
        ) if delivery_authorized and (run_dir / name).is_file()
    ]
    state["internal_non_deliverable_artifacts"] = [
        name for name in ("related-web-results.pdf", "related-web-results.manifest.json")
        if (run_dir / name).is_file()
    ]
    state["completion_claim_allowed"] = delivery_authorized
    formal_record_errors: list[str] = []
    formal_record: dict = {}
    if delivery_authorized:
        formal_record_errors, formal_record = validate_formal_published_record(
            run_dir, read_json(run_dir / "run-config.json", {}) or {}, bundle,
        )
    if formal_record and not formal_record_errors:
        state["published_delivery"] = formal_record
    else:
        state.pop("published_delivery", None)
        if delivery_authorized:
            state["completion_claim_allowed"] = False
    state["published_delivery_validation"] = {
        "ok": bool(formal_record and not formal_record_errors),
        "errors": formal_record_errors,
    }
    partial_record_errors: list[str] = []
    partial_record: dict = {}
    if (run_dir / PARTIAL_PUBLISHED_RECORD_NAME).is_file():
        partial_record_errors, partial_record = validate_partial_published_record(run_dir)
    partial_published = bool(partial_record and not partial_record_errors)
    if partial_published:
        state["partial_materials_outputs"] = partial_record
    else:
        state.pop("partial_materials_outputs", None)
    accepted_task_count = int(progress.get("public_search_completed_count") or 0) + int(
        progress.get("sales_search_completed_count") or 0
    )
    state["partial_materials_policy"] = {
        "official_action": "publish-partial",
        "customer_request_required": True,
        "request_json_required": True,
        "eligible_only_while_incomplete": True,
        "accepted_non_reference_task_required": True,
        "currently_eligible_for_explicit_request": bool(not delivery_authorized and accepted_task_count > 0),
        "published_record_valid": partial_published,
        "published_record_errors": partial_record_errors,
        "investigation_complete": False,
        "delivery_authorized": False,
        "completion_claim_allowed": False,
        "formal_publish_gate_unchanged": True,
    }
    state["platform_account_status"] = "not_determined"
    state["platform_freeze_observed"] = False
    state["platform_reported_wait_seconds"] = None
    state["claim_policy"] = {
        "allowed_status_claim": "流程校验通过" if delivery_authorized else str(state.get("status") or "incomplete"),
        "investigation_complete_claim_allowed": False,
        "legal_use_conclusion_allowed": False,
        "forbidden_claims": ["调查完成", "全面检索完成", "未发现实际使用", "不存在使用", "账号被冻结"],
    }
    state["forbidden_fallbacks"] = {
        "active": not delivery_authorized,
        "tools": [
            "WebFetch", "WebSearch", "web_fetch", "web_search",
            "mcp__cherry-tools__web_fetch", "mcp__cherry-tools__web_search",
            "exa:web_search_exa", "exa:web_fetch_exa",
            "mcp__exa__web_search_exa", "mcp__exa__web_fetch_exa",
        ],
        "actions": [
            "alternate_search", "manual_web_investigation", "manual_pdf_generation",
            "unofficial_low_level_script",
        ],
        "official_nonzero_behavior": "stop_and_report_operator_message",
        "website_access_owner": "cherrystudio_orchestrator_attached_browser_only",
    }
    state["output_policy"] = {
        "publish_authorized": delivery_authorized,
        "only_publishable_pdf": "all-detected-html-pages.pdf" if delivery_authorized else None,
        "required_manifest": "all-detected-html-pages.manifest.json",
        "required_html_attachments": True,
        "required_timestamp_footer_on_every_page": True,
        "required_source_url_footer_on_every_page": True,
        "required_clickable_source_urls": True,
        "forbidden_alternative_pdf_tools": ["FPDF", "ReportLab", "PyPDF2", "pypdf", "PdfMerger"],
        "forbidden_actions_when_unauthorized": [
            "create_summary_pdf", "merge_evidence_pdf", "copy_pdf_to_desktop",
            "write_narrative_report", "claim_investigation_complete",
        ],
        "customer_requested_partial_exception": {
            "official_action_only": "publish-partial",
            "request_json_required": True,
            "distinct_pdf": PARTIAL_PDF_NAME,
            "visible_incomplete_disclaimer_required": False,
            "visible_partial_disclosures_absent": True,
            "formal_delivery_authorized": False,
        },
    }
    status_argv = [
        sys.executable, str(Path(__file__).resolve()), "status", "--run-dir", str(run_dir.resolve()),
    ]
    state["execution_contract"] = {
        "official_commands_foreground_only": False,
        "official_commands_foreground_preferred": True,
        "shell_backgrounding_allowed": False,
        "managed_background_fallback_allowed": True,
        **managed_task_output_contract(),
        "duplicate_resume_while_active_forbidden": True,
        "shell_wait_allowed": False,
        "blind_polling_allowed": False,
        "increasing_wait_intervals_allowed": False,
        "progress_source": str(run_dir / "cherrystudio-execution-state.json"),
        "status_argv": status_argv,
        "status_command": posix_argv_command(status_argv),
        "status_commands": command_variants(status_argv),
        "status_is_local_only": True,
        "instruction": (
            "优先同步等待当前官方命令返回；若命令工具自动返回受管后台任务ID，"
            "本回合最多调用一次TaskOutput，单次最长60秒。TaskOutput timeout只表示等待窗口结束，"
            "不表示后台任务完成或失败；超时后只执行一次本地status_command，向用户报告机器状态并结束本回合。"
            "禁止第二次TaskOutput、600秒或递增等待、shell sleep及自动resume；也禁止并发执行第二次resume。"
        ),
    }
    if delivery_authorized:
        publish_argv, publish_command = canonical_publish_spec(run_dir)
        state["publish_argv"] = publish_argv
        state["publish_command"] = publish_command
        state["publish_commands"] = command_variants(publish_argv)
        state["operator_message"] = (
            "流程工件已通过终态审计；只允许执行publish_command发布唯一HTML证据汇编PDF。"
            "不得另写摘要报告或法律结论。"
        )
    else:
        state["operator_message"] = (
            f"当前状态为{state.get('status') or 'incomplete'}，调查未完成且无正式PDF发布授权。"
            "不得使用FPDF、ReportLab、PyPDF2、pypdf、临时脚本或PDF合并器创建、拼接、复制任何替代报告；"
            "只能转述本机器状态并按required_user_action/next_action继续。"
        )
    if state.get("resume_command"):
        resume_argv, canonical_command = canonical_resume_spec(run_dir)
        state["resume_command"] = canonical_command
        state["resume_argv"] = resume_argv
        state["resume_commands"] = command_variants(resume_argv)
        state["resume_contract"] = {
            "execute_verbatim": True,
            "default_shell": "posix_bash",
            "select_matching_shell_variant": True,
            "extra_arguments_allowed": False,
            "all_requested_outputs_are_read_from_run_config": True,
            "foreground_only": False,
            "foreground_preferred": True,
            "shell_backgrounding_allowed": False,
            "managed_background_fallback_allowed": True,
            **managed_task_output_contract(),
            "duplicate_resume_while_active_forbidden": True,
            "shell_wait_allowed": False,
            "blind_polling_allowed": False,
        }
    if delivery_authorized:
        published = state.get("published_delivery") if isinstance(state.get("published_delivery"), dict) else {}
        if published.get("pdf"):
            state["operator_message"] = (
                f"流程终审和物理校验已通过，唯一HTML证据汇编PDF已由官方publish发布：{published.get('pdf')}。"
                "同时发布manifest与completion receipt；不得再次生成、合并或改写替代报告，也不得作法律使用结论。"
            )
        else:
            state["operator_message"] = (
                "流程终审和物理校验已通过；只允许原样执行publish_command发布唯一HTML证据汇编PDF。"
                "不得另写摘要报告、合并PDF或作法律使用结论。"
            )
    else:
        operator_parts = [
            f"当前状态为{state.get('status') or 'incomplete'}，阶段为{state.get('phase') or 'incomplete'}；"
            "调查未完成且没有正式PDF发布授权。",
        ]
        if state.get("required_user_action"):
            operator_parts.append(f"需要人工操作：{state['required_user_action']}")
        if state.get("next_action"):
            operator_parts.append(f"下一步：{state['next_action']}")
        if state.get("resume_command"):
            operator_parts.append(f"恢复命令（必须原样执行）：{state['resume_command']}")
        operator_parts.append(
            "不得使用FPDF、ReportLab、PyPDF2、pypdf、临时脚本或PDF合并器创建、拼接、复制任何替代报告，"
            "不得声称调查完成、全面检索完成、未发现实际使用、账号被冻结；"
            "不得用命令文本自行后台启动；命令工具自动返回受管任务ID时，"
            "本回合最多调用一次TaskOutput且最长60秒；TaskOutput timeout只表示等待窗口结束，"
            "超时后只执行一次本地status_command、向用户报告并结束本回合；"
            "禁止第二次TaskOutput、600秒或递增等待、sleep、Start-Sleep、timeout和自动resume，也禁止第二次resume；"
            "官方入口非零后不得改用Exa、WebFetch、WebSearch、其他搜索抓取工具或手工网络调查，"
            "只能保留机器状态并按本operator_message继续。"
        )
        if partial_published:
            operator_parts.append(
                f"客户明确请求的部分调查材料已由官方publish-partial发布：{partial_record.get('pdf')}。"
                "该文件仍属调查未完成的部分材料，不得转述为正式完成报告或完整检索结论。"
            )
        elif accepted_task_count > 0:
            operator_parts.append(
                "若且仅若客户明确要求在当前未完成状态下取得已有材料，可创建保留其原话的请求JSON并执行"
                "官方publish-partial；该例外不改变delivery_authorized=false，也不得作完整或法律使用结论。"
            )
        state["operator_message"] = " ".join(operator_parts)
    write_json(path, state)
    return state


def validate_partial_materials_request(
    request_json: Path,
    expected_run_id: str | None = None,
    *,
    now_utc: datetime | None = None,
) -> dict:
    """Validate and normalize the customer's explicit request for an incomplete packet."""
    request_json = request_json.resolve()
    if not request_json.is_file():
        raise FileNotFoundError(f"partial-material request JSON is missing: {request_json}")
    raw = read_external_json(request_json, None)
    if not isinstance(raw, dict):
        raise ValueError("partial-material request JSON must contain one JSON object")
    request_text = clean(raw.get("request_text"))
    if raw.get("allow_partial_materials") is not True:
        raise ValueError("partial-material request must set allow_partial_materials=true")
    if not request_text:
        raise ValueError("partial-material request_text must preserve the customer's explicit request")
    requested_raw = clean(raw.get("requested_at"))
    if not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", requested_raw, re.I):
        raise ValueError("partial-material requested_at must include an explicit timezone")
    requested_at = parse_utc_timestamp(requested_raw)
    if requested_at is None:
        raise ValueError("partial-material requested_at must be an ISO-8601 timestamp with a timezone")
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if requested_at < now - timedelta(hours=24) or requested_at > now + timedelta(minutes=5):
        raise ValueError("partial-material requested_at must be within the current 24-hour request window")
    request_run_id = clean(raw.get("run_id"))
    if expected_run_id and request_run_id != clean(expected_run_id):
        raise ValueError("partial-material request run_id does not match the target RUN")
    return {
        "schema_version": "1.0",
        "record_type": PARTIAL_REQUEST_RECORD_TYPE,
        "request_text": request_text,
        "run_id": request_run_id or None,
        "allow_partial_materials": True,
        "requested_at": requested_at.isoformat(),
        "received_at": now.isoformat(),
        "source_request_json": str(request_json),
        "source_request_sha256": file_sha256(request_json),
        "investigation_complete": False,
        "delivery_authorized": False,
        "completion_claim_allowed": False,
    }


def validate_partial_materials_bundle(run_dir: Path) -> tuple[list[str], dict]:
    """Physically validate the distinct incomplete packet without authorizing formal completion."""
    try:
        import fitz
    except ImportError:
        return ["pymupdf_unavailable_for_partial_bundle_validation"], {"ok": False}
    run_dir = run_dir.resolve()
    config = read_json(run_dir / "run-config.json", {}) or {}
    pdf_path = run_dir / PARTIAL_PDF_NAME
    manifest_path = run_dir / PARTIAL_MANIFEST_NAME
    errors: list[str] = []
    manifest = read_json(manifest_path, None)
    if not pdf_path.is_file():
        errors.append("partial_pdf_missing")
    if not isinstance(manifest, dict):
        errors.append("partial_manifest_missing_or_invalid")
        manifest = {}
    expected_manifest_fields = {
        "record_type": PARTIAL_MANIFEST_RECORD_TYPE,
        "status": PARTIAL_STATUS,
        "partial_materials": True,
        "investigation_complete": False,
        "delivery_authorized": False,
        "completion_claim_allowed": False,
    }
    for key, value in expected_manifest_fields.items():
        if manifest.get(key) != value:
            errors.append(f"partial_manifest_{key}_mismatch")
    if clean(manifest.get("run_id")) != clean(config.get("run_id") or run_dir.name):
        errors.append("partial_manifest_run_id_mismatch")
    if manifest.get("partial_materials_validation", {}).get("ok") is not True:
        errors.append("partial_materials_validation_not_ok")
    if manifest.get("partial_materials_validation", {}).get("visible_partial_disclosures_absent") is not True:
        errors.append("partial_materials_visible_disclosure_validation_missing")
    if int(manifest.get("partial_materials_visible_disclosure_pages") or 0) != 0:
        errors.append("partial_materials_visible_disclosure_count_nonzero")
    if manifest.get("validation", {}).get("ok") is not False:
        errors.append("partial_manifest_must_not_claim_formal_validation")
    if manifest.get("validation", {}).get("failed_or_blocked_pages_included") != 0:
        errors.append("blocked_or_failed_pages_included")
    task_coverage = manifest.get("task_coverage") if isinstance(manifest.get("task_coverage"), dict) else {}
    if int(task_coverage.get("completed_task_count") or 0) < 1:
        errors.append("no_packet_eligible_completed_task")
    if int(task_coverage.get("incomplete_task_count") or 0) < 1:
        errors.append("partial_packet_has_no_documented_task_gap")
    if manifest.get("output") != PARTIAL_PDF_NAME:
        errors.append("partial_manifest_output_name_mismatch")
    if not any(
        isinstance(source, dict)
        and (clean(source.get("id")) == "QCC" or clean(source.get("platform")) == "reference")
        for source in manifest.get("sources") or []
    ):
        errors.append("validated_qcc_reference_not_in_partial_packet")

    bundle = {
        "ok": False,
        "pdf": PARTIAL_PDF_NAME,
        "manifest": PARTIAL_MANIFEST_NAME,
        "page_count": 0,
        "embedded_html_count": 0,
    }
    if errors or not pdf_path.is_file():
        return errors, bundle
    pdf_hash = file_sha256(pdf_path)
    manifest_hash = file_sha256(manifest_path)
    if manifest.get("output_sha256") != pdf_hash:
        errors.append("partial_pdf_sha256_mismatch")
    if int(manifest.get("output_size_bytes") or -1) != pdf_path.stat().st_size:
        errors.append("partial_pdf_size_mismatch")
    try:
        pdf = fitz.open(pdf_path)
    except Exception as error:
        errors.append(f"partial_pdf_open_failed:{type(error).__name__}")
        return errors, bundle
    uri_links: set[str] = set()
    timestamp_pages = 0
    source_url_pages = 0
    visible_disclosure_pages = 0
    try:
        if not pdf.is_pdf or pdf.needs_pass or pdf.page_count < 1:
            errors.append("partial_pdf_invalid")
        if pdf.page_count != int(manifest.get("page_count") or -1):
            errors.append("partial_pdf_page_count_mismatch")
        for page in pdf:
            text = page.get_text("text")
            timestamp_pages += int("存储时间：" in text)
            source_url_pages += int("来源网址：" in text)
            visible_disclosure_pages += int(any(
                disclosure in text for disclosure in PARTIAL_PDF_PROHIBITED_DISCLOSURES
            ))
            for link in page.get_links():
                if link.get("kind") == fitz.LINK_URI and link.get("uri"):
                    uri_links.add(str(link["uri"]))
        if timestamp_pages != pdf.page_count:
            errors.append("partial_timestamp_footer_missing")
        if source_url_pages != pdf.page_count:
            errors.append("partial_source_url_footer_missing")
        if visible_disclosure_pages:
            errors.append("partial_visible_disclosure_present")
        attachments = manifest.get("attachments") if isinstance(manifest.get("attachments"), list) else []
        names = set(pdf.embfile_names())
        if len(names) != len(attachments) or len(names) != int(manifest.get("embedded_html_count") or -1):
            errors.append("partial_embedded_html_count_mismatch")
        for attachment in attachments:
            name = clean(attachment.get("attachment_name")) if isinstance(attachment, dict) else ""
            expected_hash = clean(attachment.get("sha256")) if isinstance(attachment, dict) else ""
            if not name or name not in names:
                errors.append(f"partial_attachment_missing:{name or 'unknown'}")
                continue
            if hashlib.sha256(pdf.embfile_get(name)).hexdigest() != expected_hash:
                errors.append(f"partial_attachment_hash_mismatch:{name}")
        expected_urls = {
            clean(source.get("url")) for source in manifest.get("sources") or []
            if isinstance(source, dict) and clean(source.get("url"))
        }
        missing_urls = sorted(expected_urls - uri_links)
        if missing_urls:
            errors.append("partial_source_urls_not_clickable:" + ",".join(missing_urls[:5]))
        prohibited_states = {
            clean(source.get("status")) for source in manifest.get("sources") or []
            if isinstance(source, dict)
            and clean(source.get("platform")) != "reference"
            and clean(source.get("status")) not in {"normal", "zero", "zero_results"}
        }
        if prohibited_states:
            errors.append("partial_ineligible_source_states:" + ",".join(sorted(prohibited_states)))
        bundle.update({
            "pdf_sha256": pdf_hash,
            "manifest_sha256": manifest_hash,
            "page_count": pdf.page_count,
            "embedded_html_count": len(names),
            "timestamp_footer_pages": timestamp_pages,
            "source_url_footer_pages": source_url_pages,
            "visible_disclosure_pages": visible_disclosure_pages,
            "uri_link_count": len(uri_links),
            "task_coverage": task_coverage,
        })
    finally:
        pdf.close()
    bundle["ok"] = not errors
    return errors, bundle


def validate_partial_published_record(run_dir: Path) -> tuple[list[str], dict]:
    """Validate the independent published-record chain used by status and report guards."""
    run_dir = run_dir.resolve()
    record = read_json(run_dir / PARTIAL_PUBLISHED_RECORD_NAME, None)
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["partial_published_record_missing_or_invalid"], {}
    config = read_json(run_dir / "run-config.json", {}) or {}
    expected_run_id = clean(config.get("run_id") or run_dir.name)
    expected = {
        "record_type": PARTIAL_PUBLISHED_RECORD_TYPE,
        "partial_materials_authorized": True,
        "customer_requested": True,
        "investigation_complete": False,
        "delivery_authorized": False,
        "completion_claim_allowed": False,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            errors.append(f"partial_published_{key}_mismatch")
    if clean(record.get("run_id")) != expected_run_id:
        errors.append("partial_published_run_id_mismatch")
    for field, hash_field in (
        ("pdf", "pdf_sha256"),
        ("manifest", "manifest_sha256"),
        ("receipt", "receipt_sha256"),
        ("request_record", "request_sha256"),
    ):
        path = Path(clean(record.get(field))) if clean(record.get(field)) else None
        if path is None or not path.is_absolute() or not path.is_file():
            errors.append(f"partial_published_{field}_missing")
            continue
        if file_sha256(path) != clean(record.get(hash_field)):
            errors.append(f"partial_published_{field}_hash_mismatch")
    bundle_errors, bundle = validate_partial_materials_bundle(run_dir)
    errors.extend(bundle_errors)
    if bundle:
        if clean(record.get("pdf_sha256")) != clean(bundle.get("pdf_sha256")):
            errors.append("partial_published_pdf_not_bound_to_run_bundle")
        if clean(record.get("manifest_sha256")) != clean(bundle.get("manifest_sha256")):
            errors.append("partial_published_manifest_not_bound_to_run_bundle")
        for field in (
            "page_count", "embedded_html_count", "timestamp_footer_pages",
            "source_url_footer_pages", "visible_disclosure_pages", "uri_link_count",
        ):
            if record.get(field) != bundle.get(field):
                errors.append(f"partial_published_{field}_not_bound_to_run_bundle")
    request_record = read_json(Path(clean(record.get("request_record"))), {}) if clean(record.get("request_record")) else {}
    if (
        request_record.get("record_type") != PARTIAL_REQUEST_RECORD_TYPE
        or request_record.get("allow_partial_materials") is not True
        or not clean(request_record.get("request_text"))
        or clean(request_record.get("run_id")) != expected_run_id
    ):
        errors.append("partial_published_request_record_invalid")
    receipt = read_json(Path(clean(record.get("receipt"))), {}) if clean(record.get("receipt")) else {}
    if (
        receipt.get("record_type") != PARTIAL_RECEIPT_RECORD_TYPE
        or clean(receipt.get("run_id")) != expected_run_id
        or receipt.get("partial_materials_authorized") is not True
        or receipt.get("customer_requested") is not True
        or receipt.get("investigation_complete") is not False
        or receipt.get("delivery_authorized") is not False
        or receipt.get("completion_claim_allowed") is not False
        or receipt.get("validation", {}).get("ok") is not True
        or receipt.get("validation", {}).get("visible_partial_disclosures_absent") is not True
        or receipt.get("validation", {}).get("formal_delivery_authorized") is not False
    ):
        errors.append("partial_published_receipt_invalid")
    receipt_request = receipt.get("request") if isinstance(receipt.get("request"), dict) else {}
    receipt_delivery = receipt.get("deliverable") if isinstance(receipt.get("deliverable"), dict) else {}
    if (
        receipt_request.get("file") != PARTIAL_REQUEST_RECORD_NAME
        or clean(receipt_request.get("sha256")) != clean(record.get("request_sha256"))
        or clean(receipt_request.get("requested_at")) != clean(request_record.get("requested_at"))
        or receipt_delivery.get("pdf") != PARTIAL_PDF_NAME
        or clean(receipt_delivery.get("pdf_sha256")) != clean(record.get("pdf_sha256"))
        or receipt_delivery.get("manifest") != PARTIAL_MANIFEST_NAME
        or clean(receipt_delivery.get("manifest_sha256")) != clean(record.get("manifest_sha256"))
        or receipt_delivery.get("page_count") != record.get("page_count")
        or receipt_delivery.get("embedded_html_count") != record.get("embedded_html_count")
    ):
        errors.append("partial_published_receipt_chain_mismatch")
    for internal_name, hash_field in (
        (PARTIAL_RECEIPT_NAME, "receipt_sha256"),
        (PARTIAL_REQUEST_RECORD_NAME, "request_sha256"),
    ):
        internal = run_dir / internal_name
        if not internal.is_file() or file_sha256(internal) != clean(record.get(hash_field)):
            errors.append(f"partial_published_{internal_name}_not_bound_to_run_record")
    return errors, record


def transactional_publish_files(
    destination_root: Path,
    sources: dict[str, Path],
    destinations: dict[str, Path],
    expected_hashes: dict[str, str],
    *,
    post_commit=None,
) -> None:
    """Stage, verify and commit a related file set with rollback on failure."""
    if os.name == "nt" and str(destination_root).startswith("\\\\"):
        raise ValueError("publish output must be a local filesystem path, not UNC/network storage")
    staging = Path(tempfile.mkdtemp(prefix=".trademark-publish-", dir=destination_root))
    staged: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    committed: list[str] = []
    cleanup_staging = True
    try:
        for key, source in sources.items():
            target = staging / destinations[key].name
            shutil.copy2(source, target)
            if file_sha256(target) != clean(expected_hashes.get(key)).casefold():
                raise RuntimeError(f"publish staging hash mismatch: {key}")
            staged[key] = target
        backup_root = staging / "backup"
        backup_root.mkdir()
        try:
            for key, destination in destinations.items():
                if destination.exists():
                    backup = backup_root / destination.name
                    destination.replace(backup)
                    backups[key] = backup
            for key, destination in destinations.items():
                staged[key].replace(destination)
                committed.append(key)
            if post_commit is not None:
                post_commit()
        except Exception as original:
            rollback_errors = []
            for key in reversed(committed):
                try:
                    destinations[key].unlink(missing_ok=True)
                except OSError as remove_error:
                    rollback_errors.append(f"remove_new:{key}:{remove_error}")
            for key, backup in backups.items():
                if backup.exists():
                    try:
                        backup.replace(destinations[key])
                    except OSError as restore_error:
                        rollback_errors.append(f"restore_old:{key}:{restore_error}")
            if rollback_errors:
                cleanup_staging = False
                (staging / "RECOVERY_REQUIRED.json").write_text(json.dumps({
                    "schema_version": "1.0",
                    "record_type": "publish_restore_failure",
                    "destinations": {key: str(value) for key, value in destinations.items()},
                    "errors": rollback_errors,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                raise RuntimeError(
                    "publish rollback requires manual recovery from " + str(staging)
                ) from original
            raise
    finally:
        if cleanup_staging:
            shutil.rmtree(staging, ignore_errors=True)


def publish_delivery(
    run_dir: Path,
    output_dir: Path | None = None,
    *,
    verified_audit: dict | None = None,
) -> dict:
    """Publish only a physically verified all-detected HTML bundle."""
    run_dir = run_dir.resolve()
    try:
        with RunFileLock(run_dir, "cherrystudio-resume", timeout=1.0):
            return _publish_delivery_locked(run_dir, output_dir, verified_audit=verified_audit)
    except TimeoutError as exc:
        raise RuntimeError("publish refused: another publish is already running for this RUN") from exc


def _publish_delivery_locked(
    run_dir: Path,
    output_dir: Path | None = None,
    *,
    verified_audit: dict | None = None,
) -> dict:
    config = read_json(run_dir / "run-config.json", {}) or {}
    audit = verified_audit if verified_audit is not None else run_terminal_audit_step(run_dir)
    if audit.get("status") != "completed" or audit.get("validation", {}).get("ok") is not True:
        raise RuntimeError("publish refused: terminal audit is not complete")
    if (
        clean(audit.get("run_dir")) != str(run_dir)
        or clean(audit.get("run_id")) != clean(config.get("run_id"))
    ):
        raise RuntimeError("publish refused: terminal audit belongs to a different RUN")
    bundle = (audit.get("checks") or {}).get("all_detected_delivery_bundle") or {}
    if bundle.get("ok") is not True:
        raise RuntimeError("publish refused: all-detected bundle failed physical validation")
    receipt_path = run_dir / "cherrystudio-completion-receipt.json"
    receipt = read_json(receipt_path, {}) or {}
    receipt_errors, receipt_bundle = validate_receipt_bound_bundle(run_dir, receipt, config)
    if receipt_errors or receipt_bundle.get("ok") is not True:
        raise RuntimeError("publish refused: completion receipt hash validation failed")
    deliverable = receipt.get("deliverable") if isinstance(receipt.get("deliverable"), dict) else {}
    expected = {
        "pdf": "all-detected-html-pages.pdf",
        "pdf_sha256": bundle.get("pdf_sha256"),
        "manifest": "all-detected-html-pages.manifest.json",
        "manifest_sha256": bundle.get("manifest_sha256"),
        "page_count": bundle.get("page_count"),
        "embedded_html_count": bundle.get("embedded_html_count"),
    }
    if receipt.get("validation", {}).get("ok") is not True or any(
        deliverable.get(key) != value for key, value in expected.items()
    ):
        raise RuntimeError("publish refused: completion receipt is missing or does not bind the bundle")
    destination_root = (output_dir or (Path.home() / "Desktop")).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    registration = re.sub(
        r"[^A-Za-z0-9_-]", "", clean((config.get("trademark") or {}).get("registration_number"))
    ) or "unregistered"
    run_id = re.sub(r"[^A-Za-z0-9_-]", "-", clean(config.get("run_id") or run_dir.name))
    stem = f"商标调查HTML证据汇编-{registration}-{run_id}"
    source_pdf = run_dir / "all-detected-html-pages.pdf"
    source_manifest = run_dir / "all-detected-html-pages.manifest.json"
    destinations = {
        "pdf": destination_root / f"{stem}.pdf",
        "manifest": destination_root / f"{stem}.manifest.json",
        "completion_receipt": destination_root / f"{stem}.completion-receipt.json",
    }
    receipt_sha256 = file_sha256(receipt_path)
    record = {
        "schema_version": "1.0",
        "record_type": "cherrystudio_published_delivery",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "run_id": config.get("run_id") or run_dir.name,
        "delivery_authorized": True,
        "pdf": str(destinations["pdf"]),
        "pdf_sha256": bundle.get("pdf_sha256"),
        "manifest": str(destinations["manifest"]),
        "manifest_sha256": bundle.get("manifest_sha256"),
        "completion_receipt": str(destinations["completion_receipt"]),
        "completion_receipt_sha256": receipt_sha256,
        "page_count": bundle.get("page_count"),
        "embedded_html_count": bundle.get("embedded_html_count"),
        "timestamp_footer_pages": bundle.get("timestamp_footer_pages"),
        "source_url_footer_pages": bundle.get("source_url_footer_pages"),
        "uri_link_count": bundle.get("uri_link_count"),
    }
    record_path = run_dir / "cherrystudio-published-delivery.json"
    previous_record = record_path.read_bytes() if record_path.is_file() else None

    def commit_record() -> None:
        write_json(record_path, record)
        record_errors, _ = validate_formal_published_record(run_dir, config, bundle)
        if record_errors:
            raise RuntimeError(
                "publish failed: published record chain validation failed: "
                + ",".join(record_errors)
            )

    try:
        transactional_publish_files(
            destination_root,
            {"pdf": source_pdf, "manifest": source_manifest, "completion_receipt": receipt_path},
            destinations,
            {
                "pdf": clean(bundle.get("pdf_sha256")),
                "manifest": clean(bundle.get("manifest_sha256")),
                "completion_receipt": receipt_sha256,
            },
            post_commit=commit_record,
        )
    except Exception:
        if previous_record is None:
            record_path.unlink(missing_ok=True)
        else:
            temporary = record_path.with_suffix(record_path.suffix + ".restore")
            temporary.write_bytes(previous_record)
            temporary.replace(record_path)
        raise
    return record


def publish_partial_materials(
    run_dir: Path, request_json: Path, output_dir: Path | None = None,
) -> dict:
    """Publish accepted evidence from an incomplete RUN only after an explicit customer request."""
    run_dir = run_dir.resolve()
    try:
        with RunFileLock(run_dir, "cherrystudio-resume", timeout=1.0):
            return _publish_partial_materials_locked(run_dir, request_json, output_dir)
    except TimeoutError as exc:
        raise RuntimeError("publish-partial refused: another RUN mutation is already active") from exc


def _publish_partial_materials_locked(
    run_dir: Path, request_json: Path, output_dir: Path | None = None,
) -> dict:
    managed = [
        run_dir / PARTIAL_PDF_NAME,
        run_dir / PARTIAL_MANIFEST_NAME,
        run_dir / PARTIAL_RECEIPT_NAME,
        run_dir / PARTIAL_REQUEST_RECORD_NAME,
        run_dir / PARTIAL_PUBLISHED_RECORD_NAME,
    ]
    backup_root = Path(tempfile.mkdtemp(prefix=".partial-publish-backup-", dir=run_dir))
    backups: dict[Path, Path] = {}
    backup_phase_complete = False
    cleanup_backup = True
    try:
        for path in managed:
            if path.is_file():
                backup = backup_root / path.name
                path.replace(backup)
                backups[path] = backup
        backup_phase_complete = True
        return _publish_partial_materials_attempt(run_dir, request_json, output_dir)
    except Exception as original:
        restore_errors = []
        if backup_phase_complete:
            for path in managed:
                try:
                    path.unlink(missing_ok=True)
                except OSError as remove_error:
                    restore_errors.append(f"remove_new:{path.name}:{remove_error}")
        for path, backup in backups.items():
            if not backup.is_file():
                continue
            try:
                backup.replace(path)
            except OSError as restore_error:
                restore_errors.append(f"restore_old:{path.name}:{restore_error}")
        if restore_errors:
            cleanup_backup = False
            marker = backup_root / "RECOVERY_REQUIRED.json"
            marker.write_text(json.dumps({
                "schema_version": "1.0",
                "record_type": "partial_publish_restore_failure",
                "run_dir": str(run_dir),
                "errors": restore_errors,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError(
                "publish-partial rollback requires manual recovery from " + str(backup_root)
            ) from original
        raise
    finally:
        if cleanup_backup:
            shutil.rmtree(backup_root, ignore_errors=True)


def _publish_partial_materials_attempt(
    run_dir: Path, request_json: Path, output_dir: Path | None = None,
) -> dict:
    config = read_json(run_dir / "run-config.json", None)
    if not isinstance(config, dict):
        raise ValueError("publish-partial refused: run-config.json is missing or invalid")
    audit = run_terminal_audit_step(run_dir)
    if audit.get("status") == "completed" and audit.get("validation", {}).get("ok") is True:
        raise RuntimeError("publish-partial refused: the RUN is complete; use the formal publish action")
    qcc = validate_qcc_reference(run_dir)
    if qcc.get("ok") is not True:
        raise RuntimeError(
            "publish-partial refused: QCC visual reference validation failed: "
            + ",".join(qcc.get("errors") or ["unknown_qcc_error"])
        )
    normalized_request = validate_partial_materials_request(
        request_json, clean(config.get("run_id") or run_dir.name),
    )
    request_record_path = run_dir / PARTIAL_REQUEST_RECORD_NAME
    for stale in (
        run_dir / PARTIAL_PDF_NAME,
        run_dir / PARTIAL_MANIFEST_NAME,
        run_dir / PARTIAL_RECEIPT_NAME,
        run_dir / PARTIAL_PUBLISHED_RECORD_NAME,
    ):
        stale.unlink(missing_ok=True)
    write_json(request_record_path, normalized_request)

    sales_pages, search_pages = locked_pdf_page_budgets(config.get("cherrystudio_orchestration") or {})
    builder = Path(__file__).resolve().parent / "build-all-detected-html-pdf.py"
    step = run_step([
        sys.executable, str(builder), "--run-dir", str(run_dir),
        "--partial-customer-request", "--no-auto-assessment",
        "--max-sales-pages-per-platform", str(sales_pages),
        "--max-search-pages-per-provider", str(search_pages),
    ], timeout=PACKAGE_EXISTING_HARD_SEC)
    if step.get("return_code") != 0:
        raise RuntimeError(
            "publish-partial refused: canonical partial-material builder failed: "
            + clean(step.get("stderr_tail") or step.get("stdout_tail") or step.get("error"))
        )
    bundle_errors, bundle = validate_partial_materials_bundle(run_dir)
    if bundle_errors or bundle.get("ok") is not True:
        raise RuntimeError(
            "publish-partial refused: partial-material bundle failed physical validation: "
            + ",".join(bundle_errors or ["unknown_bundle_error"])
        )

    request_hash = file_sha256(request_record_path)
    receipt_path = run_dir / PARTIAL_RECEIPT_NAME
    receipt = {
        "schema_version": "1.0",
        "record_type": PARTIAL_RECEIPT_RECORD_TYPE,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_id": config.get("run_id") or run_dir.name,
        "partial_materials_authorized": True,
        "customer_requested": True,
        "investigation_complete": False,
        "delivery_authorized": False,
        "completion_claim_allowed": False,
        "request": {
            "file": PARTIAL_REQUEST_RECORD_NAME,
            "sha256": request_hash,
            "requested_at": normalized_request.get("requested_at"),
        },
        "deliverable": {
            "pdf": PARTIAL_PDF_NAME,
            "pdf_sha256": bundle.get("pdf_sha256"),
            "manifest": PARTIAL_MANIFEST_NAME,
            "manifest_sha256": bundle.get("manifest_sha256"),
            "page_count": bundle.get("page_count"),
            "embedded_html_count": bundle.get("embedded_html_count"),
        },
        "validation": {
            "ok": True,
            "physical_bundle_valid": True,
            "visible_partial_disclosures_absent": True,
            "blocked_or_failed_pages_included": 0,
            "formal_delivery_authorized": False,
        },
    }
    write_json(receipt_path, receipt)

    destination_root = (output_dir or (Path.home() / "Desktop")).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    registration = re.sub(
        r"[^A-Za-z0-9_-]", "", clean((config.get("trademark") or {}).get("registration_number"))
    ) or "unregistered"
    run_id = re.sub(r"[^A-Za-z0-9_-]", "-", clean(config.get("run_id") or run_dir.name))
    stem = f"商标调查部分材料-未完成-{registration}-{run_id}"
    destinations = {
        "pdf": destination_root / f"{stem}.pdf",
        "manifest": destination_root / f"{stem}.manifest.json",
        "receipt": destination_root / f"{stem}.receipt.json",
        "request_record": destination_root / f"{stem}.request.json",
    }
    sources = {
        "pdf": run_dir / PARTIAL_PDF_NAME,
        "manifest": run_dir / PARTIAL_MANIFEST_NAME,
        "receipt": receipt_path,
        "request_record": request_record_path,
    }
    expected_hashes = {
        "pdf": bundle.get("pdf_sha256"),
        "manifest": bundle.get("manifest_sha256"),
        "receipt": file_sha256(receipt_path),
        "request_record": request_hash,
    }
    hashes = {key: clean(value) for key, value in expected_hashes.items()}
    record = {
        "schema_version": "1.0",
        "record_type": PARTIAL_PUBLISHED_RECORD_TYPE,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "run_id": config.get("run_id") or run_dir.name,
        "partial_materials_authorized": True,
        "customer_requested": True,
        "investigation_complete": False,
        "delivery_authorized": False,
        "completion_claim_allowed": False,
        "pdf": str(destinations["pdf"]),
        "pdf_sha256": hashes["pdf"],
        "manifest": str(destinations["manifest"]),
        "manifest_sha256": hashes["manifest"],
        "receipt": str(destinations["receipt"]),
        "receipt_sha256": hashes["receipt"],
        "request_record": str(destinations["request_record"]),
        "request_sha256": hashes["request_record"],
        "page_count": bundle.get("page_count"),
        "embedded_html_count": bundle.get("embedded_html_count"),
        "timestamp_footer_pages": bundle.get("timestamp_footer_pages"),
        "source_url_footer_pages": bundle.get("source_url_footer_pages"),
        "visible_disclosure_pages": bundle.get("visible_disclosure_pages"),
        "uri_link_count": bundle.get("uri_link_count"),
        "task_coverage": bundle.get("task_coverage"),
    }
    def commit_partial_record() -> None:
        write_json(run_dir / PARTIAL_PUBLISHED_RECORD_NAME, record)
        published_errors, _published = validate_partial_published_record(run_dir)
        if published_errors:
            raise RuntimeError(
                "publish-partial failed: published record chain validation failed: "
                + ",".join(published_errors)
            )
        machine_state(run_dir, partial_materials_outputs=record)

    transactional_publish_files(
        destination_root, sources, destinations, hashes,
        post_commit=commit_partial_record,
    )
    return record


def finalize_run_state(run_dir: Path, config: dict, audit: dict, steps: list[dict]) -> tuple[dict, int]:
    published = None
    publish_error = None
    if audit.get("status") == "completed" and audit.get("validation", {}).get("ok") is True:
        try:
            published = _publish_delivery_locked(run_dir, verified_audit=audit)
        except Exception as error:
            publish_error = f"{type(error).__name__}: {error}"
    audit_complete = audit.get("status") == "completed" and audit.get("validation", {}).get("ok") is True
    audit_errors = list((audit.get("validation") or {}).get("errors") or [])
    if publish_error:
        phase = "publish_failed_resumable"
    elif audit_complete:
        phase = "terminal_audit_complete"
    elif "terminal_audit_timeout" in audit_errors:
        phase = "terminal_audit_timeout_resumable"
    else:
        phase = "terminal_audit_incomplete_resumable"
    state = machine_state(
        run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
        status=audit.get("status") or "incomplete", phase=phase,
        validation=audit.get("validation") or {"ok": False},
        may_claim_complete=False, terminal_audit="cherrystudio-terminal-audit.json",
        completion_receipt=(
            "cherrystudio-completion-receipt.json" if audit.get("status") == "completed" else None
        ),
        qcc_reference_status="validated", steps=steps,
        **({"published_delivery": published} if published else {}),
        **({
            "errors": ["automatic_publish_failed"],
            "next_action": "只执行本状态中的publish_command；不得自行创建、拼接或复制替代PDF",
            "publish_error": publish_error,
        } if publish_error else ({
            "errors": audit_errors or ["terminal_audit_incomplete"],
            "next_action": "再次原样执行resume；只重跑本地终审与发布，不重新访问网页",
            "resume_command": canonical_resume_spec(run_dir)[1],
        } if not audit_complete else {})),
    )
    return state, 0 if audit_complete and not publish_error else 3


def normalized_reference_file(intake: dict) -> str | None:
    direct = clean(intake.get("reference_file"))
    if direct:
        return direct
    attachments = intake.get("attachments") or []
    if isinstance(attachments, list):
        for value in attachments:
            candidate = clean(value.get("path") if isinstance(value, dict) else value)
            if candidate:
                return candidate
    return None


def start(intake_path: Path, *, prepare_only: bool, skip_qcc_fetch: bool) -> tuple[dict, int]:
    intake = read_external_json(intake_path)
    if not isinstance(intake, dict):
        raise ValueError("intake JSON must be a UTF-8 JSON object")
    workspace_value = clean(intake.get("workspace"))
    if not workspace_value:
        raise ValueError("intake workspace is required")
    workspace = resolve_external_path(workspace_value, "intake.workspace")
    if not workspace.is_dir():
        raise FileNotFoundError(f"intake.workspace directory does not exist: {workspace}")
    evidence_root = evidence_root_for_workspace(workspace)
    trademark_name = clean(intake.get("trademark_name") or intake.get("mark_name"))
    if not trademark_name:
        raise ValueError("intake trademark_name is required")
    goods = parse_goods(intake)
    validate_goods_against_request(intake, goods)
    workflow = select_workflow(intake, goods)
    if workflow != "free_assisted_browser":
        return ({
            "schema_version": "1.0",
            "record_type": "cherrystudio_trademark_machine_state",
            "workflow_mode": workflow,
            "status": "incomplete",
            "phase": "explicit_profile_not_supported_by_cherrystudio_entry",
            "validation": {"ok": False},
            "may_claim_complete": False,
            "errors": [f"explicit_{workflow}_requires_non_cherrystudio_operator"],
            "supported_cherrystudio_workflows": ["free_assisted_browser"],
            "next_action": "在CLI/Codex中执行该显式配置，或不指定Quick/Forensic以使用CherryStudio默认人工仅登录流程",
        }, 3)
    period, period_source = normalize_period(intake, workflow)
    scripts = Path(__file__).resolve().parent
    reference_value = normalized_reference_file(intake)
    reference_file = resolve_external_path(reference_value, "intake.reference_file") if reference_value else None
    if reference_file is not None and not reference_file.is_file():
        raise FileNotFoundError(f"Reference file not found: {reference_file}")
    if reference_file is None and workflow != "free_assisted_browser":
        raise ValueError("intake reference_file or attachments[0] is required outside free_assisted_browser")
    raw_request = intake.get("request_text") or intake.get("user_request") or ""
    qcc_brand_url_hint = clean(
        intake.get("qcc_brand_url") or intake.get("qcc_brand_detail_url") or intake.get("qcc_url")
        or qcc_brand_url_from_request(raw_request)
    )
    if qcc_brand_url_hint and not is_exact_qcc_brand_url(qcc_brand_url_hint):
        raise ValueError("intake qcc_brand_url must be an exact canonical QCC brandDetail URL")
    preflight = run_step([
        sys.executable, str(scripts / "preflight.py"),
        "--skill-dir", str(scripts.parent),
        "--run-root", str(evidence_root),
        "--profile", "assisted" if workflow == "free_assisted_browser" else "full",
    ], timeout=180)
    if preflight["return_code"] != 0:
        return ({
            "schema_version": "1.0",
            "record_type": "cherrystudio_trademark_machine_state",
            "status": "incomplete", "phase": "preflight_failed",
            "validation": {"ok": False}, "may_claim_complete": False,
            "errors": ["required_runtime_preflight_failed"],
            "preflight": preflight,
        }, 3)
    run_id = new_run_id(intake)
    run_dir = evidence_root / run_id
    normalized = {
        "profile": "forensic" if workflow == "forensic" else "quick",
        "workspace": str(workspace), "run_id": run_id,
        "reference_file": str(reference_file) if reference_file else None,
        "qcc_brand_url": qcc_brand_url_hint or None,
        "allow_missing_reference": reference_file is None,
        "trademark_name": trademark_name,
        "registration_number": clean(intake.get("registration_number")),
        "owner": clean(intake.get("owner")), "goods": goods,
        "period": period,
    }
    normalized_path = intake_path.with_name(f"{run_id}-normalized-intake.json")
    write_json(normalized_path, normalized)
    init = run_step([sys.executable, str(scripts / "init-run-from-json.py"), "--intake-json", str(normalized_path)])
    if init["return_code"] != 0:
        raise RuntimeError(init["stderr_tail"] or init["stdout_tail"] or "run initialization failed")
    if _ACTIVE_EXECUTION is not None:
        _ACTIVE_EXECUTION.attach_run_dir(run_dir)

    config_path = run_dir / "run-config.json"
    config = read_json(config_path, {}) or {}
    raw_outputs = intake.get("requested_outputs") or []
    if isinstance(raw_outputs, str):
        raw_outputs = [raw_outputs]
    outputs = set(clean(value) for value in raw_outputs if clean(value))
    if workflow == "free_assisted_browser":
        outputs.add("all_detected_html_pdf")
    request = clean(raw_request)
    if re.search(r"全部.*HTML|所有.*HTML|检测.*HTML.*PDF", request, re.I):
        outputs.add("all_detected_html_pdf")
    config["cherrystudio_orchestration"] = {
        "schema_version": "1.0", "entry_script": "scripts/cherrystudio_orchestrator.py",
        "workflow_mode": workflow, "cache_policy": "new_run_no_reuse",
        "source_intake_utf8": str(intake_path.resolve()),
        "workspace_input_raw": workspace_value,
        "workspace_input": str(workspace),
        "workspace_input_normalized": str(workspace),
        "evidence_root": str(evidence_root),
        "workspace_input_was_evidence_root": workspace.name.casefold() == "trademark-evidence",
        "profile_explicit": explicit_profile(intake) is not None,
        "requested_outputs": sorted(outputs),
        "qcc_brand_url_hint": qcc_brand_url_hint or None,
        "period_source": period_source,
        "pdf_page_budgets": {
            "max_sales_pages_per_platform": DEFAULT_SALES_PAGES_PER_PLATFORM,
            "max_search_pages_per_provider": DEFAULT_SEARCH_PAGES_PER_PROVIDER,
        },
        "terminal_audit_required": True,
        "narrative_report_authorized": False,
    }
    write_json(config_path, config)
    write_json(run_dir / "cherrystudio-intake.json", {
        **normalized, "request_text": request, "no_cache": no_cache_requested(intake),
        "source_intake": str(intake_path.resolve()),
    })

    steps = [preflight, init]
    if not goods or not normalized["registration_number"] or not normalized["owner"]:
        raise ValueError("free assisted-browser investigation requires registration_number, owner, and goods")
    try:
        selected_browser, browser_executable = preferred_chromium_browser()
    except FileNotFoundError:
        if not prepare_only:
            raise
        selected_browser, browser_executable = "edge", None
    browser_label = "Microsoft Edge" if selected_browser == "edge" else "Google Chrome"
    config["cherrystudio_orchestration"].update({
        "browser_selection_policy": "edge_then_chrome",
        "selected_browser": selected_browser,
        "browser_fallback_used": selected_browser == "chrome",
        "browser_detection_status": "available" if browser_executable else "not_available_prepare_only",
        "browser_executable": str(browser_executable) if browser_executable else None,
    })
    write_json(config_path, config)
    for script_name, extra in (
        ("build-query-plan.py", ["--scope", "sales-platforms", *sum((["--platform", p] for p in PLATFORMS), [])]),
        ("build-manual-capture-queue.py", sum((["--platform", p] for p in PLATFORMS), [])),
        ("build-sales-login-queue.py", ["--browser", "auto" if browser_executable else selected_browser, *sum((["--platform", p] for p in PLATFORMS), [])]),
    ):
        result = run_step([sys.executable, str(scripts / script_name), "--run-dir", str(run_dir), *extra])
        steps.append(result)
        if result["return_code"] != 0:
            state = machine_state(
                run_dir, run_id=run_id, workflow_mode=workflow, status="incomplete",
                phase="stage_a_failed", validation={"ok": False}, may_claim_complete=False,
                errors=[f"{script_name}_failed"], steps=steps,
            )
            return state, 3
    public_plan = build_public_plan(run_dir, config)

    qcc_status = "not_attempted"
    if not skip_qcc_fetch and qcc_brand_url_hint:
        qcc_step = run_step(
            qcc_fetch_command(scripts, run_dir, qcc_brand_url_hint), timeout=75,
        )
        steps.append(qcc_step)
        qcc_status = "validated" if qcc_step["return_code"] == 0 and validate_qcc_reference(run_dir).get("ok") else "pending_live_or_network_capture"
    elif not skip_qcc_fetch:
        # Assisted mode already opens the authoritative QCC search page and
        # resolves the exact brandDetail through the attached browser.  Public
        # index discovery here adds latency and can fail before Edge is opened.
        qcc_status = "pending_live_browser_resolution"

    if prepare_only:
        state = machine_state(
            run_dir, run_id=run_id, workflow_mode=workflow, status="incomplete",
            phase="prepared_awaiting_browser_launch", validation={"ok": False},
            may_claim_complete=False, cache_reused=False,
            sales_task_count=len(PLATFORMS) * (len(goods) + 1),
            public_task_count=public_plan["task_count"], qcc_reference_status=qcc_status,
            selected_browser=selected_browser, browser_fallback_used=selected_browser == "chrome",
            browser_detection_status="available" if browser_executable else "not_available_prepare_only",
            next_action="rerun_start_without_prepare_only_to_launch_dedicated_browser", steps=steps,
        )
        return state, 3

    launch = run_step([
        sys.executable, str(scripts / "open-sales-login-profile.py"),
        "--run-dir", str(run_dir), "--browser", "auto",
        "--browser-executable", str(browser_executable), "--handoff-mode", "attach",
        *sum((["--platform", p] for p in PLATFORMS), []),
    ], timeout=45, persistent_launcher=True)
    steps.append(launch)
    if launch["return_code"] != 0:
        failed_login_state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
        launch_phase = clean(failed_login_state.get("phase"))
        reconciliation_failed = launch_phase == "tab_reconciliation_failed"
        state = machine_state(
            run_dir, run_id=run_id, workflow_mode=workflow, status="incomplete",
            phase=launch_phase if reconciliation_failed else "browser_launch_failed",
            validation={"ok": False}, may_claim_complete=False,
            errors=["login_tab_reconciliation_failed" if reconciliation_failed else "dedicated_browser_launch_failed"],
            sales_task_count=len(PLATFORMS) * (len(goods) + 1),
            public_task_count=public_plan["task_count"], qcc_reference_status=qcc_status, steps=steps,
            **({
                "required_user_action": (
                    f"专用{browser_label}已保持打开，但标签页验收未通过；"
                    "不要执行resume，检查状态中的login_tab_reconciliation后重新启动新的RUN"
                ),
                "login_tab_reconciliation": failed_login_state.get("login_tab_reconciliation"),
            } if reconciliation_failed else {}),
        )
        return state, 3

    login_state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
    qcc_target_kind = clean(login_state.get("qcc_target_kind"))
    qcc_url = clean(login_state.get("qcc_url"))
    baidu_url = clean(login_state.get("baidu_url"))
    so360_url = clean(login_state.get("so360_url"))
    if not (
        login_state.get("qcc_opened") is True
        and qcc_target_kind in {"exact_brand_detail", "exact_brand_detail_candidate", "exact_brand_detail_hint", "trademark_search"}
        and qcc_url.startswith("https://www.qcc.com/")
    ):
        state = machine_state(
            run_dir, run_id=run_id, workflow_mode=workflow, status="incomplete",
            phase="stage_a_qcc_tab_missing", validation={"ok": False}, may_claim_complete=False,
            errors=["qcc_login_tab_was_not_opened_or_recorded"],
            selected_browser=selected_browser, browser_fallback_used=selected_browser == "chrome",
            qcc_opened=False, qcc_url=qcc_url or None, qcc_target_kind=qcc_target_kind or None,
            next_action="启动新的RUN；不得把缺少企查查标签页的阶段A报告为awaiting_manual_login",
            steps=steps,
        )
        return state, 3
    config["cherrystudio_orchestration"].update({
        "browser_user_data": login_state.get("browser_user_data"),
        "profile_directory": login_state.get("profile_directory"),
        "cdp_endpoint": login_state.get("cdp_endpoint"),
        "cdp_browser": login_state.get("cdp_browser"),
        "qcc_url": qcc_url,
        "qcc_target_kind": qcc_target_kind,
        "baidu_url": baidu_url,
        "so360_url": so360_url,
    })
    write_json(config_path, config)
    browser_lock = validate_locked_browser_state(
        run_dir, config, login_state, require_baidu=True, require_so360=True,
    )
    if not browser_lock["ok"]:
        state = machine_state(
            run_dir, run_id=run_id, workflow_mode=workflow, status="incomplete",
            phase="stage_a_browser_lock_invalid", validation={"ok": False}, may_claim_complete=False,
            errors=browser_lock["errors"], browser_state_validation=browser_lock, steps=steps,
        )
        return state, 3
    qcc_exact_visible = login_state.get("qcc_exact_detail_opened") is True
    if qcc_exact_visible:
        qcc_action = "企查查精确brandDetail页已实际打开，只需完成企查查登录或验证"
    elif qcc_target_kind in {"exact_brand_detail", "exact_brand_detail_candidate", "exact_brand_detail_hint"}:
        qcc_action = "企查查精确brandDetail链接已锁定，当前显示登录/验证页；完成后系统只接受该精确详情页"
    else:
        qcc_action = "企查查商标搜索页已实际打开；无需手工搜索，恢复后系统会自动定位并核验精确brandDetail"
    state = machine_state(
        run_dir, run_id=run_id, workflow_mode=workflow, status="awaiting_manual_login",
        phase="awaiting_manual_login", validation={"ok": False}, may_claim_complete=False,
        cache_reused=False, sales_task_count=len(PLATFORMS) * (len(goods) + 1),
        public_task_count=public_plan["task_count"], qcc_reference_status=qcc_status,
        selected_browser=selected_browser, browser_fallback_used=selected_browser == "chrome",
        qcc_opened=login_state.get("qcc_opened") is True,
        qcc_url=qcc_url, qcc_target_kind=qcc_target_kind,
        baidu_opened=login_state.get("baidu_opened") is True, baidu_url=baidu_url,
        so360_opened=login_state.get("so360_opened") is True, so360_url=so360_url,
        required_user_action=(
            f"在专用{browser_label}中完成{'、'.join(SALES_PLATFORM_LABELS[p] for p in PLATFORMS)}登录和验证；{qcc_action}；"
            "同时处理已打开的百度和360搜索预检页安全验证。保持窗口打开，然后回复“已登录并保持打开”"
        ),
        resume_command=canonical_resume_spec(run_dir)[1],
        steps=steps,
    )
    return state, 3


def ensure_live_qcc_import(run_dir: Path, scripts: Path) -> dict | None:
    metadata_path = run_dir / "reference" / "qcc-live-capture.json"
    html_path = run_dir / "reference" / "qcc-live-page.html"
    metadata = read_json(metadata_path, {}) or {}
    brand_url = clean(metadata.get("source_url"))
    image_rel = clean(metadata.get("image_file"))
    image_path = (run_dir / image_rel).resolve() if image_rel else None
    if not (
        brand_url and html_path.is_file() and image_path and image_path.is_file()
        and image_path.is_relative_to(run_dir) and metadata_path.is_file()
    ):
        return None
    return run_step([
        sys.executable, str(scripts / "fetch-qcc-trademark-reference.py"), "--run-dir", str(run_dir),
        "--brand-url", brand_url, "--html-file", str(html_path), "--image-file", str(image_path),
        "--metadata-file", str(metadata_path),
    ], timeout=90)


def _resume_locked(run_dir: Path) -> tuple[dict, int]:
    run_dir = run_dir.resolve()
    config = read_json(run_dir / "run-config.json", {}) or {}
    orchestration = config.get("cherrystudio_orchestration") or {}
    if orchestration.get("workflow_mode") != "free_assisted_browser":
        raise ValueError("resume is only valid for a free_assisted_browser run created by this entry")
    legacy_cooldown_migration = migrate_legacy_first_strike_cooldowns(run_dir)
    locked_sales_pages, locked_search_pages = locked_pdf_page_budgets(orchestration)
    state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
    state = reconcile_interrupted_running_phase(run_dir, state)
    resume_phase = clean(state.get("phase"))
    if resume_phase not in {
        "awaiting_manual_login", "public_search_verification_required",
        "public_search_internal_cooldown",
        "public_search_capture_retry_required", "public_search_technical_failure_resumable",
        "waiting_internal_cooldown", "sales_platform_resume_ready",
        "sales_platform_verification_required", "sales_platform_capture_retry_required",
        "sales_platform_verification_or_cooldown_required",
        "capture_failed", "capture_complete",
    }:
        raise ValueError(f"run is not in a resumable phase: {resume_phase!r}")
    pending_sales = state.get("sales_search_pending_platforms") or []
    sales_work = sales_pending_partition(run_dir, pending_sales)
    public_work = public_pending_partition(run_dir)
    sales_work, public_work, verification_probe = promote_manual_verification_for_explicit_resume(
        sales_work, public_work,
    )
    channel_decision = resume_channel_decision(sales_work, public_work)
    if not channel_decision["should_execute"]:
        selected_browser = clean(state.get("default_browser"))
        browser_label = "Google Chrome" if selected_browser == "chrome" else "Microsoft Edge"
        phase = channel_decision["phase"]
        if phase == "public_search_verification_required":
            required_action = "在专用浏览器中处理列出的公开搜索验证页并保持窗口打开"
            next_action = "验证后原样执行resume_command；冷却中的平台仍会跳过"
        elif phase == "sales_platform_verification_required":
            required_action, next_action = sales_cooldown_guidance(
                sales_work["cooldown"], browser_label, pending_sales,
            )
        else:
            if sales_work.get("cooling_pending"):
                required_action, next_action = sales_cooldown_guidance(
                    sales_work["cooldown"], browser_label, pending_sales,
                )
            else:
                required_action = None
                next_action = "公开搜索内部安全计时到期后原样执行resume_command"
            if public_work.get("cooling_pending"):
                next_action = "所有剩余查询均处于内部安全计时；到期后原样执行resume_command"
        result = machine_state(
            run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
            status="awaiting_manual_login" if "verification_required" in phase else "incomplete",
            phase=phase, validation={"ok": False}, may_claim_complete=False,
            pending_sales_platforms=pending_sales,
            sales_cooldown=sales_work["cooldown"],
            sales_pending_partition=sales_work,
            public_search_pending_partition=public_work,
            resume_channel_decision=channel_decision,
            explicit_resume_verification_probe=verification_probe,
            legacy_cooldown_migration=legacy_cooldown_migration,
            blocking_conditions=[{
                "type": "manual_verification" if "verification_required" in phase else "internal_safety_cooldown",
                "active_platforms": sales_work.get("cooling_platforms") or [],
                "active_public_providers": public_work.get("cooling_providers") or [],
                "automatic_resume": False,
            }],
            required_user_action=required_action, next_action=next_action,
            resume_command=canonical_resume_spec(run_dir)[1],
        )
        return result, 3
    preliminary_lock = validate_locked_browser_state(
        run_dir, config, state, require_baidu=True, require_so360=True,
    )
    recoverable_lock_errors = {
        "cdp_endpoint_mismatch", "cdp_browser_identity_mismatch",
        "qcc_url_mismatch", "qcc_target_kind_mismatch",
    }
    nonrecoverable_lock_errors = [
        value for value in preliminary_lock.get("errors") or []
        if value not in recoverable_lock_errors
    ]
    if nonrecoverable_lock_errors:
        result = machine_state(
            run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
            status="incomplete", phase="legacy_browser_policy_requires_fresh_run",
            validation={"ok": False}, may_claim_complete=False,
            errors=["legacy_run_has_no_complete_locked_browser_identity", *nonrecoverable_lock_errors],
            browser_state_validation=preliminary_lock,
            next_action="使用相同用户请求启动新的RUN；旧RUN不得在Chrome与Edge之间迁移登录态",
        )
        return result, 3

    selected_browser_before_lock = clean(state.get("default_browser"))
    if selected_browser_before_lock not in {"edge", "chrome"}:
        raise ValueError(f"invalid browser recorded in workflow state: {selected_browser_before_lock!r}")
    attachment_before_lock = resolve_live_browser_attachment(state, selected_browser_before_lock)
    browser_required_for_resume = resume_phase != "capture_complete"
    if attachment_before_lock.get("ok") is not True and browser_required_for_resume:
        browser_label = "Microsoft Edge" if selected_browser_before_lock == "edge" else "Google Chrome"
        reopen_argv, reopen_command = canonical_reopen_browser_spec(run_dir, selected_browser_before_lock)
        result = machine_state(
            run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
            status="incomplete", phase="browser_attachment_unavailable",
            validation={"ok": False}, may_claim_complete=False, delivery_authorized=False,
            browser_attachment_validation=attachment_before_lock,
            errors=["dedicated_browser_cdp_unavailable"],
            required_user_action=(
                f"专用{browser_label}已关闭或调试端口不可达；原样执行reopen_browser_command，"
                "确认登录状态并保持窗口打开"
            ),
            next_action="浏览器重新打开且页面正常后再执行新的resume_command；当前不得搜索",
            reopen_browser_argv=reopen_argv, reopen_browser_command=reopen_command,
        )
        return result, 3
    if attachment_before_lock.get("ok") is True:
        state.update({
            "cdp_endpoint": attachment_before_lock.get("endpoint") or state.get("cdp_endpoint"),
            "cdp_browser": attachment_before_lock.get("browser") or state.get("cdp_browser"),
        })
        if attachment_before_lock.get("process_id"):
            state["login_browser_pid"] = attachment_before_lock.get("process_id")
        stamp_validated_cdp_recovery(config, state, attachment_before_lock)
        write_json(run_dir / "discovery" / "sales-workflow-state.json", state)

    browser_lock = validate_locked_browser_state(
        run_dir, config, state, require_baidu=True, require_so360=True,
    )
    if not browser_lock["ok"]:
        result = machine_state(
            run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
            status="incomplete", phase="legacy_browser_policy_requires_fresh_run",
            validation={"ok": False}, may_claim_complete=False,
            errors=["legacy_run_has_no_complete_locked_browser_identity", *browser_lock["errors"]],
            browser_state_validation=browser_lock,
            next_action="使用相同用户请求启动新的RUN；旧RUN不得在Chrome与Edge之间迁移登录态",
        )
        return result, 3
    scripts = Path(__file__).resolve().parent
    steps = []
    selected_browser = clean(state.get("default_browser"))
    if selected_browser not in {"edge", "chrome"}:
        raise ValueError(f"invalid browser recorded in workflow state: {selected_browser!r}")
    browser_label = "Microsoft Edge" if selected_browser == "edge" else "Google Chrome"
    browser_attachment = (
        {"ok": True, "not_required": True}
        if resume_phase == "capture_complete"
        else attachment_before_lock
    )
    if browser_attachment.get("ok") is True and browser_attachment.get("endpoint_recovered") is True:
        state.update({
            "cdp_endpoint": browser_attachment.get("endpoint"),
            "login_browser_pid": browser_attachment.get("process_id"),
            "cdp_endpoint_recovered_at": datetime.now(timezone.utc).isoformat(),
        })
        write_json(run_dir / "discovery" / "sales-workflow-state.json", state)
    if browser_attachment.get("ok") is not True:
        reopen_argv, reopen_command = canonical_reopen_browser_spec(run_dir, selected_browser)
        result = machine_state(
            run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
            status="incomplete", phase="browser_attachment_unavailable",
            validation={"ok": False}, may_claim_complete=False,
            delivery_authorized=False, browser_state_validation=browser_lock,
            browser_attachment_validation=browser_attachment,
            errors=["dedicated_browser_cdp_unavailable"],
            required_user_action=(
                f"专用{browser_label}已关闭或调试端口不可达；原样执行reopen_browser_command，"
                "确认登录状态并保持窗口打开"
            ),
            next_action="浏览器重新打开且页面正常后再执行新的resume_command；当前不得搜索",
            reopen_browser_argv=reopen_argv, reopen_browser_command=reopen_command,
        )
        return result, 3
    machine_state(
        run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
        status="incomplete", phase="resuming_stage_b", validation={"ok": False},
        may_claim_complete=False, browser_state_validation=browser_lock,
        explicit_resume_verification_probe=verification_probe,
    )

    qcc = validate_qcc_reference(run_dir)
    if not qcc.get("ok"):
        cdp_endpoint = clean(state.get("cdp_endpoint"))
        trademark = config.get("trademark") or {}
        if cdp_endpoint:
            live_command = [
                "node", str(scripts / "capture-qcc-reference-from-cdp.mjs"),
                "--run-dir", str(run_dir), "--cdp-endpoint", cdp_endpoint,
                "--browser-product", selected_browser,
                "--registration-number", clean(trademark.get("registration_number")),
                "--mark-name", clean(trademark.get("name")),
                "--owner", clean(trademark.get("owner")),
            ]
            locked_qcc_url = clean(state.get("qcc_url"))
            known_brand_url = (
                locked_qcc_url if is_exact_qcc_brand_url(locked_qcc_url)
                else diagnostic_qcc_navigation_candidate(run_dir)
            )
            if known_brand_url:
                live_command.extend(["--brand-url", known_brand_url])
            live_capture = run_step(live_command, timeout=420)
            steps.append(live_capture)
        live = ensure_live_qcc_import(run_dir, scripts)
        if live:
            steps.append(live)
        qcc_brand_url_hint = clean(orchestration.get("qcc_brand_url_hint"))
        if (
            not validate_qcc_reference(run_dir).get("ok")
            and is_exact_qcc_brand_url(qcc_brand_url_hint)
        ):
            network = run_step(
                qcc_fetch_command(scripts, run_dir, qcc_brand_url_hint),
                timeout=75,
            )
            steps.append(network)
    qcc = validate_qcc_reference(run_dir)
    if not qcc.get("ok"):
        result = machine_state(
            run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
            status="incomplete", phase="qcc_reference_required", validation={"ok": False},
            may_claim_complete=False, qcc_reference_status=(
                "pending_live_or_network_capture"
                if is_exact_qcc_brand_url(clean(orchestration.get("qcc_brand_url_hint")))
                else "pending_live_browser_resolution"
            ),
            errors=[f"qcc:{value}" for value in qcc.get("errors") or []],
            next_action=(
                f"只在当前专用{browser_label}完成企查查登录或安全验证并保持窗口打开，然后重新resume；"
                "系统会自动检索、定位并核验准确的brandDetail，不需要人工搜索或选择商标"
            ),
            resume_command=canonical_resume_spec(run_dir)[1],
            steps=steps,
        )
        return result, 3

    qcc_record = read_json(run_dir / "reference" / "qcc-reference.json", {}) or {}
    state["qcc_reference_status"] = "validated"
    state["resolved_qcc_url"] = clean(qcc_record.get("source_url")) or None
    state["qcc_exact_detail_resolved"] = bool(state["resolved_qcc_url"])
    if state["qcc_exact_detail_resolved"] and is_exact_qcc_brand_url(state["resolved_qcc_url"]):
        previous_qcc_url = clean(state.get("qcc_url"))
        if previous_qcc_url:
            state.setdefault("qcc_initial_url", previous_qcc_url)
        state["qcc_url"] = state["resolved_qcc_url"]
        state["qcc_target_kind"] = "exact_brand_detail"
        qcc_live = live_exact_qcc_tab(
            browser_attachment.get("endpoint"), state["resolved_qcc_url"],
        )
        state["qcc_exact_detail_opened"] = qcc_live.get("confirmed") is True
        state["qcc_live_tab_validation"] = qcc_live
    write_json(run_dir / "discovery" / "sales-workflow-state.json", state)
    machine_state(
        run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
        status="incomplete", phase="resuming_stage_b", validation={"ok": False},
        may_claim_complete=False, browser_state_validation=browser_lock,
        qcc_reference_status="validated", resolved_qcc_url=state["resolved_qcc_url"],
        steps=steps,
    )

    if resume_phase == "capture_complete":
        package_command = [
            sys.executable, str(scripts / "run-visual-sales-after-login.py"),
            "--run-dir", str(run_dir), "--package-existing",
            "--max-sales-pages-per-platform", str(locked_sales_pages),
            "--max-search-pages-per-provider", str(locked_search_pages),
        ]
        if "all_detected_html_pdf" in set(orchestration.get("requested_outputs") or []):
            package_command.append("--all-html-pdf")
        package = run_step(package_command, timeout=PACKAGE_EXISTING_HARD_SEC)
        steps.append(package)
        if package["return_code"] != 0:
            result = machine_state(
                run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
                status="incomplete", phase="packaging_failed_resumable", validation={"ok": False},
                may_claim_complete=False, qcc_reference_status="validated",
                errors=["existing_capture_packaging_failed"],
                next_action="修复本地汇编依赖后再次resume；不会重跑网页搜索或详情抓取",
                resume_command=canonical_resume_spec(run_dir)[1],
                steps=steps,
            )
            return result, 3
        audit = run_terminal_audit_step(run_dir, steps)
        return finalize_run_state(run_dir, config, audit, steps)

    if resume_phase == "capture_failed":
        state["phase"] = "awaiting_manual_login"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["retry_from_phase"] = "capture_failed"
        write_json(run_dir / "discovery" / "sales-workflow-state.json", state)

    locked_profile_directory = clean(state.get("profile_directory"))
    if not locked_profile_directory:
        raise ValueError("Locked browser state is missing profile_directory")
    runnable_sales_platforms = list(sales_work.get("runnable_platforms") or [])
    command = [
        sys.executable, str(scripts / "run-visual-sales-after-login.py"),
        "--run-dir", str(run_dir), "--browser", selected_browser,
        "--profile-directory", locked_profile_directory,
        "--min-success-platforms", str(len(PLATFORMS)), "--allow-zero-results",
        "--max-sales-pages-per-platform", str(locked_sales_pages),
        "--max-search-pages-per-provider", str(locked_search_pages),
        *sales_resume_cli_args(sales_work),
    ]
    if not public_work.get("runnable_pending"):
        command.append("--skip-public-search")
    public_rate_state = read_json(run_dir / "discovery" / "public-search-rate-limit-state.json", {}) or {}
    for provider, provider_state in (public_rate_state.get("providers") or {}).items():
        if provider in PUBLIC_PROVIDERS and (provider_state or {}).get("circuit_open") is True:
            command.extend(["--resume-public-provider", provider])
    if "all_detected_html_pdf" in set(orchestration.get("requested_outputs") or []):
        command.append("--all-html-pdf")
    if resume_phase == "capture_failed":
        command.append("--skip-search")
    visual = run_step(command, timeout=RESUME_STAGE_B_HARD_SEC)
    steps.append(visual)
    if visual["return_code"] in {6, 7, 8, 9, 10}:
        latest_state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
        pending_public = latest_state.get("public_search_pending_providers") or []
        pending_public_tasks = latest_state.get("public_search_pending_tasks") or []
        pending_sales = latest_state.get("sales_search_pending_platforms") or []
        if visual["return_code"] == 6:
            pending_label = "、".join(
                PUBLIC_SEARCH_PROVIDER_LABELS.get(value, str(value)) for value in pending_public
            ) or "公开搜索页面"
            required_action = (
                f"在当前专用{browser_label}处理{pending_label}的安全验证并保持窗口打开；"
                "完成后回复继续，系统只恢复未完成的公开搜索任务"
            )
            next_action = "处理列出的公开搜索验证后，执行本状态中的resume_command；企查查参考已经校验，无需再次捕获"
        elif visual["return_code"] == 8:
            required_action = None
            next_action = (
                "无需人工验证；原样执行本状态中的resume_command，仅恢复列出的公开搜索技术失败任务，"
                "销售平台已完成的任务不重跑，未完成渠道按并行调度继续"
            )
        elif visual["return_code"] == 10:
            required_action = None
            next_action = public_search_cooldown_guidance(
                public_search_cooldown_snapshot(run_dir)
            )
        elif visual["return_code"] == 7:
            cooldown = sales_cooldown_snapshot(run_dir, pending_sales)
            required_action, next_action = sales_cooldown_guidance(
                cooldown, browser_label, pending_sales,
            )
            waiting_for_internal_cooldown = cooldown.get("active") is True
            verification_required = cooldown.get("verification_required") is True
        else:
            required_action = None
            next_action = (
                "无需重新登录或处理验证码；原样执行本状态中的resume_command，"
                "系统只重试未完成的销售平台技术失败任务"
            )
        result = machine_state(
            run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
            status=(
                "awaiting_manual_login" if visual["return_code"] == 7 and verification_required
                else "incomplete" if visual["return_code"] == 7 and waiting_for_internal_cooldown
                else "incomplete" if visual["return_code"] in {8, 9, 10}
                else "awaiting_manual_login"
            ), phase=(
                "public_search_verification_required" if visual["return_code"] == 6
                else "public_search_capture_retry_required" if visual["return_code"] == 8
                else "public_search_internal_cooldown" if visual["return_code"] == 10
                else "sales_platform_capture_retry_required" if visual["return_code"] == 9
                else "sales_platform_verification_required" if verification_required
                else "waiting_internal_cooldown" if waiting_for_internal_cooldown
                else "sales_platform_verification_required"
            ),
            validation={"ok": False}, may_claim_complete=False,
            qcc_reference_status="validated",
            pending_public_providers=pending_public, pending_public_tasks=pending_public_tasks,
            pending_sales_platforms=pending_sales,
            **({"sales_cooldown": cooldown} if visual["return_code"] == 7 else {}),
            **({
                "blocking_conditions": [{
                    "type": "internal_safety_cooldown",
                    "active_platforms": cooldown.get("active_platforms") or [],
                    "automatic_resume": False,
                }],
            } if visual["return_code"] == 7 and waiting_for_internal_cooldown else {}),
            **({"required_user_action": required_action} if required_action else {}),
            next_action=next_action,
            resume_command=canonical_resume_spec(run_dir)[1],
            steps=steps,
        )
        return result, 3
    if visual["return_code"] == 4:
        latest_state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
        result = machine_state(
            run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
            status="incomplete", phase="capture_failed_resumable", validation={"ok": False},
            may_claim_complete=False, qcc_reference_status="validated",
            capture_status=latest_state.get("capture_status") or "failed",
            errors=["direct_page_capture_incomplete"],
            next_action=(
                "保持专用浏览器打开并原样执行resume；系统只重试详情抓取，"
                "不会重跑已完成的公开搜索和销售平台搜索"
            ),
            resume_command=canonical_resume_spec(run_dir)[1], steps=steps,
        )
        return result, 3
    if visual["return_code"] != 0:
        latest_state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
        latest_phase = clean(latest_state.get("phase"))
        recoverable_phases = {
            "public_search_capture_retry_required",
            "public_search_technical_failure_resumable",
            "sales_platform_capture_retry_required",
            "capture_failed", "capture_complete",
        }
        if visual["return_code"] == 124 or latest_phase in recoverable_phases:
            phase = (
                latest_phase if latest_phase in recoverable_phases
                else "stage_b_timeout_resumable"
            )
            result = machine_state(
                run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
                status="incomplete", phase=phase, validation={"ok": False},
                may_claim_complete=False, qcc_reference_status="validated",
                errors=[
                    "stage_b_wall_timeout" if visual["return_code"] == 124
                    else "recoverable_stage_b_worker_failure"
                ],
                next_action=(
                    "原样执行resume_command；系统从已校验的物理检查点恢复，不重跑已完成网页任务"
                ),
                resume_command=canonical_resume_spec(run_dir)[1], steps=steps,
            )
            return result, 3
        result = machine_state(
            run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
            status="incomplete", phase="stage_b_failed", validation={"ok": False},
            may_claim_complete=False, qcc_reference_status="validated",
            errors=["visual_sales_workflow_failed"], steps=steps,
        )
        return result, 3
    audit = run_terminal_audit_step(run_dir, steps)
    return finalize_run_state(run_dir, config, audit, steps)


def resume(run_dir: Path, *, emit_progress: bool = False) -> tuple[dict, int]:
    global _ACTIVE_EXECUTION
    run_dir = run_dir.resolve()
    try:
        with RunFileLock(run_dir, "cherrystudio-resume", timeout=1.0):
            with ExecutionHeartbeat(
                run_dir, action="resume", emit_progress=emit_progress,
            ) as execution:
                _ACTIVE_EXECUTION = execution
                try:
                    return _resume_locked(run_dir)
                finally:
                    _ACTIVE_EXECUTION = None
    except TimeoutError:
        config = read_json(run_dir / "run-config.json", {}) or {}
        next_action = (
            "后台已有同一RUN的恢复执行；本回合不得再次TaskOutput或resume；"
            "只报告当前状态并结束本回合"
        )
        result = machine_state(
            run_dir, run_id=config.get("run_id"), workflow_mode="free_assisted_browser",
            status="incomplete", phase="resume_already_running", validation={"ok": False},
            may_claim_complete=False, errors=["concurrent_resume_rejected"],
            next_action=next_action,
        )
        result = apply_status_return_now_contract(result, next_action)
        write_json(run_dir / "cherrystudio-machine-state.json", result)
        return result, 3


def status(run_dir: Path) -> tuple[dict, int]:
    """Reconcile the user-visible state from existing artifacts without I/O to websites."""
    run_dir = run_dir.resolve()
    config = read_json(run_dir / "run-config.json", {}) or {}
    orchestration = config.get("cherrystudio_orchestration") or {}
    if orchestration.get("workflow_mode") != "free_assisted_browser":
        raise ValueError("status is only valid for a free_assisted_browser run created by this entry")
    legacy_cooldown_migration = migrate_legacy_first_strike_cooldowns(run_dir)
    current = read_json(run_dir / "cherrystudio-machine-state.json", {}) or {}
    execution = read_json(run_dir / "cherrystudio-execution-state.json", {}) or {}
    execution, execution_liveness = reconcile_execution_liveness(run_dir, execution)
    if (
        execution_liveness.get("execution_state") == "running"
        and execution_liveness.get("pid_alive") is True
        and execution_liveness.get("heartbeat_fresh") is not True
    ):
        progress = workflow_progress(run_dir)
        live = progress.get("sales_search_live_progress") or {}
        active_task = live.get("active_task") if isinstance(live.get("active_task"), dict) else None
        owner_pid = execution_liveness.get("orchestrator_pid")
        next_action = (
            f"旧编排器PID {owner_pid}仍存活但执行心跳已过期；RunFileLock可能仍由该进程持有。"
            "本回合不得再次TaskOutput或resume，也不得进入离线恢复；"
            "只报告技术停滞和所需人工终止动作并结束本回合。"
            "核对并精确终止该PID及其受管子进程树后，由用户在新回合只运行一次status；"
            "只有status确认旧PID已死并落盘interrupted后，才能按新的机器状态继续"
        )
        stalled_state = {
            **current,
            **progress,
            "schema_version": "1.0",
            "record_type": "cherrystudio_trademark_machine_state",
            "run_dir": str(run_dir),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": "incomplete",
            "phase": "technical_execution_stalled",
            "validation": {"ok": False},
            "may_claim_complete": False,
            "delivery_authorized": False,
            "completion_claim_allowed": False,
            "active_execution": {**execution, "pid_alive": True},
            "execution_liveness": execution_liveness,
            "active_search_task": active_task,
            "active_search_stage": live.get("stage") or None,
            "active_search_stage_deadline_at": live.get("stage_deadline_at") or None,
            "technical_execution_stalled": True,
            "technical_capture_stalled": False,
            "resume_blocked_while_pid_alive": True,
            "errors": ["orchestrator_stalled_pid_alive"],
            "stalled_execution_termination": {
                "required_before_recovery": True,
                "orchestrator_pid": owner_pid,
                "scope": "exact_orchestrator_pid_and_owned_descendants",
                "pid_identity_check_required": True,
                "status_after_termination_required": True,
                "resume_before_status_forbidden": True,
            },
            "required_user_action": (
                f"核对并精确终止本RUN的旧编排器PID {owner_pid}及其受管子进程树"
            ),
            "next_action": next_action,
            "operator_message": next_action,
        }
        for key in (
            "resume_command", "resume_argv", "resume_commands", "resume_contract",
            "reopen_browser_command", "reopen_browser_argv",
            "publish_command", "publish_argv", "publish_commands",
        ):
            stalled_state.pop(key, None)
        previous_contract = (
            current.get("execution_contract")
            if isinstance(current.get("execution_contract"), dict) else {}
        )
        stalled_state["execution_contract"] = {
            **previous_contract,
            **exhausted_managed_task_output_contract(),
            "duplicate_resume_while_active_forbidden": True,
            "resume_blocked_while_pid_alive": True,
            "status_is_local_only": True,
            "instruction": next_action,
        }
        stalled_state = apply_status_return_now_contract(stalled_state, next_action)
        write_json(run_dir / "cherrystudio-machine-state.json", stalled_state)
        return stalled_state, 0
    if execution_liveness["considered_running"]:
        progress = workflow_progress(run_dir)
        live = progress.get("sales_search_live_progress") or {}
        now_utc = datetime.now(timezone.utc)
        live_updated_at = parse_utc_timestamp(live.get("updated_at"))
        live_deadline_at = parse_utc_timestamp(live.get("stage_deadline_at"))
        live_age_seconds = (
            max(0, math.ceil((now_utc - live_updated_at).total_seconds()))
            if live_updated_at else None
        )
        fallback_stall_seconds = max(
            120,
            int((RUNTIME_POLICY.get("concurrency") or {}).get("artifact_lock_timeout_sec") or 180)
            + PROGRESS_STALL_GRACE_SEC,
        )
        live_is_active = clean(live.get("state")) in {
            "starting", "running", "waiting_rate_limit", "waiting_artifact_lock",
        }
        live_stalled = bool(
            live_is_active and (
                (live_deadline_at and now_utc > live_deadline_at)
                or (
                    not live_deadline_at and live_age_seconds is not None
                    and live_age_seconds > fallback_stall_seconds
                )
            )
        )
        active_task = live.get("active_task") if isinstance(live.get("active_task"), dict) else None
        if live_stalled:
            phase = "technical_capture_stalled"
            next_action = (
                "当前浏览器工件操作已超过本地有界截止时间；这是技术停滞，不是平台冻结。"
                "后台官方命令可能仍在收尾并释放工件锁；本回合不得再次TaskOutput或resume，"
                "只报告当前技术停滞状态并结束本回合"
            )
        elif clean(live.get("state")) == "failed":
            phase = "search_channel_finishing_after_failure"
            next_action = (
                "销售搜索子进程已记录技术失败，后台官方命令继续收尾；"
                "本回合不得再次TaskOutput或resume，只报告当前状态并结束本回合"
            )
        else:
            phase = "resume_running"
            if active_task:
                next_action = (
                    f"当前正在处理{active_task.get('platform') or '销售平台'}的"
                    f"{active_task.get('query_id') or '查询'}，阶段为{live.get('stage') or 'running'}；"
                    "后台任务继续运行，本回合不得再次TaskOutput或resume；"
                    "只报告当前阶段并结束本回合"
                )
            else:
                next_action = (
                    "当前resume仍在后台运行；本回合不得再次TaskOutput或resume，"
                    "只报告当前阶段并结束本回合"
                )
        previous_contract = (
            current.get("execution_contract")
            if isinstance(current.get("execution_contract"), dict) else {}
        )
        active_state = {
            **current,
            **progress,
            "status": "incomplete",
            "phase": phase,
            "may_claim_complete": False,
            "delivery_authorized": False,
            "active_execution": {**execution, "pid_alive": True},
            "execution_contract": {
                **previous_contract,
                **exhausted_managed_task_output_contract(),
            },
            "execution_liveness": execution_liveness,
            "active_search_task": active_task,
            "active_search_stage": live.get("stage") or None,
            "active_search_progress_age_seconds": live_age_seconds,
            "active_search_stage_deadline_at": live.get("stage_deadline_at") or None,
            "technical_capture_stalled": live_stalled,
            "next_action": next_action,
            "operator_message": next_action,
        }
        return apply_status_return_now_contract(active_state, next_action), 0
    workflow_state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
    workflow_state = reconcile_interrupted_running_phase(run_dir, workflow_state)
    qcc = validate_qcc_reference(run_dir)
    selected_browser = clean(workflow_state.get("default_browser") or orchestration.get("selected_browser"))
    browser_label = "Google Chrome" if selected_browser == "chrome" else "Microsoft Edge"
    pending_public = current.get("pending_public_providers") or workflow_state.get("public_search_pending_providers") or []
    pending_public_tasks = current.get("pending_public_tasks") or workflow_state.get("public_search_pending_tasks") or []
    pending_sales = current.get("pending_sales_platforms") or workflow_state.get("sales_search_pending_platforms") or []
    sales_work = sales_pending_partition(run_dir, pending_sales)
    public_work = public_pending_partition(run_dir)
    channel_decision = resume_channel_decision(sales_work, public_work)
    current_phase = clean(current.get("phase"))
    phase = current_phase or clean(workflow_state.get("phase")) or "incomplete"
    if current_phase in {
        "resuming_stage_b", "resume_running",
        "stage_b_public_search_running", "stage_b_sales_search_running",
        "stage_b_parallel_search_running", "stage_b_parallel_search_evaluating",
        "stage_b_capture_and_packaging_running",
    }:
        phase = clean(workflow_state.get("phase")) or phase
    if phase == "browser_attachment_unavailable":
        phase = clean(workflow_state.get("phase")) or phase
    factual_progress = workflow_progress(run_dir)
    if (
        phase == "public_search_verification_required"
        and factual_progress.get("public_search_status") == "capture_retry_required"
    ):
        phase = "public_search_capture_retry_required"
    state_status = clean(current.get("status"))
    if state_status not in {"completed", "incomplete", "awaiting_manual_login"}:
        state_status = "incomplete"
    current_validation = current.get("validation")
    if not isinstance(current_validation, dict):
        current_validation = {"ok": False}
    updates = {
        "run_id": config.get("run_id"),
        "workflow_mode": "free_assisted_browser",
        "status": state_status,
        "phase": phase,
        "validation": current_validation,
        "may_claim_complete": bool(current.get("may_claim_complete") and current_validation.get("ok")),
        "pending_public_providers": pending_public,
        "pending_public_tasks": pending_public_tasks,
        "pending_sales_platforms": pending_sales,
        "sales_pending_partition": sales_work,
        "public_search_pending_partition": public_work,
        "resume_channel_decision": channel_decision,
        "legacy_cooldown_migration": legacy_cooldown_migration,
        "execution_liveness": execution_liveness,
        **({"last_execution": execution} if execution else {}),
        **factual_progress,
    }
    channel_managed_phases = {
        "public_search_verification_required", "public_search_internal_cooldown",
        "public_search_capture_retry_required", "public_search_technical_failure_resumable",
        "sales_platform_verification_or_cooldown_required", "waiting_internal_cooldown",
        "sales_platform_verification_required", "sales_platform_resume_ready",
        "sales_platform_capture_retry_required",
    }
    if phase in channel_managed_phases:
        phase = channel_decision["phase"]
        updates["phase"] = phase
    if not qcc.get("ok"):
        updates.update({
            "status": "incomplete",
            "phase": "qcc_reference_required",
            "validation": {"ok": False},
            "may_claim_complete": False,
            "qcc_reference_status": "pending_live_or_network_capture",
            "errors": [f"qcc:{value}" for value in qcc.get("errors") or []],
            "required_user_action": f"在当前专用{browser_label}完成企查查登录或安全验证并保持窗口打开",
            "next_action": "随后执行本状态中的resume_command；不得手工创建企查查参考记录",
        })
    else:
        qcc_record = read_json(run_dir / "reference" / "qcc-reference.json", {}) or {}
        resolved_qcc_url = clean(qcc_record.get("source_url")) or None
        if resolved_qcc_url and is_exact_qcc_brand_url(resolved_qcc_url):
            previous_qcc_url = clean(workflow_state.get("qcc_url"))
            if previous_qcc_url:
                workflow_state.setdefault("qcc_initial_url", previous_qcc_url)
            workflow_state.update({
                "qcc_url": resolved_qcc_url,
                "resolved_qcc_url": resolved_qcc_url,
                "qcc_target_kind": "exact_brand_detail",
                "qcc_exact_detail_resolved": True,
            })
            write_json(run_dir / "discovery" / "sales-workflow-state.json", workflow_state)
        remaining_errors = [
            value for value in current.get("errors") or []
            if not str(value).startswith("qcc:")
        ]
        updates.update({
            "qcc_reference_status": "validated",
            "resolved_qcc_url": resolved_qcc_url,
            **({"errors": remaining_errors} if remaining_errors else {}),
        })
        if phase == "awaiting_manual_login":
            reconciliation = workflow_state.get("login_tab_reconciliation") or {}
            satisfaction = reconciliation.get("satisfaction") or {}
            pending_kinds = [
                kind for kind, value in satisfaction.items()
                if value == "pending_verification"
            ]
            labels = {
                "qcc": "企查查",
                **PUBLIC_SEARCH_PROVIDER_LABELS,
                **SALES_PLATFORM_LABELS,
            }
            pending_label = "、".join(labels.get(value, value) for value in pending_kinds)
            sales_login_label = "、".join(SALES_PLATFORM_LABELS[p] for p in PLATFORMS)
            action = (
                f"在当前专用{browser_label}处理实际保留的{pending_label}验证页，"
                f"并确认{sales_login_label}登录状态后保持窗口打开"
                if pending_label else
                f"在当前专用{browser_label}确认{sales_login_label}登录状态并保持窗口打开"
            )
            updates.update({
                "status": "awaiting_manual_login",
                "login_tab_reconciliation": reconciliation,
                "required_user_action": action,
                "next_action": "完成实际显示的登录或验证后执行本状态中的resume_command",
            })
        elif phase == "public_search_verification_required":
            pending_label = "、".join(
                PUBLIC_SEARCH_PROVIDER_LABELS.get(value, str(value)) for value in pending_public
            ) or "公开搜索页面"
            updates.update({
                "status": "awaiting_manual_login",
                "required_user_action": f"在当前专用{browser_label}处理{pending_label}的安全验证并保持窗口打开",
                "next_action": "验证后执行本状态中的resume_command；企查查参考已经校验，无需再次捕获",
            })
        elif phase == "public_search_internal_cooldown":
            public_cooldown = public_search_cooldown_snapshot(run_dir)
            updates.update({
                "status": "incomplete",
                "phase": (
                    "public_search_internal_cooldown" if public_cooldown.get("active") is True
                    else "public_search_capture_retry_required"
                ),
                "public_search_cooldown": public_cooldown,
                "platform_freeze_observed": False,
                "platform_account_status": "not_determined",
                "required_user_action": None,
                "next_action": public_search_cooldown_guidance(public_cooldown),
            })
        elif phase in {"public_search_capture_retry_required", "public_search_technical_failure_resumable"}:
            updates.update({
                "status": "incomplete",
                "phase": "public_search_capture_retry_required",
                "next_action": (
                    "无需人工验证；原样执行本状态中的resume_command，仅恢复公开搜索技术失败任务，"
                    "销售渠道按自身物理检查点独立继续，已完成任务不重跑"
                ),
            })
        elif phase in {
            "sales_platform_verification_or_cooldown_required",
            "waiting_internal_cooldown",
            "sales_platform_verification_required",
            "sales_platform_resume_ready",
        }:
            cooldown = sales_work["cooldown"]
            runnable = channel_decision.get("runnable_pending_count", 0) > 0
            waiting_for_internal_cooldown = (
                not runnable and channel_decision.get("phase") == "waiting_internal_cooldown"
            )
            verification_required = (
                not runnable and channel_decision.get("phase") == "sales_platform_verification_required"
            )
            if runnable:
                runnable_labels = [
                    *(SALES_PLATFORM_LABELS.get(value, value) for value in channel_decision.get("runnable_sales_platforms") or []),
                    *(PUBLIC_SEARCH_PROVIDER_LABELS.get(value, value) for value in channel_decision.get("runnable_public_providers") or []),
                ]
                required_action = None
                next_action = (
                    f"原样执行resume_command；本次只运行{'、'.join(runnable_labels)}的未完成任务，"
                    "冷却或等待人工验证的渠道会跳过"
                )
            else:
                if sales_work.get("cooling_pending") or sales_work.get("verification_pending"):
                    required_action, next_action = sales_cooldown_guidance(
                        cooldown, browser_label, pending_sales,
                    )
                else:
                    required_action = None
                    next_action = "公开搜索内部安全计时到期后原样执行resume_command"
            updates.update({
                "status": "awaiting_manual_login" if verification_required else "incomplete",
                "phase": (
                    "sales_platform_verification_required" if verification_required
                    else "waiting_internal_cooldown" if waiting_for_internal_cooldown
                    else "sales_platform_resume_ready"
                ),
                "sales_cooldown": cooldown,
                **({
                    "blocking_conditions": [{
                        "type": "internal_safety_cooldown",
                        "active_platforms": cooldown.get("active_platforms") or [],
                        "automatic_resume": False,
                    }],
                } if waiting_for_internal_cooldown else {}),
                "required_user_action": required_action,
                "next_action": next_action,
            })
        elif phase == "sales_platform_capture_retry_required":
            updates.update({
                "status": "incomplete",
                "phase": "sales_platform_capture_retry_required",
                "required_user_action": None,
                "next_action": (
                    "无需重新登录或处理验证码；原样执行本状态中的resume_command，"
                    "系统只重试未完成的销售平台技术失败任务"
                ),
            })
    browser_required_phases = {
        "awaiting_manual_login", "qcc_reference_required",
        "public_search_verification_required", "public_search_capture_retry_required",
        "public_search_technical_failure_resumable", "sales_platform_resume_ready",
        "sales_platform_verification_required", "sales_platform_capture_retry_required",
        "capture_failed", "capture_failed_resumable", "browser_attachment_unavailable",
    }
    if updates.get("phase") in browser_required_phases:
        attachment = resolve_live_browser_attachment(workflow_state, selected_browser)
        updates["browser_attachment_validation"] = attachment
        if attachment.get("ok") is True:
            qcc_live = live_exact_qcc_tab(
                attachment.get("endpoint"),
                updates.get("resolved_qcc_url") or workflow_state.get("resolved_qcc_url")
                or workflow_state.get("qcc_url"),
            )
            updates["qcc_live_tab_validation"] = qcc_live
            updates["qcc_exact_detail_opened"] = qcc_live.get("confirmed") is True
            if updates.get("phase") == "sales_platform_verification_required":
                live_verification = live_sales_verification_tabs(
                    attachment.get("endpoint"), pending_sales,
                )
                updates["sales_live_verification_tabs"] = live_verification
                visible = live_verification.get("visible_platforms") or []
                if not visible:
                    updates.update({
                        "status": "incomplete",
                        "phase": "sales_platform_resume_ready",
                        "required_user_action": None,
                        "next_action": (
                            "当前专用浏览器中没有可见的销售平台验证码/登录确认页；不能声称页面已保留。"
                            "现在可原样执行resume_command，对未完成任务做一次有界重试。"
                        ),
                    })
                else:
                    visible_labels = "、".join(
                        SALES_PLATFORM_LABELS.get(value, value) for value in visible
                    )
                    updates["required_user_action"] = (
                        f"专用{browser_label}中实际可见{visible_labels}验证页，请人工完成并保持窗口打开。"
                        "验证码不代表账号被冻结，也没有平台倒计时。"
                    )
            state_changed = False
            if attachment.get("endpoint_recovered") is True:
                workflow_state.update({
                    "cdp_endpoint": attachment.get("endpoint"),
                    "cdp_browser": attachment.get("browser") or workflow_state.get("cdp_browser"),
                    "login_browser_pid": attachment.get("process_id"),
                    "cdp_endpoint_recovered_at": datetime.now(timezone.utc).isoformat(),
                })
                state_changed = True
            if stamp_validated_cdp_recovery(config, workflow_state, attachment):
                state_changed = True
            if state_changed:
                write_json(run_dir / "discovery" / "sales-workflow-state.json", workflow_state)
            remaining = [
                value for value in updates.get("errors") or []
                if value != "dedicated_browser_cdp_unavailable"
            ]
            if remaining:
                updates["errors"] = remaining
            else:
                updates.pop("errors", None)
        else:
            reopen_argv, reopen_command = canonical_reopen_browser_spec(run_dir, selected_browser)
            updates.update({
                "status": "incomplete",
                "phase": "browser_attachment_unavailable",
                "validation": {"ok": False},
                "may_claim_complete": False,
                "delivery_authorized": False,
                "errors": ["dedicated_browser_cdp_unavailable"],
                "required_user_action": (
                    f"专用{browser_label}已关闭或调试端口不可达；原样执行reopen_browser_command，"
                    "确认登录状态并保持窗口打开"
                ),
                "next_action": "浏览器重新打开且页面正常后再执行新的resume_command；当前不得搜索",
                "reopen_browser_argv": reopen_argv,
                "reopen_browser_command": reopen_command,
            })
    if updates["status"] != "completed" and updates.get("phase") != "browser_attachment_unavailable":
        updates["resume_command"] = canonical_resume_spec(run_dir)[1]
    reconciled = machine_state(run_dir, **updates)
    return apply_status_return_now_contract(reconciled), 0


def main() -> None:
    global _ACTIVE_EXECUTION
    parser = argparse.ArgumentParser(description="CherryStudio hard-gated trademark investigation orchestrator")
    sub = parser.add_subparsers(dest="action", required=True)
    start_parser = sub.add_parser("start")
    start_parser.add_argument("--intake-json", required=True)
    start_parser.add_argument("--prepare-only", action="store_true", help="Build plans but do not launch Chrome; tests/offline setup")
    start_parser.add_argument("--skip-qcc-fetch", action="store_true", help="Skip network QCC preflight; tests/offline setup")
    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("--run-dir", required=True)
    status_parser = sub.add_parser("status", help="Reconcile machine state without accessing websites")
    status_parser.add_argument("--run-dir", required=True)
    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("--run-dir", required=True)
    doctor_parser = sub.add_parser("doctor", help="Run an offline CherryStudio runtime acceptance probe")
    doctor_parser.add_argument("--run-root", required=True)
    publish_parser = sub.add_parser("publish", help="Publish the physically verified HTML evidence bundle")
    publish_parser.add_argument("--run-dir", required=True)
    publish_parser.add_argument("--output-dir")
    partial_parser = sub.add_parser(
        "publish-partial",
        help="Publish a visibly incomplete partial-material packet after an explicit customer request",
    )
    partial_parser.add_argument("--run-dir", required=True)
    partial_parser.add_argument("--request-json", required=True)
    partial_parser.add_argument("--output-dir")
    args = parser.parse_args()

    try:
        if args.action == "start":
            with ExecutionHeartbeat(
                None, action="start", emit_progress=True,
            ) as execution:
                _ACTIVE_EXECUTION = execution
                try:
                    result, code = start(
                        resolve_external_path(args.intake_json, "--intake-json"),
                        prepare_only=args.prepare_only,
                        skip_qcc_fetch=args.skip_qcc_fetch,
                    )
                finally:
                    _ACTIVE_EXECUTION = None
        elif args.action == "resume":
            result, code = resume(
                resolve_external_path(args.run_dir, "--run-dir"), emit_progress=True,
            )
        elif args.action == "status":
            result, code = status(resolve_external_path(args.run_dir, "--run-dir"))
        elif args.action == "doctor":
            result, code = run_cherrystudio_doctor(
                resolve_external_path(args.run_root, "--run-root")
            )
        elif args.action == "publish":
            output_dir = resolve_external_path(args.output_dir, "--output-dir") if args.output_dir else None
            result = publish_delivery(resolve_external_path(args.run_dir, "--run-dir"), output_dir)
            code = 0
        elif args.action == "publish-partial":
            output_dir = resolve_external_path(args.output_dir, "--output-dir") if args.output_dir else None
            result = publish_partial_materials(
                resolve_external_path(args.run_dir, "--run-dir"),
                resolve_external_path(args.request_json, "--request-json"),
                output_dir,
            )
            code = 0
        else:
            result = run_terminal_audit_step(resolve_external_path(args.run_dir, "--run-dir"))
            code = 0 if result["status"] == "completed" else 3
    except Exception as exc:
        result = {
            "schema_version": "1.0",
            "record_type": "cherrystudio_trademark_entry_error",
            "status": "incomplete",
            "phase": "entry_error",
            "validation": {"ok": False},
            "may_claim_complete": False,
            "error_type": type(exc).__name__,
            "errors": [str(exc)],
        }
        code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    raise SystemExit(code)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
