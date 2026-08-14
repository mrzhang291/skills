#!/usr/bin/env python3

"""Capture sales candidates after the user confirms manual Chrome/Edge login."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edge_profile import (
    browser_profile_process_is_running,
    dedicated_browser_user_data,
    is_default_browser_user_data,
    resolve_browser_selection,
    resolve_profile_directory,
)
from process_utils import run_bounded
from audit_cherrystudio_run import artifact_record_matches, valid_image, validate_pdf


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def artifact(path: Path, base: Path) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": str(path.relative_to(base)).replace("\\", "/"),
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }


def reusable_capture(output_dir: Path, metadata: dict, requested_url: str) -> bool:
    if metadata.get("content_valid") is not True or str(metadata.get("requested_url") or "") != requested_url:
        return False
    visual = metadata.get("visual_capture") or {}
    if not (
        visual.get("strategy") == "viewport_tile_stitch_v1"
        and visual.get("acceptable") is True
        and visual.get("output_created") is True
        and metadata.get("image_load_complete") is True
    ):
        return False
    required = {
        "raw_html": "response.html", "body_text": "body-text.txt",
        "links_index": "page-links.json", "rendered_dom": "rendered-dom.html",
        "fullpage": "fullpage.png", "mhtml": "page.mhtml", "pdf": "page.pdf",
    }
    artifacts = metadata.get("artifacts") or {}
    for key, filename in required.items():
        target = output_dir / filename
        valid = valid_image(target) if key == "fullpage" else target.is_file() and target.stat().st_size > 0
        if not valid or not artifact_record_matches(output_dir, artifacts.get(key), target):
            return False
    return validate_pdf(output_dir / "page.pdf")[0]


def build_expected(config: dict) -> str:
    trademark = config.get("trademark") or {}
    values = [
        trademark.get("name"),
        trademark.get("registration_number"),
        trademark.get("owner"),
        *(trademark.get("goods_services") or []),
    ]
    return "|||".join(str(value).strip() for value in values if str(value or "").strip())


def validate_cdp_endpoint(endpoint: str) -> str:
    endpoint = str(endpoint or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("cdp-endpoint must be an HTTP loopback URL")
    try:
        with urlopen(endpoint + "/json/version", timeout=2) as response:
            value = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"CDP endpoint is unavailable: {exc}") from exc
    if not value.get("webSocketDebuggerUrl"):
        raise RuntimeError("CDP endpoint did not return a browser WebSocket URL")
    return endpoint


def capture(
    run_dir: Path,
    browser_user_data: Path,
    browser_executable: Path,
    profile_directory: str,
    platforms: list[str] | None,
    limit: int,
    timeout_ms: int,
    candidate_ids: list[str] | None = None,
    request_delay_min_sec: float = 15.0,
    request_delay_max_sec: float = 25.0,
    visual_threshold: float = 0.46,
    browser: str = "chrome",
    cdp_endpoint: str | None = None,
    allow_diagnostic_detail_skips: bool = False,
) -> tuple[Path, dict, int]:
    run_dir = run_dir.resolve()
    browser = str(browser or "chrome").strip().casefold()
    browser_user_data = browser_user_data.resolve()
    browser_executable = browser_executable.resolve()
    label = "Google Chrome" if browser == "chrome" else "Microsoft Edge"
    if browser_user_data == run_dir or run_dir in browser_user_data.parents:
        raise ValueError("Browser user data must be outside RUN_DIR")
    if is_default_browser_user_data(browser, browser_user_data):
        raise ValueError(f"The system default {label} profile cannot be automated; use the dedicated trademark profile")
    profile_running = browser_profile_process_is_running(browser, browser_user_data)
    if cdp_endpoint:
        cdp_endpoint = validate_cdp_endpoint(cdp_endpoint)
        if not profile_running:
            raise RuntimeError(f"The dedicated trademark {label} profile is not running")
    elif profile_running:
        raise RuntimeError(f"The dedicated trademark {label} profile is still running. Close that profile before resuming capture.")
    if not browser_executable.is_file():
        raise FileNotFoundError(f"{label} executable not found: {browser_executable}")
    if not browser_user_data.is_dir():
        raise FileNotFoundError(f"Browser user data directory not found: {browser_user_data}")
    profile_directory = resolve_profile_directory(browser_user_data, profile_directory)

    config = read_json(run_dir / "run-config.json")
    state_path = run_dir / "discovery" / "sales-workflow-state.json"
    state = read_json(state_path)
    results = read_json(run_dir / "discovery" / "sales-platform-results.json")
    if not isinstance(config, dict) or not isinstance(state, dict) or not isinstance(results, dict):
        raise FileNotFoundError("run config, sales workflow state, and sales platform results are required")
    allowed_capture_phases = {"awaiting_manual_login", "stage_b_capture_and_packaging_running"}
    if state.get("phase") not in allowed_capture_phases:
        raise ValueError(f"Workflow is not ready for capture: {state.get('phase')!r}")

    candidates = [item for item in results.get("items") or [] if item.get("url")]
    if platforms:
        candidates = [item for item in candidates if item.get("platform") in set(platforms)]
    if candidate_ids:
        selected_ids = set(candidate_ids)
        candidates = [item for item in candidates if item.get("candidate_id") in selected_ids]
    candidates = candidates[:max(1, min(20, limit))]
    if not candidates:
        assisted = read_json(run_dir / "discovery" / "assisted-sales-results.json", {}) or {}
        platform_runs = assisted.get("platform_runs") or []
        if candidate_ids or not platform_runs or any(item.get("delivery_eligible") is not True for item in platform_runs):
            raise ValueError(
                "No sales candidates are available and the logged-in search matrix is not fully delivery-eligible"
            )

    scripts_dir = Path(__file__).resolve().parent
    node_script = scripts_dir / "capture-page-to-pdf.mjs"
    output_root = run_dir / "capture" / "sales-after-login"
    output_root.mkdir(parents=True, exist_ok=True)
    expected = build_expected(config)
    attempts = []
    accepted = 0
    started_at = datetime.now(timezone.utc).isoformat()
    for index, item in enumerate(candidates, start=1):
        if index > 1:
            time.sleep(random.uniform(
                max(0.0, request_delay_min_sec),
                max(request_delay_min_sec, request_delay_max_sec),
            ))
        candidate_id = str(item.get("candidate_id") or f"SP{index:03d}")
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", candidate_id)
        output_dir = output_root / safe_id
        existing_metadata = read_json(output_dir / "metadata.json", {}) or {}
        if reusable_capture(output_dir, existing_metadata, str(item["url"])):
            accepted += 1
            attempts.append({
                "candidate_id": candidate_id,
                "platform": item.get("platform"),
                "url": item.get("url"),
                "return_code": 0,
                "page_state": existing_metadata.get("page_state"),
                "content_valid": True,
                "pdf_created": True,
                "singlefile_created": bool((output_dir / "page.singlefile.html").is_file()),
                "singlefile_offline_ok": bool(existing_metadata.get("portable_html_ok")),
                "output_dir": str(output_dir.relative_to(run_dir)).replace("\\", "/"),
                "reused_existing_valid_capture": True,
                "stdout": "existing valid candidate capture reused",
                "stderr": "",
            })
            continue
        if output_dir.exists():
            diagnostic_root = run_dir / "capture-diagnostics" / "sales-after-login"
            diagnostic_root.mkdir(parents=True, exist_ok=True)
            suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            shutil.move(str(output_dir), str(diagnostic_root / f"{safe_id}-retry-{suffix}"))
        command = [
            "node", str(node_script),
            "--url", str(item["url"]),
            "--output-dir", str(output_dir),
            "--source-id", safe_id,
            "--headed",
            "--user-data-dir", str(browser_user_data),
            "--profile-directory", profile_directory,
            "--browser-executable", str(browser_executable),
            "--timeout-ms", str(timeout_ms),
            "--capture-images",
            "--allow-image-only",
        ]
        if cdp_endpoint:
            command.extend(["--cdp-endpoint", cdp_endpoint])
        if expected:
            command.extend(["--expected-text-any", expected])
        try:
            completed = run_bounded(
                command,
                timeout=max(60, timeout_ms / 1000 + 90),
            )
            metadata = read_json(output_dir / "metadata.json", {}) or {}
            singlefile_created = False
            singlefile_ok = False
            singlefile_stdout = ""
            singlefile_stderr = ""
            if metadata.get("content_valid") is True and metadata.get("final_url") and not cdp_endpoint:
                singlefile_path = output_dir / "page.singlefile.html"
                singlefile_command = [
                    sys.executable, str(scripts_dir / "archive-singlefile.py"),
                    "--url", str(metadata["final_url"]), "--output", str(singlefile_path),
                    "--browser-executable", str(browser_executable),
                    "--user-data-dir", str(browser_user_data),
                    "--profile-directory", profile_directory,
                    "--headed", "--timeout-ms", str(max(45_000, timeout_ms)),
                ]
                archive_timeout_ms = max(45_000, timeout_ms)
                singlefile = run_bounded(
                    singlefile_command,
                    timeout=max(180, 2 * archive_timeout_ms / 1000 + 75),
                )
                singlefile_stdout = (singlefile.stdout or "")[-1500:]
                singlefile_stderr = (singlefile.stderr or "")[-1500:]
                singlefile_created = singlefile_path.is_file() and singlefile_path.stat().st_size >= 1000
                if singlefile_created:
                    offline_path = output_dir / "singlefile-validation.json"
                    offline_command = [
                        "node", str(scripts_dir / "verify-offline-page.mjs"),
                        "--html", str(singlefile_path), "--output", str(offline_path),
                        "--screenshot", str(output_dir / "singlefile-offline.png"),
                        "--online-text", str(output_dir / "body-text.txt"),
                        "--browser-executable", str(browser_executable),
                    ]
                    offline = run_bounded(
                        offline_command, timeout=120,
                    )
                    offline_data = read_json(offline_path, {}) or {}
                    singlefile_ok = offline.returncode == 0 and bool(offline_data.get("ok"))
                    metadata["portable_html"] = "page.singlefile.html"
                    metadata["portable_html_ok"] = singlefile_ok
                    artifacts = metadata.setdefault("artifacts", {})
                    artifacts["portable_html"] = artifact(singlefile_path, output_dir)
                    raw_path = output_dir / "page.singlefile.raw.html"
                    if raw_path.is_file():
                        artifacts["singlefile_original"] = artifact(raw_path, output_dir)
                    if offline_path.is_file():
                        artifacts["singlefile_validation"] = artifact(offline_path, output_dir)
                    offline_shot = output_dir / "singlefile-offline.png"
                    if offline_shot.is_file():
                        artifacts["singlefile_offline"] = artifact(offline_shot, output_dir)
                    write_json(output_dir / "metadata.json", metadata)
            elif cdp_endpoint:
                singlefile_stderr = "SingleFile CLI skipped in attached-browser mode; MHTML/rendered DOM/PDF remain captured"
            pdf_created = validate_pdf(output_dir / "page.pdf")[0]
            raw_content_valid = bool(metadata.get("content_valid"))
            content_valid = reusable_capture(output_dir, metadata, str(item["url"]))
            accepted += int(content_valid)
            attempts.append({
                "candidate_id": candidate_id,
                "platform": item.get("platform"),
                "url": item.get("url"),
                "return_code": completed.returncode,
                "page_state": metadata.get("page_state"),
                "content_valid": content_valid,
                "raw_content_valid": raw_content_valid,
                "pdf_created": pdf_created,
                "singlefile_created": singlefile_created,
                "singlefile_offline_ok": singlefile_ok,
                "output_dir": str(output_dir.relative_to(run_dir)).replace("\\", "/"),
                "stdout": (completed.stdout or "")[-1500:],
                "stderr": (completed.stderr or "")[-1500:],
                "singlefile_stdout": singlefile_stdout,
                "singlefile_stderr": singlefile_stderr,
            })
            if not content_valid and output_dir.exists():
                diagnostic_root = run_dir / "capture-diagnostics" / "sales-after-login"
                diagnostic_root.mkdir(parents=True, exist_ok=True)
                suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                diagnostic_dir = diagnostic_root / f"{safe_id}-{suffix}"
                shutil.move(str(output_dir), str(diagnostic_dir))
                attempts[-1]["output_dir"] = str(diagnostic_dir.relative_to(run_dir)).replace("\\", "/")
                attempts[-1]["diagnostic_only"] = True
        except Exception as exc:
            attempts.append({
                "candidate_id": candidate_id,
                "platform": item.get("platform"),
                "url": item.get("url"),
                "return_code": None,
                "page_state": "subprocess_error",
                "content_valid": False,
                "pdf_created": False,
                "output_dir": str(output_dir.relative_to(run_dir)).replace("\\", "/"),
                "error": str(exc),
            })
            if output_dir.exists():
                diagnostic_root = run_dir / "capture-diagnostics" / "sales-after-login"
                diagnostic_root.mkdir(parents=True, exist_ok=True)
                suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                diagnostic_dir = diagnostic_root / f"{safe_id}-{suffix}"
                shutil.move(str(output_dir), str(diagnostic_dir))
                attempts[-1]["output_dir"] = str(diagnostic_dir.relative_to(run_dir)).replace("\\", "/")
                attempts[-1]["diagnostic_only"] = True

    visual_match = {
        "status": "not_run",
        "retained_count": 0,
        "visual_near_match_count": 0,
        "text_review_count": 0,
        "output": None,
    }
    qcc_reference = run_dir / "reference" / "qcc-reference.json"
    if qcc_reference.is_file():
        visual_script = scripts_dir / "retain-visual-mark-matches.py"
        visual_completed = run_bounded(
            [
                sys.executable, str(visual_script), "--run-dir", str(run_dir),
                "--threshold", str(visual_threshold),
            ],
            timeout=120,
        )
        visual_result_path = run_dir / "visual-match-results.json"
        visual_result = read_json(visual_result_path, {}) or {}
        visual_match = {
            "status": "complete" if visual_completed.returncode == 0 else "failed",
            "retained_count": int(visual_result.get("retained_count") or 0),
            "visual_near_match_count": int(visual_result.get("visual_near_match_count") or 0),
            "text_review_count": int(visual_result.get("text_review_count") or 0),
            "output": str(visual_result_path.relative_to(run_dir)).replace("\\", "/") if visual_result_path.is_file() else None,
            "stdout": (visual_completed.stdout or "")[-1500:],
            "stderr": (visual_completed.stderr or "")[-1500:],
        }

    now = datetime.now(timezone.utc).isoformat()
    visual_match_required = qcc_reference.is_file()
    visual_match_complete = (not visual_match_required) or visual_match.get("status") == "complete"
    diagnostic_skip_count = sum(1 for item in attempts if item.get("diagnostic_only") is True)
    capture_complete = visual_match_complete and (
        accepted == len(attempts)
        or allow_diagnostic_detail_skips
        and accepted + diagnostic_skip_count == len(attempts)
    )
    capture_status = "complete" if capture_complete else ("partial" if accepted else "failed")
    summary = {
        "schema_version": "1.0",
        "record_type": "sales_platform_post_login_capture",
        "run_id": config.get("run_id"),
        "started_at": started_at,
        "finished_at": now,
        "browser": browser,
        "browser_executable": str(browser_executable),
        "browser_handoff_mode": "attach" if cdp_endpoint else "restart",
        "cdp_endpoint_loopback": bool(cdp_endpoint),
        "profile_kind": "dedicated_non_default",
        "browser_user_data": str(browser_user_data),
        "edge_user_data": str(browser_user_data) if browser == "edge" else None,
        "profile_directory": profile_directory,
        "cookie_exported": False,
        "attempted_count": len(attempts),
        "accepted_count": accepted,
        "detail_capture_required": not allow_diagnostic_detail_skips,
        "diagnostic_detail_skip_count": diagnostic_skip_count,
        "visual_match": visual_match,
        "visual_match_required": visual_match_required,
        "visual_match_complete": visual_match_complete,
        "status": capture_status,
        "attempts": attempts,
    }
    summary_path = output_root / "capture-summary.json"
    write_json(summary_path, summary)
    state.update({
        "phase": "capture_complete" if capture_complete else "capture_failed",
        "updated_at": now,
        "manual_login_confirmed": True,
        "capture_summary": str(summary_path.relative_to(run_dir)).replace("\\", "/"),
        "capture_status": summary["status"],
    })
    write_json(state_path, state)
    return summary_path, summary, 0 if capture_complete else 4


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture sales pages using an already logged-in Chrome/Edge profile")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--browser", choices=("auto", "edge", "chrome"), default="auto")
    parser.add_argument("--browser-user-data")
    parser.add_argument("--browser-executable")
    parser.add_argument("--edge-user-data")
    parser.add_argument("--edge-executable")
    parser.add_argument("--cdp-endpoint")
    parser.add_argument("--profile-directory", default="auto")
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--candidate-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument("--request-delay-min-sec", type=float, default=15.0)
    parser.add_argument("--request-delay-max-sec", type=float, default=25.0)
    parser.add_argument("--visual-threshold", type=float, default=0.46)
    parser.add_argument(
        "--allow-diagnostic-detail-skips", action="store_true",
        help="Treat blocked/invalid optional detail pages as diagnostics after the search matrices are complete",
    )
    args = parser.parse_args()
    try:
        run_dir = Path(args.run_dir).resolve()
        state_path = run_dir / "discovery" / "sales-workflow-state.json"
        state = read_json(state_path, {}) or {}
        state_browser = str(state.get("default_browser") or "").strip().casefold()
        if state_path.is_file():
            if state_browser not in {"edge", "chrome"}:
                raise ValueError("Existing workflow state is missing a locked Edge/Chrome browser identity")
            requested_browser = "edge" if args.edge_user_data or args.edge_executable else args.browser
            if requested_browser != "auto" and requested_browser != state_browser:
                raise ValueError("Browser override conflicts with workflow state")
            browser = state_browser
            state_executable = state.get("browser_executable")
            state_user_data = state.get("browser_user_data") or state.get("edge_user_data")
            if not state_executable or not state_user_data:
                raise ValueError("Workflow state is missing browser_executable or browser_user_data")
            explicit_executable = args.browser_executable or args.edge_executable
            explicit_user_data = args.browser_user_data or args.edge_user_data
            if explicit_executable and Path(explicit_executable).resolve() != Path(state_executable).resolve():
                raise ValueError("Browser executable override conflicts with workflow state")
            if explicit_user_data and Path(explicit_user_data).resolve() != Path(state_user_data).resolve():
                raise ValueError("Browser user-data override conflicts with workflow state")
            executable = Path(state_executable).resolve()
            user_data = Path(state_user_data).resolve()
            cdp_endpoint = state.get("cdp_endpoint")
            if args.cdp_endpoint and cdp_endpoint and args.cdp_endpoint.rstrip("/") != str(cdp_endpoint).rstrip("/"):
                raise ValueError("CDP endpoint override conflicts with workflow state")
            cdp_endpoint = cdp_endpoint or args.cdp_endpoint
            state_profile = str(state.get("profile_directory") or "").strip()
            if args.profile_directory != "auto" and state_profile and args.profile_directory != state_profile:
                raise ValueError("Profile-directory override conflicts with workflow state")
            profile_directory = state_profile or args.profile_directory
        else:
            browser_request = "edge" if args.edge_user_data or args.edge_executable else args.browser
            browser, executable = resolve_browser_selection(
                browser_request, args.browser_executable or args.edge_executable,
            )
            user_data = Path(args.browser_user_data or args.edge_user_data or dedicated_browser_user_data(browser))
            cdp_endpoint = args.cdp_endpoint
            profile_directory = args.profile_directory
        summary_path, summary, exit_code = capture(
            run_dir, user_data, executable,
            profile_directory, args.platform or None, args.limit, args.timeout_ms,
            candidate_ids=args.candidate_id or None,
            request_delay_min_sec=args.request_delay_min_sec,
            request_delay_max_sec=args.request_delay_max_sec,
            visual_threshold=args.visual_threshold,
            browser=browser,
            cdp_endpoint=cdp_endpoint,
            allow_diagnostic_detail_skips=args.allow_diagnostic_detail_skips,
        )
        print(json.dumps({
            "capture_after_login": True,
            "status": summary["status"],
            "attempted_count": summary["attempted_count"],
            "accepted_count": summary["accepted_count"],
            "visual_retained_count": summary["visual_match"]["retained_count"],
            "summary": str(summary_path),
        }, ensure_ascii=False, indent=2))
        raise SystemExit(exit_code)
    except Exception as exc:
        print(json.dumps({"capture_after_login": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
