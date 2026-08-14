#!/usr/bin/env python3
"""Validated shared defaults for the CherryStudio production workflow."""

from __future__ import annotations

import json
import math
from pathlib import Path


POLICY_PATH = Path(__file__).with_name("runtime-policy.json")


def _named_ids(policy: dict, field: str) -> tuple[str, ...]:
    rows = policy.get(field)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"runtime policy {field!r} must be a non-empty list")
    values = tuple(str(row.get("id") or "").strip() for row in rows if isinstance(row, dict))
    if len(values) != len(rows) or any(not value for value in values) or len(set(values)) != len(values):
        raise RuntimeError(f"runtime policy {field!r} contains missing or duplicate ids")
    return values


def load_runtime_policy(path: Path = POLICY_PATH) -> dict:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"cannot load runtime policy {path}: {exc}") from exc
    if policy.get("schema_version") != "1.0":
        raise RuntimeError("unsupported runtime policy schema_version")
    _named_ids(policy, "sales_platforms")
    _named_ids(policy, "public_search_providers")
    rate = policy.get("sales_rate_limit")
    if not isinstance(rate, dict) or "default" not in rate:
        raise RuntimeError("runtime policy sales_rate_limit.default is required")
    for name, row in rate.items():
        if not isinstance(row, dict):
            raise RuntimeError(f"runtime policy rate entry {name!r} must be an object")
        minimum_delay = int(row.get("min_delay_ms", -1))
        maximum_delay = int(row.get("max_delay_ms", -1))
        legacy_cooldown = int(row.get("legacy_fixed_cooldown_minutes", -1))
        if minimum_delay < 0 or maximum_delay < minimum_delay or legacy_cooldown < 0:
            raise RuntimeError(f"runtime policy rate entry {name!r} is invalid")
        if "batch_size" in row:
            batch_size = int(row.get("batch_size", 0))
            pause_minimum = int(row.get("batch_pause_min_ms", -1))
            pause_maximum = int(row.get("batch_pause_max_ms", -1))
            if batch_size <= 0 or pause_minimum < 0 or pause_maximum < pause_minimum:
                raise RuntimeError(f"runtime policy rate batch entry {name!r} is invalid")
        if "delay_from_task_finish" in row and not isinstance(row.get("delay_from_task_finish"), bool):
            raise RuntimeError(f"runtime policy task-finish delay anchor {name!r} is invalid")
        settle_minimum = row.get("initial_settle_min_ms")
        settle_maximum = row.get("initial_settle_max_ms")
        if (settle_minimum is None) != (settle_maximum is None):
            raise RuntimeError(f"runtime policy initial settle range {name!r} is incomplete")
        if settle_minimum is not None:
            settle_minimum = int(settle_minimum)
            settle_maximum = int(settle_maximum)
            if settle_minimum < 0 or settle_maximum < settle_minimum:
                raise RuntimeError(f"runtime policy initial settle range {name!r} is invalid")
        cooldown_minimum = row.get("post_batch_cooldown_min_minutes")
        cooldown_maximum = row.get("post_batch_cooldown_max_minutes")
        if (cooldown_minimum is None) != (cooldown_maximum is None):
            raise RuntimeError(f"runtime policy post-batch cooldown range {name!r} is incomplete")
        if cooldown_minimum is not None:
            cooldown_minimum = int(cooldown_minimum)
            cooldown_maximum = int(cooldown_maximum)
            if cooldown_minimum < 0 or cooldown_maximum < cooldown_minimum:
                raise RuntimeError(f"runtime policy post-batch cooldown range {name!r} is invalid")
    risk = policy.get("sales_risk_circuit_breaker")
    if not isinstance(risk, dict):
        raise RuntimeError("runtime policy sales_risk_circuit_breaker object is required")
    risk_window = int(risk.get("risk_window_minutes", 0))
    decay = int(risk.get("success_decay_strikes", 0))
    history_limit = int(risk.get("max_history_entries", 0))
    tolerance = float(risk.get("legacy_match_tolerance_seconds", -1))
    tiers = risk.get("tiers")
    if risk_window <= 0 or decay <= 0 or history_limit <= 0 or tolerance < 0 or not isinstance(tiers, list) or not tiers:
        raise RuntimeError("runtime policy sales risk circuit breaker is invalid")
    previous_strike = 0
    for tier in tiers:
        if not isinstance(tier, dict):
            raise RuntimeError("runtime policy sales risk tier must be an object")
        strike = int(tier.get("min_strike_count", 0))
        minimum = int(tier.get("min_minutes", -1))
        maximum = int(tier.get("max_minutes", -1))
        if strike <= previous_strike or minimum < 0 or maximum < minimum:
            raise RuntimeError("runtime policy sales risk tiers are invalid or unordered")
        previous_strike = strike
    if int(tiers[0].get("min_strike_count", 0)) != 1:
        raise RuntimeError("runtime policy sales risk tiers must start at strike 1")
    public_search = policy.get("public_search")
    if not isinstance(public_search, dict):
        raise RuntimeError("runtime policy public_search object is required")
    if float(public_search.get("min_delay_sec", -1)) < 0 or float(public_search.get("max_delay_sec", -1)) < float(public_search.get("min_delay_sec", -1)):
        raise RuntimeError("runtime policy public-search delay range is invalid")
    if float(public_search.get("batch_pause_min_sec", -1)) < 0 or float(public_search.get("batch_pause_max_sec", -1)) < float(public_search.get("batch_pause_min_sec", -1)):
        raise RuntimeError("runtime policy public-search batch-pause range is invalid")
    if float(public_search.get("risk_cooldown_min_sec", -1)) < 0 or float(public_search.get("risk_cooldown_max_sec", -1)) < float(public_search.get("risk_cooldown_min_sec", -1)):
        raise RuntimeError("runtime policy public-search risk-cooldown range is invalid")
    if int(public_search.get("limit", 0)) <= 0 or int(public_search.get("timeout_ms", 0)) <= 0 or int(public_search.get("batch_size", 0)) <= 0:
        raise RuntimeError("runtime policy public-search limit and timeout must be positive")
    concurrency = policy.get("concurrency")
    if not isinstance(concurrency, dict):
        raise RuntimeError("runtime policy concurrency object is required")
    max_search_channels = int(concurrency.get("max_search_channels", 0))
    max_same_domain_requests = int(concurrency.get("max_same_domain_requests", 0))
    max_artifact_captures = int(concurrency.get("max_artifact_captures", 0))
    artifact_capture_wall_timeout_sec = int(concurrency.get("artifact_capture_wall_timeout_sec", 0))
    browser_protocol_timeout_sec = int(concurrency.get("browser_protocol_timeout_sec", 0))
    pdf_capture_timeout_sec = int(concurrency.get("pdf_capture_timeout_sec", 0))
    progress_stall_grace_sec = int(concurrency.get("progress_stall_grace_sec", 0))
    artifact_lock_timeout_sec = int(concurrency.get("artifact_lock_timeout_sec", 0))
    artifact_lock_stale_sec = int(concurrency.get("artifact_lock_stale_sec", 0))
    artifact_lock_poll_ms = int(concurrency.get("artifact_lock_poll_ms", 0))
    if (
        max_search_channels != 2
        or max_same_domain_requests != 1
        or max_artifact_captures != 1
        or browser_protocol_timeout_sec <= 0
        or pdf_capture_timeout_sec < browser_protocol_timeout_sec
        or artifact_capture_wall_timeout_sec < pdf_capture_timeout_sec
        or artifact_capture_wall_timeout_sec >= artifact_lock_timeout_sec
        or progress_stall_grace_sec <= 0
        or artifact_lock_timeout_sec <= 0
        or artifact_lock_stale_sec < artifact_lock_timeout_sec
        or artifact_lock_poll_ms < 25
    ):
        raise RuntimeError("runtime policy controlled concurrency limits are invalid")
    workflow_time_budget = policy.get("workflow_time_budget")
    if not isinstance(workflow_time_budget, dict):
        raise RuntimeError("runtime policy workflow_time_budget object is required")
    required_positive_budgets = (
        "resume_stage_b_hard_sec", "terminal_audit_hard_sec",
        "package_existing_hard_sec", "publish_local_hard_sec",
        "sales_channel_base_sec", "sales_channel_per_pending_task_sec",
        "sales_channel_hard_cap_sec", "fresh_run_target_minutes",
        "resumed_tail_target_minutes",
    )
    if any(int(workflow_time_budget.get(name, 0)) <= 0 for name in required_positive_budgets):
        raise RuntimeError("runtime policy workflow time budgets must be positive")
    if (
        int(workflow_time_budget["terminal_audit_hard_sec"])
        >= int(workflow_time_budget["resume_stage_b_hard_sec"])
        or int(workflow_time_budget["sales_channel_base_sec"])
        >= int(workflow_time_budget["sales_channel_hard_cap_sec"])
        or int(workflow_time_budget["sales_channel_hard_cap_sec"])
        >= int(workflow_time_budget["resume_stage_b_hard_sec"])
    ):
        raise RuntimeError("runtime policy workflow time budgets are inconsistent")
    pdf = policy.get("pdf")
    if not isinstance(pdf, dict):
        raise RuntimeError("runtime policy pdf object is required")
    minimum = int(pdf.get("min_pages_per_section", 0))
    maximum = int(pdf.get("max_pages_per_section", 0))
    sales_default = int(pdf.get("default_sales_pages_per_platform", 0))
    search_default = int(pdf.get("default_search_pages_per_provider", 0))
    if not (0 < minimum <= sales_default <= maximum and 0 < minimum <= search_default <= maximum):
        raise RuntimeError("runtime policy PDF limits are inconsistent")
    tail_quality = pdf.get("tail_quality")
    if not isinstance(tail_quality, dict):
        raise RuntimeError("runtime policy PDF tail_quality object is required")
    max_text_chars = int(tail_quality.get("max_text_chars", -1))
    max_nonwhite_ratio = float(tail_quality.get("max_nonwhite_ratio", -1))
    max_raster_ratio = float(tail_quality.get("max_largest_raster_area_ratio", -1))
    gray_threshold = int(tail_quality.get("nonwhite_gray_threshold", -1))
    analysis_scale = float(tail_quality.get("analysis_scale", -1))
    if (
        max_text_chars < 0
        or not 0 <= max_nonwhite_ratio <= 1
        or not 0 <= max_raster_ratio <= 1
        or not 0 <= gray_threshold <= 255
        or not 0 < analysis_scale <= 4
    ):
        raise RuntimeError("runtime policy PDF tail_quality thresholds are invalid")
    return policy


