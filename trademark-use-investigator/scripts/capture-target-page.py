#!/usr/bin/env python3

"""Capture a real destination page into candidate-pages and verify its offline HTML."""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from run_lock import RunFileLock, atomic_write_jsonl
from process_utils import run_bounded
from url_utils import (
    hostname, is_search_result_url, normalize_url, require_safe_file_id, site_key,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


QUICK_MAX_TIMEOUT_MS = 30_000
ACCESS_DENIED_HTTP_STATUSES = {401, 403, 405, 407, 429, 451}


def classify_error_page(url: str, title: str, body: str, http_status: object = None) -> dict | None:
    """Return a terminal error classification that image-only review cannot override."""
    try:
        status = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        status = None
    if status in ACCESS_DENIED_HTTP_STATUSES:
        return {"state": "access_denied", "signal": f"http_status_{status}"}
    if status is not None and status >= 400:
        return {"state": "error_page", "signal": f"http_status_{status}"}

    try:
        parsed = urlsplit(str(url or ""))
        pathname = parsed.path
        error_hints = dict(
            pair.split("=", 1) if "=" in pair else (pair, "")
            for pair in parsed.query.split("&") if pair
        )
        error_hint = " ".join(
            error_hints.get(key, "") for key in ("status", "error", "code", "httpStatus")
        ).strip()
    except ValueError:
        pathname = str(url or "")
        error_hint = ""
    if error_hint == "405" or re.search(
        r"(?:^|[/_.-])405(?:[/_.-]|$)|/(?:forbidden|access[-_]?denied|permission[-_]?denied|blocked)(?:[/.]|$)",
        pathname, re.I,
    ):
        return {"state": "access_denied", "signal": "access_denied_url_path"}
    if error_hint == "404" or re.search(
        r"(?:^|[/_.-])404(?:[/_.-]|$)|/(?:errors?|not[-_]?found|page[-_]?not[-_]?found)(?:[/.]|$)",
        pathname, re.I,
    ):
        return {"state": "error_page", "signal": "error_url_path"}

    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    if re.search(
        r"(?:^|[\s([（])405(?:[\s\])）:：-]+(?:method not allowed|错误|请求不允许|不允许)|\s*$)|method not allowed|access denied|forbidden|拒绝访问|无权访问|访问受限|请求被拒绝",
        clean_title,
        re.I,
    ):
        return {"state": "access_denied", "signal": "access_denied_title"}
    if re.search(
        r"^(?:4(?:00|10)\b|5\d\d\b)|(?:^|[\s([（])404(?:[\s\])）:：-]+(?:not found|page not found|错误|页面不存在|页面未找到)|\s*$)|page not found|not found|页面不存在|页面未找到|找不到页面|错误页|系统错误|服务器错误|bad gateway|service unavailable",
        clean_title,
        re.I,
    ):
        return {"state": "error_page", "signal": "error_page_title"}

    clean_body = re.sub(r"\s+", " ", str(body or "")).strip()[:12_000]
    if re.search(
        r"\b405\s+method not allowed\b|the requested method is not allowed|请求方法不允许|您没有权限访问|无权访问此页面|拒绝访问此页面",
        clean_body,
        re.I,
    ):
        return {"state": "access_denied", "signal": "access_denied_body"}
    if re.search(
        r"\b404\s+(?:not found|page not found)\b|the requested (?:url|page) was not found|您访问的页面不存在|页面不存在或已删除|抱歉[，, ]*(?:您访问的)?页面(?:不存在|找不到)|系统(?:发生)?错误|服务器(?:内部)?错误",
        clean_body,
        re.I,
    ):
        return {"state": "error_page", "signal": "error_page_body"}
    return None


def execution_profile(config: dict) -> str:
    """Legacy run configurations retain the original forensic behaviour."""
    return str(config.get("execution_profile") or "forensic").strip().lower()


def timeout_output(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_command(command: list[str], wall_timeout_seconds: float) -> subprocess.CompletedProcess:
    """Run one worker with a hard wall clock and a normal timeout result."""
    return run_bounded(command, timeout=wall_timeout_seconds)


def browser_wall_timeout_seconds(timeout_ms: int, wait_for_unblock_ms: int = 0) -> float:
    return max(
        60.0,
        2 * timeout_ms / 1000.0 + wait_for_unblock_ms / 1000.0 + 45.0,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(path: Path, root: Path) -> dict:
    return {"path": str(path.relative_to(root)).replace("\\", "/"), "size_bytes": path.stat().st_size, "sha256": sha256(path)}


def read_frontier(run_dir: Path) -> dict:
    path = run_dir / "discovery" / "url-frontier.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"items": []}


def read_attempts(run_dir: Path) -> list[dict]:
    path = run_dir / "capture-attempts.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def merge_attempt_record(records: list[dict], record: dict, phase: str) -> list[dict]:
    previous = next((item for item in records if item.get("candidate_id") == record.get("candidate_id")), None)
    if previous:
        if normalize_url(previous.get("url") or "") != normalize_url(record.get("url") or ""):
            raise ValueError("A candidate-id cannot be reused for a different normalized URL")
        history = [dict(item) for item in (previous.get("attempt_history") or [])]
        if not history:
            history.append({
                "attempted_at": previous.get("attempted_at"),
                "attempt_id": previous.get("attempt_id"),
                "status": previous.get("status"),
                "diagnostic_dir": previous.get("diagnostic_dir"),
            })
        if phase == "started":
            history.append({
                "attempted_at": record.get("attempted_at"),
                "attempt_id": record.get("attempt_id"),
                "invocation_id": record.get("attempt_id"),
                "status": "started",
                "diagnostic_dir": record.get("diagnostic_dir"),
            })
            attempt_count = int(previous.get("attempt_count") or 1) + 1
        else:
            current = next((item for item in history if item.get("attempt_id") == record.get("attempt_id")), None)
            if current is None:
                raise ValueError("Terminal capture state has no matching started attempt")
            current.update({
                "status": record.get("status"),
                "completed_at": record.get("completed_at"),
                "diagnostic_dir": record.get("diagnostic_dir"),
                "candidate_dir": record.get("candidate_dir"),
            })
            attempt_count = int(previous.get("attempt_count") or len(history) or 1)
        record["attempt_count"] = attempt_count
        record["first_attempted_at"] = previous.get("first_attempted_at") or previous.get("attempted_at")
        record["attempt_history"] = history
    else:
        if phase != "started":
            raise ValueError("Terminal capture state cannot be written before started state")
        record["attempt_count"] = 1
        record["first_attempted_at"] = record.get("attempted_at")
        record["attempt_history"] = [{
            "attempted_at": record.get("attempted_at"),
            "attempt_id": record.get("attempt_id"),
            "invocation_id": record.get("attempt_id"),
            "status": "started",
            "diagnostic_dir": record.get("diagnostic_dir"),
        }]
    record["last_attempted_at"] = record.get("attempted_at")
    records = [item for item in records if item.get("candidate_id") != record.get("candidate_id")]
    records.append(record)
    records.sort(key=lambda item: item.get("candidate_id", ""))
    return records


def write_attempt_phase(run_dir: Path, record: dict, phase: str) -> None:
    path = run_dir / "capture-attempts.jsonl"
    with RunFileLock(run_dir, "capture-attempts"):
        records = merge_attempt_record(read_attempts(run_dir), record, phase)
        atomic_write_jsonl(path, records)


def sensitive_descriptor(kind: str, path: Path, run_dir: Path) -> dict:
    absolute = str(path.resolve())
    return {
        "kind": kind,
        "basename": path.name,
        "absolute_path_sha256": hashlib.sha256(absolute.encode("utf-8")).hexdigest(),
        "inside_run_dir": path.resolve() == run_dir or path.resolve().is_relative_to(run_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture and offline-verify one non-search destination URL")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--page-type", default="other", choices=["product", "company", "registry", "article", "official_site", "other"])
    parser.add_argument("--frontier-id")
    parser.add_argument("--probe-id", help="Quick profile probe selected in discovery/probe-shortlist.json")
    parser.add_argument("--manual-source-reason")
    parser.add_argument("--expected-text")
    parser.add_argument("--expected-text-all")
    parser.add_argument("--expected-text-any")
    parser.add_argument("--allow-image-only", action="store_true")
    parser.add_argument("--capture-images", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--storage-state")
    parser.add_argument("--user-data-dir")
    parser.add_argument("--browser-executable")
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument(
        "--wait-for-unblock-ms", type=int, default=0,
        help="With --headed, wait this long for manual login/CAPTCHA completion before rejecting the page",
    )
    args = parser.parse_args()

    args.candidate_id = require_safe_file_id(args.candidate_id, "candidate-id")
    if args.wait_for_unblock_ms < 0:
        raise ValueError("wait-for-unblock-ms cannot be negative")
    if args.wait_for_unblock_ms and not args.headed:
        raise ValueError("--wait-for-unblock-ms requires --headed")
    run_dir = Path(args.run_dir).resolve()
    config_path = run_dir / "run-config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"run-config.json not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    profile = execution_profile(config)
    if args.probe_id:
        args.probe_id = require_safe_file_id(args.probe_id, "probe-id")
    if args.timeout_ms < 1:
        raise ValueError("timeout-ms must be a positive integer")
    if profile == "quick":
        forbidden = []
        if args.headed:
            forbidden.append("--headed")
        if args.wait_for_unblock_ms:
            forbidden.append("--wait-for-unblock-ms")
        if args.storage_state:
            forbidden.append("--storage-state")
        if args.user_data_dir:
            forbidden.append("--user-data-dir")
        if forbidden:
            raise ValueError("Quick profile forbids " + ", ".join(forbidden))
        args.timeout_ms = min(args.timeout_ms, QUICK_MAX_TIMEOUT_MS)
    expectation_values = [
        item.strip()
        for raw in (args.expected_text, args.expected_text_all, args.expected_text_any)
        for item in str(raw or "").split("|||")
        if item.strip()
    ]
    if not expectation_values:
        raise ValueError(
            "At least one non-empty --expected-text/--expected-text-all/--expected-text-any assertion is required"
        )
    normalized = normalize_url(args.url)
    if not normalized:
        raise ValueError("URL is not a valid HTTP(S) target")
    if is_search_result_url(normalized):
        raise ValueError("Search-engine and platform search pages are discovery artifacts and cannot be captured as candidates")

    frontier = read_frontier(run_dir)
    frontier_item = None
    if args.frontier_id:
        if isinstance(args.manual_source_reason, str) and args.manual_source_reason.strip():
            raise ValueError("Use either --frontier-id or --manual-source-reason, not both")
        frontier_item = next((
            item for item in frontier.get("items", [])
            if item.get("normalized_url") == normalized and item.get("target_id") == args.frontier_id
        ), None)
        if not frontier_item:
            raise ValueError("frontier-id and normalized URL do not identify the same URL-frontier item")
    elif not isinstance(args.manual_source_reason, str) or not args.manual_source_reason.strip():
        raise ValueError("A discovered target requires --frontier-id matching both its target ID and normalized URL; use --manual-source-reason only for a user-supplied direct URL")
    probe_item = None
    if profile == "quick" and not config.get("test_mode"):
        if not frontier_item:
            raise ValueError("Quick full capture requires a discovered frontier URL selected by the probe stage")
        if not args.probe_id:
            raise ValueError("Quick full capture requires --probe-id from discovery/probe-shortlist.json")
        shortlist_path = run_dir / "discovery" / "probe-shortlist.json"
        if not shortlist_path.is_file():
            raise FileNotFoundError(f"Quick probe shortlist not found: {shortlist_path}")
        shortlist = json.loads(shortlist_path.read_text(encoding="utf-8"))
        probe_item = next((
            item for item in (shortlist.get("items") or [])
            if item.get("target_id") == args.frontier_id
            and normalize_url(item.get("normalized_url") or item.get("url") or "") == normalized
            and (item.get("probe") or {}).get("probe_id") == args.probe_id
        ), None)
        if not probe_item:
            raise ValueError("probe-id, frontier-id and URL do not identify one content-valid shortlisted probe")
        probe_rel = str((probe_item.get("probe") or {}).get("probe_dir") or "")
        probe_dir = (run_dir / probe_rel).resolve()
        probe_metadata_path = probe_dir / "metadata.json"
        if (
            not probe_rel
            or not probe_dir.is_relative_to(run_dir)
            or not probe_metadata_path.is_file()
        ):
            raise ValueError("Shortlisted probe metadata is missing or outside RUN_DIR")
        probe_metadata = json.loads(probe_metadata_path.read_text(encoding="utf-8"))
        if (
            probe_metadata.get("probe_id") != args.probe_id
            or probe_metadata.get("frontier_id") != args.frontier_id
            or probe_metadata.get("content_valid") is not True
            or normalize_url(probe_metadata.get("requested_normalized_url") or "") != normalized
        ):
            raise ValueError("Shortlisted probe metadata is not content-valid or does not match the requested URL")
    manual_source_reason = args.manual_source_reason.strip() if args.manual_source_reason else None
    source_mode = "discovered_frontier" if frontier_item else "manual_user_url"
    source_provenance = {
        "source_mode": source_mode,
        "frontier_id": frontier_item.get("target_id") if frontier_item else None,
        "manual_source_reason": manual_source_reason,
        "probe_id": args.probe_id,
        "discovered_via": frontier_item.get("discovered_via", []) if frontier_item else [],
    }

    sensitive_inputs = []
    if args.storage_state:
        storage_state = Path(args.storage_state).resolve()
        if storage_state == run_dir or storage_state.is_relative_to(run_dir):
            raise ValueError("storage-state is sensitive browser state and must be outside RUN_DIR")
        if not storage_state.is_file():
            raise FileNotFoundError(f"storage-state file not found: {storage_state}")
        sensitive_inputs.append(sensitive_descriptor("storage_state", storage_state, run_dir))
        args.storage_state = str(storage_state)
    if args.user_data_dir:
        profile_dir = Path(args.user_data_dir).resolve()
        if profile_dir == run_dir or profile_dir.is_relative_to(run_dir):
            raise ValueError("user-data-dir is private browser state and must be outside RUN_DIR")
        sensitive_inputs.append(sensitive_descriptor("user_data_dir", profile_dir, run_dir))
        args.user_data_dir = str(profile_dir)

    budgets = config.get("budgets") or {}
    max_pages_per_domain = max(1, int(budgets.get("max_pages_per_domain", 5)))
    max_capture_attempts = max(1, int(budgets.get("max_capture_attempts", 30)))
    domain = hostname(normalized)
    requested_site_key = site_key(normalized)

    candidate_dir = (run_dir / "candidate-pages" / args.candidate_id).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    attempt_id = f"{args.candidate_id}-{stamp}"
    attempt = run_dir / "capture-diagnostics" / args.candidate_id / f"target-{stamp}"
    diagnostic_rel = str(attempt.relative_to(run_dir)).replace("\\", "/")
    started_record = {
        "schema_version": "2.0", "candidate_id": args.candidate_id, "url": normalized,
        "requested_normalized_url": normalized, "domain": domain, "site_key": requested_site_key,
        "frontier_id": frontier_item.get("target_id") if frontier_item else None,
        "source_mode": source_mode, "source_provenance": source_provenance,
        "manual_source_reason": manual_source_reason,
        "probe_id": args.probe_id,
        "expected_assertions": expectation_values, "sensitive_inputs": sensitive_inputs,
        "attempt_id": attempt_id, "attempted_at": stamp, "status": "started",
        "candidate_dir": None, "diagnostic_dir": diagnostic_rel,
    }
    # Budget reservation, unique candidate/URL binding, directory creation and
    # the started audit record are one run-level critical section.
    with RunFileLock(run_dir, "capture-attempts"):
        attempts = read_attempts(run_dir)
        prior_candidate = next((item for item in attempts if item.get("candidate_id") == args.candidate_id), None)
        if prior_candidate and normalize_url(prior_candidate.get("url") or "") != normalized:
            raise ValueError("A candidate-id cannot be reused for a different normalized URL")
        prior_url = next((item for item in attempts if normalize_url(item.get("url") or "") == normalized), None)
        if prior_url and prior_url.get("candidate_id") != args.candidate_id:
            raise ValueError(f"Retry this normalized URL with its original candidate-id: {prior_url.get('candidate_id')}")
        attempted_site_urls = {
            candidate_url
            for item in attempts
            if (candidate_url := normalize_url(item.get("url") or ""))
            and site_key(candidate_url) == requested_site_key
        }
        attempted_urls = {
            candidate_url
            for item in attempts
            if (candidate_url := normalize_url(item.get("url") or ""))
        }
        if normalized not in attempted_urls and len(attempted_urls) >= max_capture_attempts:
            raise ValueError(
                f"max_capture_attempts={max_capture_attempts} reached; "
                "retrying an already attempted normalized URL remains allowed"
            )
        if normalized not in attempted_site_urls and len(attempted_site_urls) >= max_pages_per_domain:
            raise ValueError(
                f"max_pages_per_domain={max_pages_per_domain} reached for site_key={requested_site_key}; "
                "retries of an already attempted normalized URL remain allowed"
            )
        if candidate_dir.exists() and any(candidate_dir.iterdir()):
            raise FileExistsError(f"Candidate directory already exists: {candidate_dir}")
        if candidate_dir.exists():
            candidate_dir.rmdir()
        attempt.mkdir(parents=True, exist_ok=False)
        records = merge_attempt_record(attempts, started_record, "started")
        atomic_write_jsonl(run_dir / "capture-attempts.jsonl", records)

    terminal_written = False

    def mark_aborted() -> None:
        if terminal_written:
            return
        aborted = dict(started_record)
        aborted.update({
            "status": "aborted", "completed_at": datetime.now(timezone.utc).isoformat(),
            "content_valid": False, "offline_ok": False, "portable_html_ok": False,
            "visual_similarity_ok": False,
        })
        try:
            write_attempt_phase(run_dir, aborted, "terminal")
        except Exception:
            # A hard process kill can intentionally leave the started record;
            # strict validation reports that orphan rather than hiding it.
            pass

    atexit.register(mark_aborted)

    capture_script = Path(__file__).resolve().with_name("capture-page-to-pdf.mjs")
    command = [
        "node", str(capture_script), "--url", normalized, "--output-dir", str(attempt),
        "--source-id", args.candidate_id, "--label", args.label, "--page-type", args.page_type,
        "--timeout-ms", str(args.timeout_ms),
    ]
    for flag, value in (
        ("--expected-text", args.expected_text), ("--expected-text-all", args.expected_text_all),
        ("--expected-text-any", args.expected_text_any), ("--storage-state", args.storage_state),
        ("--user-data-dir", args.user_data_dir), ("--browser-executable", args.browser_executable),
    ):
        if value:
            command.extend([flag, str(value)])
    if args.allow_image_only:
        command.append("--allow-image-only")
    if args.capture_images:
        command.append("--capture-images")
    if args.headed:
        command.append("--headed")
    if args.wait_for_unblock_ms:
        command.extend(["--wait-for-unblock-ms", str(args.wait_for_unblock_ms)])

    worker_wall_timeout = browser_wall_timeout_seconds(args.timeout_ms, args.wait_for_unblock_ms)
    capture = run_command(command, worker_wall_timeout)
    metadata_path = attempt / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    if capture.returncode == 124:
        metadata.update({
            "page_state": "subprocess_timeout",
            "content_valid": False,
            "subprocess_error": (capture.stderr or "")[-2000:],
        })
    body_path = attempt / "body-text.txt"
    captured_body = body_path.read_text(encoding="utf-8", errors="replace") if body_path.is_file() else ""
    error_classification = classify_error_page(
        metadata.get("final_url") or normalized,
        metadata.get("title") or "",
        captured_body,
        metadata.get("http_status"),
    )
    if error_classification and metadata.get("page_state") != "subprocess_timeout":
        metadata["page_state"] = error_classification["state"]
        metadata["content_valid"] = False
        signals = list(metadata.get("block_signals") or [])
        signals.append(error_classification["signal"])
        metadata["block_signals"] = list(dict.fromkeys(signals))
    final_normalized = normalize_url(metadata.get("final_url") or "")
    final_search_page = not final_normalized or is_search_result_url(final_normalized)
    blocked_states = {
        "captcha", "login_required", "access_denied", "error_page", "empty_shell", "empty_results",
        "search_result_page",
    }
    redirect_external_domain = bool(
        final_normalized and site_key(final_normalized) != requested_site_key
    )
    assertion_passed = metadata.get("page_state") == "normal"
    if final_search_page and metadata.get("page_state") != "subprocess_timeout":
        metadata["page_state"] = "search_result_page"
        metadata["content_valid"] = False
        metadata["search_result_page"] = True
        metadata.setdefault("block_signals", []).append("final_url_is_search_or_invalid")
    if redirect_external_domain and not assertion_passed:
        metadata["page_state"] = "unexpected_content"
        metadata["content_valid"] = False
        metadata.setdefault("block_signals", []).append("external_redirect_without_text_assertion_match")
    base_ok = bool(
        capture.returncode == 0
        and metadata.get("content_valid") is True
        and metadata.get("page_state") not in blocked_states
        and not metadata.get("search_result_page")
        and final_normalized
    )
    archive_report = None
    singlefile_replay = None
    mhtml_replay = None
    visual_comparison = None

    if base_ok:
        verify_script = Path(__file__).resolve().with_name("verify-offline-page.mjs")

        def add_expectations(command_line):
            if metadata.get("page_state") == "manual_visual_review":
                return
            expected_all = [item for item in (args.expected_text, args.expected_text_all) if item]
            if expected_all:
                command_line.extend(["--expected-text-all", "|||".join(expected_all)])
            if args.expected_text_any:
                command_line.extend(["--expected-text-any", args.expected_text_any])

        if (attempt / "page.mhtml").is_file():
            mhtml_command = [
                "node", str(verify_script), "--html", str(attempt / "page.mhtml"),
                "--output", str(attempt / "mhtml-validation.json"),
                "--screenshot", str(attempt / "mhtml-offline.png"),
                "--online-text", str(attempt / "body-text.txt"),
                "--online-substantive-images", str(metadata.get("substantive_image_count") or 0),
            ]
            if args.browser_executable:
                mhtml_command.extend(["--browser-executable", args.browser_executable])
            add_expectations(mhtml_command)
            mhtml_completed = run_command(mhtml_command, worker_wall_timeout)
            mhtml_path = attempt / "mhtml-validation.json"
            mhtml_replay = json.loads(mhtml_path.read_text(encoding="utf-8")) if mhtml_path.is_file() else {
                "ok": False, "errors": [{"stage": "subprocess", "message": (mhtml_completed.stderr or mhtml_completed.stdout)[-2000:]}]
            }
            if mhtml_replay.get("ok") and (attempt / "mhtml-offline.png").is_file():
                compare_script = Path(__file__).resolve().with_name("compare-screenshots.py")
                compared = run_command([
                    sys.executable, str(compare_script), "--online", str(attempt / "fullpage.png"),
                    "--offline", str(attempt / "mhtml-offline.png"),
                    "--output", str(attempt / "archive-visual-comparison.json"), "--min-similarity", "0.90",
                ], max(15.0, min(60.0, worker_wall_timeout)))
                comparison_path = attempt / "archive-visual-comparison.json"
                visual_comparison = json.loads(comparison_path.read_text(encoding="utf-8")) if comparison_path.is_file() else {
                    "ok": False, "errors": [(compared.stderr or compared.stdout)[-2000:]]
                }

        archive_script = Path(__file__).resolve().with_name("archive-singlefile.py")
        archive_timeout_ms = (
            args.timeout_ms
            if profile == "quick" or config.get("test_mode") is True
            else max(args.timeout_ms, 90000)
        )
        archive_command = [
            sys.executable, str(archive_script), "--url", metadata.get("final_url") or normalized,
            "--output", str(attempt / "page.singlefile.html"), "--timeout-ms", str(archive_timeout_ms),
        ]
        if args.browser_executable:
            archive_command.extend(["--browser-executable", args.browser_executable])
        if args.storage_state:
            archive_command.extend(["--storage-state", args.storage_state])
        if args.user_data_dir:
            archive_command.extend(["--user-data-dir", args.user_data_dir])
        if args.headed:
            archive_command.append("--headed")
        if args.wait_for_unblock_ms:
            archive_command.extend(["--wait-for-unblock-ms", str(args.wait_for_unblock_ms)])
        archive_inner_timeout = max(
            45.0,
            2 * max(10_000, archive_timeout_ms) / 1000.0
            + args.wait_for_unblock_ms / 1000.0 + 30.0,
        )
        # The archive worker still needs bounded CDP startup and independent
        # browser/profile cleanup after its SingleFile deadline expires.
        archive_wall_timeout = archive_inner_timeout + 45.0
        archived = run_command(archive_command, archive_wall_timeout)
        try:
            archive_report = json.loads(archived.stdout)
        except Exception:
            archive_report = {"ok": False, "exit_code": archived.returncode, "stderr_tail": (archived.stderr or archived.stdout)[-2000:]}

        if archive_report.get("ok") and (attempt / "page.singlefile.html").is_file():
            verify_command = [
                "node", str(verify_script), "--html", str(attempt / "page.singlefile.html"),
                "--output", str(attempt / "offline-validation.json"),
                "--screenshot", str(attempt / "offline.png"),
                "--online-text", str(attempt / "body-text.txt"),
                "--online-substantive-images", "0",
            ]
            if args.browser_executable:
                verify_command.extend(["--browser-executable", args.browser_executable])
            add_expectations(verify_command)
            verified = run_command(verify_command, worker_wall_timeout)
            offline_path = attempt / "offline-validation.json"
            singlefile_replay = json.loads(offline_path.read_text(encoding="utf-8")) if offline_path.is_file() else {
                "ok": False, "errors": [{"stage": "subprocess", "message": (verified.stderr or verified.stdout)[-2000:]}]
            }

    accepted = bool(
        base_ok and archive_report and archive_report.get("ok")
        and singlefile_replay and singlefile_replay.get("ok")
        and mhtml_replay and mhtml_replay.get("ok")
        and visual_comparison and visual_comparison.get("ok")
    )
    metadata.update({
        "schema_version": "2.0", "record_type": "target_page_capture", "page_role": "candidate",
        "candidate_id": args.candidate_id, "frontier_id": frontier_item.get("target_id") if frontier_item else None,
        "discovered_via": frontier_item.get("discovered_via", []) if frontier_item else [],
        "source_mode": source_mode, "source_provenance": source_provenance,
        "manual_source_reason": manual_source_reason,
        "probe_id": args.probe_id,
        "expected_assertions": expectation_values, "sensitive_inputs": sensitive_inputs,
        "requested_normalized_url": normalized, "normalized_url": normalized,
        "final_normalized_url": final_normalized,
        "redirect_external_domain": redirect_external_domain,
        "content_assertion_passed": assertion_passed,
        "candidate_accepted": accepted, "evidence_accepted": False,
        "archive_primary": "page.mhtml", "portable_html": "page.singlefile.html",
        "singlefile": archive_report, "singlefile_replay": singlefile_replay,
        "offline_replay": mhtml_replay, "archive_visual_comparison": visual_comparison,
    })
    if (attempt / "page.singlefile.html").is_file():
        metadata.setdefault("artifacts", {})["singlefile_html"] = artifact(attempt / "page.singlefile.html", attempt)
    if (attempt / "page.singlefile.raw.html").is_file():
        metadata.setdefault("artifacts", {})["singlefile_raw_html"] = artifact(attempt / "page.singlefile.raw.html", attempt)
    if (attempt / "offline-validation.json").is_file():
        metadata.setdefault("artifacts", {})["offline_validation"] = artifact(attempt / "offline-validation.json", attempt)
    if (attempt / "offline.png").is_file():
        metadata.setdefault("artifacts", {})["offline_screenshot"] = artifact(attempt / "offline.png", attempt)
    if (attempt / "mhtml-validation.json").is_file():
        metadata.setdefault("artifacts", {})["mhtml_validation"] = artifact(attempt / "mhtml-validation.json", attempt)
    if (attempt / "mhtml-offline.png").is_file():
        metadata.setdefault("artifacts", {})["mhtml_offline_screenshot"] = artifact(attempt / "mhtml-offline.png", attempt)
    if (attempt / "archive-visual-comparison.json").is_file():
        metadata.setdefault("artifacts", {})["archive_visual_comparison"] = artifact(attempt / "archive-visual-comparison.json", attempt)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    final_location = attempt
    if accepted:
        candidate_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(attempt), str(candidate_dir))
        final_location = candidate_dir
    attempt_record = {
        "schema_version": "2.0", "candidate_id": args.candidate_id, "url": normalized,
        "requested_normalized_url": normalized, "final_normalized_url": final_normalized,
        "redirect_external_domain": redirect_external_domain,
        "domain": domain, "site_key": requested_site_key,
        "frontier_id": frontier_item.get("target_id") if frontier_item else None,
        "source_mode": source_mode, "source_provenance": source_provenance,
        "manual_source_reason": manual_source_reason,
        "probe_id": args.probe_id,
        "expected_assertions": expectation_values, "sensitive_inputs": sensitive_inputs,
        "attempt_id": attempt_id, "attempted_at": stamp,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "accepted_candidate" if accepted else metadata.get("page_state", "failed"),
        "candidate_dir": str(candidate_dir.relative_to(run_dir)).replace("\\", "/") if accepted else None,
        "diagnostic_dir": None if accepted else str(attempt.relative_to(run_dir)).replace("\\", "/"),
        "content_valid": bool(metadata.get("content_valid")),
        "offline_ok": bool((mhtml_replay or {}).get("ok")),
        "portable_html_ok": bool((singlefile_replay or {}).get("ok")),
        "visual_similarity_ok": bool((visual_comparison or {}).get("ok")),
    }
    write_attempt_phase(run_dir, attempt_record, "terminal")
    terminal_written = True
    atexit.unregister(mark_aborted)
    print(json.dumps({
        "accepted": accepted, "candidate_id": args.candidate_id, "path": str(final_location),
        "page_state": metadata.get("page_state"), "mhtml_offline_ok": bool((mhtml_replay or {}).get("ok")),
        "portable_html_ok": bool((singlefile_replay or {}).get("ok")),
        "visual_similarity": (visual_comparison or {}).get("similarity"),
    }, ensure_ascii=False, indent=2))
    if not accepted:
        raise SystemExit(3 if metadata else 2)


if __name__ == "__main__":
    main()
