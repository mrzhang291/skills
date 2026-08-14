#!/usr/bin/env python3

"""Cheap, headless-only validation of one ranked destination page.

The probe is deliberately not an evidence capture.  It persists only a small
viewport image, visible body text and classification metadata so the Quick
profile can avoid building PDF/MHTML packages for blocked or irrelevant URLs.
"""

from __future__ import annotations

import argparse
import atexit
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

from discovery_limits import positive_budget
from run_lock import RunFileLock, atomic_write_jsonl
from process_utils import run_bounded
from url_utils import is_search_result_url, normalize_url, site_key


FORBIDDEN_BROWSER_STATE_FLAGS = {
    "--headed",
    "--storage-state",
    "--user-data-dir",
    "--cdp-endpoint",
    "--save-storage-state",
    "--wait-for-unblock-ms",
}
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


def write_record(run_dir: Path, record: dict, *, require_started: bool) -> None:
    path = run_dir / "probe-attempts.jsonl"
    with RunFileLock(run_dir, "probe-attempts"):
        records = read_attempts(run_dir)
        previous = next((item for item in records if item.get("probe_id") == record.get("probe_id")), None)
        if require_started and (not previous or previous.get("status") != "started"):
            raise ValueError("Terminal probe state has no matching started record")
        if previous:
            if normalize_url(previous.get("url") or "") != normalize_url(record.get("url") or ""):
                raise ValueError("A probe-id cannot be reused for a different normalized URL")
            records = [item for item in records if item.get("probe_id") != record.get("probe_id")]
        records.append(record)
        records.sort(key=lambda item: str(item.get("probe_id") or ""))
        atomic_write_jsonl(path, records)


def next_probe_id(records: list[dict]) -> str:
    used = {str(item.get("probe_id") or "") for item in records}
    number = 1
    while f"P{number:03d}" in used:
        number += 1
    return f"P{number:03d}"


def reject_browser_state_flags(argv: list[str]) -> None:
    for argument in argv:
        flag = argument.split("=", 1)[0]
        if flag in FORBIDDEN_BROWSER_STATE_FLAGS:
            raise ValueError(f"{flag} is forbidden in Quick probe mode")