RUNTIME_POLICY = load_runtime_policy()
SALES_PLATFORMS = _named_ids(RUNTIME_POLICY, "sales_platforms")
PUBLIC_SEARCH_PROVIDERS = _named_ids(RUNTIME_POLICY, "public_search_providers")
SALES_PLATFORM_LABELS = {
    str(row["id"]): str(row.get("label") or row["id"])
    for row in RUNTIME_POLICY["sales_platforms"]
}
PUBLIC_SEARCH_PROVIDER_LABELS = {
    str(row["id"]): str(row.get("label") or row["id"])
    for row in RUNTIME_POLICY["public_search_providers"]
}
SALES_RISK_CIRCUIT_BREAKER_POLICY = RUNTIME_POLICY["sales_risk_circuit_breaker"]
PUBLIC_SEARCH_POLICY = RUNTIME_POLICY["public_search"]
DEFAULT_PUBLIC_SEARCH_LIMIT = int(PUBLIC_SEARCH_POLICY["limit"])
DEFAULT_PUBLIC_SEARCH_TIMEOUT_MS = int(PUBLIC_SEARCH_POLICY["timeout_ms"])
DEFAULT_PUBLIC_SEARCH_MIN_DELAY_SEC = float(PUBLIC_SEARCH_POLICY["min_delay_sec"])
DEFAULT_PUBLIC_SEARCH_MAX_DELAY_SEC = float(PUBLIC_SEARCH_POLICY["max_delay_sec"])
DEFAULT_PUBLIC_SEARCH_BATCH_SIZE = int(PUBLIC_SEARCH_POLICY["batch_size"])
DEFAULT_PUBLIC_SEARCH_BATCH_PAUSE_MIN_SEC = float(PUBLIC_SEARCH_POLICY["batch_pause_min_sec"])
DEFAULT_PUBLIC_SEARCH_BATCH_PAUSE_MAX_SEC = float(PUBLIC_SEARCH_POLICY["batch_pause_max_sec"])
DEFAULT_PUBLIC_SEARCH_RISK_COOLDOWN_MIN_SEC = float(PUBLIC_SEARCH_POLICY["risk_cooldown_min_sec"])
DEFAULT_PUBLIC_SEARCH_RISK_COOLDOWN_MAX_SEC = float(PUBLIC_SEARCH_POLICY["risk_cooldown_max_sec"])
CONCURRENCY_POLICY = RUNTIME_POLICY["concurrency"]
MAX_PARALLEL_SEARCH_CHANNELS = int(CONCURRENCY_POLICY["max_search_channels"])
MAX_SAME_DOMAIN_REQUESTS = int(CONCURRENCY_POLICY["max_same_domain_requests"])
MAX_PARALLEL_ARTIFACT_CAPTURES = int(CONCURRENCY_POLICY["max_artifact_captures"])
ARTIFACT_CAPTURE_WALL_TIMEOUT_SEC = int(CONCURRENCY_POLICY["artifact_capture_wall_timeout_sec"])
BROWSER_PROTOCOL_TIMEOUT_SEC = int(CONCURRENCY_POLICY["browser_protocol_timeout_sec"])
PDF_CAPTURE_TIMEOUT_SEC = int(CONCURRENCY_POLICY["pdf_capture_timeout_sec"])
PROGRESS_STALL_GRACE_SEC = int(CONCURRENCY_POLICY["progress_stall_grace_sec"])
ARTIFACT_LOCK_TIMEOUT_SEC = int(CONCURRENCY_POLICY["artifact_lock_timeout_sec"])
ARTIFACT_LOCK_STALE_SEC = int(CONCURRENCY_POLICY["artifact_lock_stale_sec"])
ARTIFACT_LOCK_POLL_MS = int(CONCURRENCY_POLICY["artifact_lock_poll_ms"])
WORKFLOW_TIME_BUDGET = RUNTIME_POLICY["workflow_time_budget"]
RESUME_STAGE_B_HARD_SEC = int(WORKFLOW_TIME_BUDGET["resume_stage_b_hard_sec"])
TERMINAL_AUDIT_HARD_SEC = int(WORKFLOW_TIME_BUDGET["terminal_audit_hard_sec"])
PACKAGE_EXISTING_HARD_SEC = int(WORKFLOW_TIME_BUDGET["package_existing_hard_sec"])
PUBLISH_LOCAL_HARD_SEC = int(WORKFLOW_TIME_BUDGET["publish_local_hard_sec"])
SALES_CHANNEL_BASE_SEC = int(WORKFLOW_TIME_BUDGET["sales_channel_base_sec"])
SALES_CHANNEL_PER_PENDING_TASK_SEC = int(WORKFLOW_TIME_BUDGET["sales_channel_per_pending_task_sec"])
SALES_CHANNEL_HARD_CAP_SEC = int(WORKFLOW_TIME_BUDGET["sales_channel_hard_cap_sec"])


