#!/usr/bin/env python3

"""Capture only direct product pages retained by the official-API image prefilter."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edge_profile import (
    browser_profile_process_is_running,
    is_default_browser_user_data,
    resolve_browser_selection,
    resolve_profile_directory,
)
from process_utils import run_bounded


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def artifact(path: Path, base: Path) -> dict:
    return {
        "path": str(path.relative_to(base)).replace("\\", "/"),
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:80]


def move_diagnostic(output_dir: Path, diagnostics_root: Path) -> Path:
    diagnostics_root.mkdir(parents=True, exist_ok=True)
    destination = diagnostics_root / output_dir.name
    if destination.exists():
        destination = diagnostics_root / f"{output_dir.name}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
    resolved_output = output_dir.resolve()
    resolved_destination = destination.resolve()
    if resolved_output.parent != (output_dir.parent).resolve() or resolved_destination.parent != diagnostics_root.resolve():
        raise ValueError("Refusing to move a capture outside the intended run directories")
    shutil.move(str(output_dir), str(destination))
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture API-shortlisted direct product pages")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--shortlist")
    parser.add_argument("--browser", choices=("auto", "edge", "chrome"), default="auto")
    parser.add_argument("--browser-executable")
    parser.add_argument("--browser-user-data", help="Optional dedicated non-default profile; omit for isolated headless capture")
    parser.add_argument("--edge-executable", help="Backward-compatible alias for --browser edge --browser-executable")
    parser.add_argument("--edge-user-data", help="Backward-compatible alias for --browser edge --browser-user-data")
    parser.add_argument("--profile-directory", default="auto")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument("--visual-threshold", type=float, default=0.46)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    shortlist_path = Path(args.shortlist).resolve() if args.shortlist else run_dir / "discovery" / "api-visual-shortlist.json"
    shortlist = read_json(shortlist_path)
    if not isinstance(shortlist, dict):
        raise FileNotFoundError(f"API visual shortlist not found: {shortlist_path}")
    candidates = [item for item in shortlist.get("items") or [] if item.get("retained") and item.get("url")]
    candidates = candidates[:max(1, min(20, args.limit))]
    if not candidates:
        raise ValueError("API visual shortlist contains no retained direct product page")

    browser_request = "edge" if args.edge_executable or args.edge_user_data else args.browser
    automatic_selection = browser_request == "auto"
    browser, browser_executable = resolve_browser_selection(
        browser_request, args.browser_executable or args.edge_executable,
    )
    if not browser_executable.is_file():
        raise FileNotFoundError(f"{browser.title()} executable not found: {browser_executable}")
    configured_user_data = args.browser_user_data or args.edge_user_data
    user_data = Path(configured_user_data).resolve() if configured_user_data else None
    profile_directory = None
    if user_data:
        if user_data == run_dir or user_data.is_relative_to(run_dir):
            raise ValueError("Browser user data must stay outside RUN_DIR")
        if is_default_browser_user_data(browser, user_data):
            raise ValueError(f"The system default {browser.title()} profile cannot be automated")
        if not user_data.is_dir():
            raise FileNotFoundError(f"Dedicated {browser.title()} user data not found: {user_data}")
        if browser_profile_process_is_running(browser, user_data):
            raise RuntimeError(f"The dedicated trademark {browser.title()} profile is still running. Close it before capture.")
        profile_directory = resolve_profile_directory(user_data, args.profile_directory)
    elif args.headed:
        raise ValueError("--headed requires --browser-user-data pointing to the dedicated trademark profile")

    config = read_json(run_dir / "run-config.json", {}) or {}
    trademark = config.get("trademark") or {}
    expected = "|||".join(
        str(value).strip() for value in (
            trademark.get("name"),
            *(trademark.get("goods_services") or []),
        ) if str(value or "").strip()
    )
    scripts = Path(__file__).resolve().parent
    node_script = scripts / "capture-page-to-pdf.mjs"
    output_root = run_dir / "capture" / "visual-first"
    diagnostics_root = run_dir / "capture-diagnostics" / "direct-product-pages"
    output_root.mkdir(parents=True, exist_ok=True)
    attempts = []
    accepted = 0
    started_at = datetime.now(timezone.utc).isoformat()

    for index, item in enumerate(candidates, start=1):
        cid = safe_id(str(item.get("candidate_id") or f"API-{index:03d}"))
        output_dir = output_root / cid
        command = [
            "node", str(node_script), "--url", str(item["url"]),
            "--output-dir", str(output_dir), "--source-id", cid,
            "--browser-executable", str(browser_executable),
            "--timeout-ms", str(max(10000, min(90000, args.timeout_ms))),
            "--capture-images", "--allow-image-only",
        ]
        if expected:
            command.extend(["--expected-text-any", expected])
        if user_data:
            command.extend(["--user-data-dir", str(user_data), "--profile-directory", str(profile_directory)])
        if args.headed:
            command.append("--headed")
        try:
            completed = run_bounded(
                command,
                timeout=max(75, args.timeout_ms / 1000 + 90),
            )
            metadata = read_json(output_dir / "metadata.json", {}) or {}
            valid = metadata.get("content_valid") is True
            singlefile_ok = False
            if valid and metadata.get("final_url"):
                singlefile_path = output_dir / "page.singlefile.html"
                singlefile_command = [
                    sys.executable, str(scripts / "archive-singlefile.py"),
                    "--url", str(metadata["final_url"]), "--output", str(singlefile_path),
                    "--browser-executable", str(browser_executable), "--timeout-ms", str(max(45000, args.timeout_ms)),
                ]
                if user_data:
                    singlefile_command.extend(["--user-data-dir", str(user_data), "--profile-directory", str(profile_directory)])
                if args.headed:
                    singlefile_command.append("--headed")
                archive_timeout_ms = max(45_000, args.timeout_ms)
                singlefile = run_bounded(
                    singlefile_command,
                    timeout=max(180, 2 * archive_timeout_ms / 1000 + 75),
                )
                if singlefile.returncode == 0 and singlefile_path.is_file() and singlefile_path.stat().st_size >= 1000:
                    validation_path = output_dir / "singlefile-validation.json"
                    offline = run_bounded([
                        "node", str(scripts / "verify-offline-page.mjs"),
                        "--html", str(singlefile_path), "--output", str(validation_path),
                        "--screenshot", str(output_dir / "singlefile-offline.png"),
                        "--online-text", str(output_dir / "body-text.txt"),
                        "--browser-executable", str(browser_executable),
                    ], timeout=120)
                    offline_data = read_json(validation_path, {}) or {}
                    singlefile_ok = offline.returncode == 0 and bool(offline_data.get("ok"))
                    metadata["portable_html"] = "page.singlefile.html"
                    metadata["portable_html_ok"] = singlefile_ok
                    metadata.setdefault("artifacts", {})["portable_html"] = artifact(singlefile_path, output_dir)
                    write_json(output_dir / "metadata.json", metadata)
            pdf_created = (output_dir / "page.pdf").is_file()
            if valid and pdf_created:
                accepted += 1
                final_dir = output_dir
            else:
                final_dir = move_diagnostic(output_dir, diagnostics_root) if output_dir.exists() else diagnostics_root / cid
            attempts.append({
                "candidate_id": cid,
                "platform": item.get("platform"),
                "api_name": item.get("api_name"),
                "url": item.get("url"),
                "prefilter_status": item.get("status"),
                "prefilter_visual_score": item.get("visual_score"),
                "return_code": completed.returncode,
                "page_state": metadata.get("page_state"),
                "content_valid": valid,
                "pdf_created": pdf_created,
                "singlefile_offline_ok": singlefile_ok,
                "output_dir": str(final_dir.relative_to(run_dir)).replace("\\", "/"),
                "stdout": (completed.stdout or "")[-1200:],
                "stderr": (completed.stderr or "")[-1200:],
            })
        except Exception as exc:
            final_dir = move_diagnostic(output_dir, diagnostics_root) if output_dir.exists() else diagnostics_root / cid
            attempts.append({
                "candidate_id": cid, "platform": item.get("platform"), "url": item.get("url"),
                "page_state": "subprocess_error", "content_valid": False, "pdf_created": False,
                "output_dir": str(final_dir.relative_to(run_dir)).replace("\\", "/"), "error": str(exc),
            })

    visual_completed = run_bounded([
        sys.executable, str(scripts / "retain-visual-mark-matches.py"),
        "--run-dir", str(run_dir), "--threshold", str(args.visual_threshold),
    ], timeout=180)
    visual = read_json(run_dir / "visual-match-results.json", {}) or {}
    summary = {
        "schema_version": "1.0",
        "record_type": "official_api_shortlist_capture",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "discovery_channel": "official_api",
        "browser": browser,
        "browser_executable": str(browser_executable),
        "browser_selection_policy": "edge_then_chrome",
        "browser_fallback_used": automatic_selection and browser == "chrome",
        "headed": bool(args.headed),
        "profile_kind": "dedicated_non_default" if user_data else "isolated_ephemeral",
        "cookie_exported": False,
        "attempted_count": len(attempts),
        "accepted_count": accepted,
        "failed_or_blocked_count": len(attempts) - accepted,
        "visual_retained_count": int(visual.get("retained_count") or 0),
        "visual_step_return_code": visual_completed.returncode,
        "status": "complete" if accepted == len(attempts) else ("partial" if accepted else "failed"),
        "attempts": attempts,
    }
    summary_path = output_root / "capture-summary.json"
    write_json(summary_path, summary)
    print(json.dumps({**summary, "summary": str(summary_path)}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if accepted else 4)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