def parse_args(argv: list[str]) -> argparse.Namespace:
    reject_browser_state_flags(argv)
    parser = argparse.ArgumentParser(description="Probe one Quick-profile frontier URL without making evidence files")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--frontier-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--expected-text-any")
    parser.add_argument("--browser-executable")
    parser.add_argument("--timeout-ms", type=int, default=15_000)
    parser.add_argument("--wall-clock-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.timeout_ms < 1:
        parser.error("--timeout-ms must be positive")
    if args.wall_clock_timeout_seconds <= 0:
        parser.error("--wall-clock-timeout-seconds must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    run_dir = Path(args.run_dir).resolve()
    config = read_json(run_dir / "run-config.json")
    if config.get("execution_profile") != "quick":
        raise ValueError("probe-target-page.py is available only when execution_profile=quick")

    normalized = normalize_url(args.url)
    if not normalized:
        raise ValueError("URL is not a valid HTTP(S) target")
    if is_search_result_url(normalized):
        raise ValueError("Search-engine and platform search pages cannot be probed")

    frontier = read_json(run_dir / "discovery" / "url-frontier.json")
    frontier_item = next((
        item for item in (frontier.get("items") or [])
        if item.get("target_id") == args.frontier_id
        and normalize_url(str(item.get("normalized_url") or item.get("url") or "")) == normalized
    ), None)
    if not frontier_item:
        raise ValueError("frontier-id and normalized URL do not identify the same URL-frontier item")

    max_attempts = positive_budget(config, "max_probe_attempts", 4)
    fallback_site_limit = positive_budget(config, "max_pages_per_domain", 3)
    per_site_limit = positive_budget(
        config,
        "max_probe_pages_per_site",
        fallback_site_limit,
    )
    requested_site = site_key(normalized)
    with RunFileLock(run_dir, "probe-attempts"):
        attempts = read_attempts(run_dir)
        prior_url = next((
            item for item in attempts
            if normalize_url(str(item.get("url") or "")) == normalized
        ), None)
        if prior_url:
            raise ValueError(f"URL was already probed as {prior_url.get('probe_id')}")
        if len(attempts) >= max_attempts:
            raise ValueError(f"max_probe_attempts={max_attempts} reached")
        site_attempts = [item for item in attempts if site_key(str(item.get("url") or "")) == requested_site]
        if len(site_attempts) >= per_site_limit:
            raise ValueError(
                f"Quick probe per-site limit={per_site_limit} reached for site_key={requested_site}"
            )

        probe_id = next_probe_id(attempts)
        probe_dir = (run_dir / "probe-pages" / probe_id).resolve()
        if probe_dir.exists():
            raise FileExistsError(f"Probe directory already exists: {probe_dir}")
        probe_dir.mkdir(parents=True, exist_ok=False)
        started = {
            "schema_version": "1.0",
            "record_type": "quick_probe_attempt",
            "probe_id": probe_id,
            "frontier_id": args.frontier_id,
            "url": normalized,
            "requested_normalized_url": normalized,
            "site_key": requested_site,
            "site_attempt_number": len(site_attempts) + 1,
            "per_site_limit": per_site_limit,
            "rank": frontier_item.get("rank"),
            "status": "started",
            "started_at": utc_now(),
            "completed_at": None,
            "probe_dir": str(probe_dir.relative_to(run_dir)).replace("\\", "/"),
            "content_valid": False,
        }
        attempts.append(started)
        attempts.sort(key=lambda item: str(item.get("probe_id") or ""))
        atomic_write_jsonl(run_dir / "probe-attempts.jsonl", attempts)

    terminal_written = False

    def terminal_from_failure(status: str, message: str) -> None:
        nonlocal terminal_written
        if terminal_written:
            return
        terminal = dict(started)
        terminal.update({
            "status": status,
            "completed_at": utc_now(),
            "content_valid": False,
            "page_state": status,
            "error": message,
        })
        try:
            write_record(run_dir, terminal, require_started=True)
            terminal_written = True
        except Exception:
            pass

    atexit.register(terminal_from_failure, "aborted", "probe wrapper exited before a terminal state was written")
    capture_script = Path(__file__).resolve().with_name("capture-page-to-pdf.mjs")
    command = [
        "node", str(capture_script), "--probe-only",
        "--url", normalized, "--output-dir", str(probe_dir), "--source-id", probe_id,
        "--timeout-ms", str(args.timeout_ms), "--max-scroll-steps", "8", "--settle-ms", "500",
        "--allow-image-only",
    ]
    if args.expected_text_any:
        command.extend(["--expected-text-any", args.expected_text_any])
    if args.browser_executable:
        command.extend(["--browser-executable", args.browser_executable])

    started_monotonic = time.monotonic()
    timed_out = False
    completed = run_bounded(command, timeout=args.wall_clock_timeout_seconds)
    returncode = completed.returncode
    timed_out = returncode == 124
    stdout_tail = (completed.stdout or "")[-2000:]
    stderr_tail = (completed.stderr or "")[-2000:]
    elapsed_ms = round((time.monotonic() - started_monotonic) * 1000)

    metadata_path = probe_dir / "metadata.json"
    if metadata_path.is_file():
        try:
            metadata = read_json(metadata_path)
        except Exception as error:
            metadata = {"errors": [{"stage": "metadata_parse", "message": str(error)}]}
    else:
        metadata = {"errors": []}

    final_url = normalize_url(str(metadata.get("final_url") or ""))
    final_is_search = bool(final_url and is_search_result_url(final_url))
    required_artifacts = all((probe_dir / name).is_file() for name in ("body-text.txt", "probe.png"))
    content_valid = bool(
        not timed_out
        and returncode == 0
        and metadata.get("content_valid") is True
        and final_url
        and not final_is_search
        and required_artifacts
    )
    if timed_out:
        page_state = "timed_out"
    elif final_is_search:
        page_state = "search_result_page"
    elif not required_artifacts:
        page_state = "probe_artifact_failed"
    else:
        page_state = str(metadata.get("page_state") or "failed")

    metadata.update({
        "schema_version": str(metadata.get("schema_version") or "2.0"),
        "record_type": "target_page_probe",
        "capture_mode": "probe",
        "probe_id": probe_id,
        "frontier_id": args.frontier_id,
        "requested_normalized_url": normalized,
        "page_state": page_state,
        "content_valid": content_valid,
        "search_result_page": final_is_search or bool(metadata.get("search_result_page")),
        "wrapper_returncode": returncode,
        "wrapper_timed_out": timed_out,
        "wrapper_elapsed_ms": elapsed_ms,
        "per_site_limit": per_site_limit,
    })
    metadata.setdefault("errors", [])
    if timed_out:
        metadata["errors"].append({"stage": "wall_clock_timeout", "message": f"exceeded {args.wall_clock_timeout_seconds} seconds"})
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    terminal = dict(started)
    terminal.update({
        "status": "content_valid" if content_valid else page_state,
        "completed_at": utc_now(),
        "content_valid": content_valid,
        "page_state": page_state,
        "final_url": final_url,
        "final_normalized_url": final_url,
        "returncode": returncode,
        "timed_out": timed_out,
        "elapsed_ms": elapsed_ms,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    })
    write_record(run_dir, terminal, require_started=True)
    terminal_written = True
    atexit.unregister(terminal_from_failure)
    print(json.dumps({
        "probed": True,
        "probe_id": probe_id,
        "frontier_id": args.frontier_id,
        "url": normalized,
        "probe_dir": str(probe_dir),
        "content_valid": content_valid,
        "page_state": page_state,
        "timed_out": timed_out,
        "elapsed_ms": elapsed_ms,
        "per_site_limit": per_site_limit,
    }, ensure_ascii=False, indent=2))
    return 0 if content_valid else 3


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, FileExistsError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