def sales_channel_wall_timeout_seconds(pending_task_count: int, pages_per_query: int = 1) -> int:
    """Bound one sales subprocess by remaining work, never by the original matrix size."""
    pending_task_count = int(pending_task_count)
    pages_per_query = int(pages_per_query)
    if pending_task_count <= 0 or pages_per_query <= 0:
        raise ValueError("pending_task_count and pages_per_query must be positive")
    calculated = SALES_CHANNEL_BASE_SEC + (
        pending_task_count * pages_per_query * SALES_CHANNEL_PER_PENDING_TASK_SEC
    )
    return min(SALES_CHANNEL_HARD_CAP_SEC, max(SALES_CHANNEL_BASE_SEC, calculated))


def public_search_matrix_wall_timeout_seconds(task_count: int, provider_count: int) -> int:
    """Return a fail-safe outer timeout derived from the configured matrix budget."""
    task_count = int(task_count)
    provider_count = int(provider_count)
    if task_count <= 0 or provider_count <= 0:
        raise ValueError("task_count and provider_count must be positive")
    tasks_per_provider = math.ceil(task_count / provider_count)
    wait_intervals = max(0, tasks_per_provider - 1)
    batch_size = DEFAULT_PUBLIC_SEARCH_BATCH_SIZE
    batch_pauses = wait_intervals // batch_size if batch_size > 0 else 0
    maximum_wait = provider_count * (
        wait_intervals * DEFAULT_PUBLIC_SEARCH_MAX_DELAY_SEC
        + batch_pauses * DEFAULT_PUBLIC_SEARCH_BATCH_PAUSE_MAX_SEC
    )
    per_task_wall = DEFAULT_PUBLIC_SEARCH_TIMEOUT_MS / 1000 + 20
    safety_margin = max(120, task_count * 5)
    return max(600, math.ceil(task_count * per_task_wall + maximum_wait + safety_margin))
PDF_POLICY = RUNTIME_POLICY["pdf"]
DEFAULT_SALES_PAGES_PER_PLATFORM = int(PDF_POLICY["default_sales_pages_per_platform"])
DEFAULT_SEARCH_PAGES_PER_PROVIDER = int(PDF_POLICY["default_search_pages_per_provider"])
MIN_PAGES_PER_SECTION = int(PDF_POLICY["min_pages_per_section"])
MAX_PAGES_PER_SECTION = int(PDF_POLICY["max_pages_per_section"])
PDF_TAIL_QUALITY_POLICY = PDF_POLICY["tail_quality"]
PDF_TAIL_MAX_TEXT_CHARS = int(PDF_TAIL_QUALITY_POLICY["max_text_chars"])
PDF_TAIL_MAX_NONWHITE_RATIO = float(PDF_TAIL_QUALITY_POLICY["max_nonwhite_ratio"])
PDF_TAIL_MAX_LARGEST_RASTER_AREA_RATIO = float(
    PDF_TAIL_QUALITY_POLICY["max_largest_raster_area_ratio"]
)
PDF_TAIL_NONWHITE_GRAY_THRESHOLD = int(PDF_TAIL_QUALITY_POLICY["nonwhite_gray_threshold"])
PDF_TAIL_ANALYSIS_SCALE = float(PDF_TAIL_QUALITY_POLICY["analysis_scale"])
