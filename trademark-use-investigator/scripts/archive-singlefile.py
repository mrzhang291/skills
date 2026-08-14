#!/usr/bin/env python3

"""Invoke the unmodified SingleFile CLI as an external archival backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from process_utils import run_bounded, spawn_managed, terminate_process_tree


VERSION = "2.0.83"
MIN_BACKEND_TIMEOUT_MS = 10_000
MIN_SINGLEFILE_WALL_TIMEOUT_SECONDS = 45.0
SINGLEFILE_SHUTDOWN_MARGIN_SECONDS = 30.0


def singlefile_wall_timeout_seconds(timeout_ms: int, wait_for_unblock_ms: int = 0) -> float:
    """Return a wall clock strictly larger than SingleFile's bounded phases."""
    if timeout_ms < 1:
        raise ValueError("timeout-ms must be a positive integer")
    if wait_for_unblock_ms < 0:
        raise ValueError("wait-for-unblock-ms cannot be negative")
    effective_timeout_ms = max(MIN_BACKEND_TIMEOUT_MS, timeout_ms)
    return max(
        MIN_SINGLEFILE_WALL_TIMEOUT_SECONDS,
        2 * effective_timeout_ms / 1000.0
        + wait_for_unblock_ms / 1000.0
        + SINGLEFILE_SHUTDOWN_MARGIN_SECONDS,
    )


def local_singlefile_command() -> list[str] | None:
    node = shutil.which("node")
    entry = Path(__file__).resolve().parents[1] / "node_modules" / "single-file-cli" / "single-file-node.js"
    package = entry.parent / "package.json"
    if not node or not entry.is_file() or not package.is_file():
        return None
    installed = json.loads(package.read_text(encoding="utf-8")).get("version")
    if installed != VERSION:
        raise RuntimeError(f"Expected single-file-cli {VERSION}, found {installed!r}")
    return [node, str(entry)]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_browser(explicit: str | None) -> Path | None:
    if explicit:
        requested = Path(explicit)
        return requested if requested.is_file() else None
    candidates = [
        os.environ.get("EDGE_EXECUTABLE_PATH"),
        str(Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"),
        str(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe"),
        os.environ.get("CHROME_EXECUTABLE_PATH"),
        str(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
    ]
    return next((Path(item) for item in candidates if item and Path(item).is_file()), None)


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def start_browser_server(
    browser: Path,
    *,
    user_data_dir: str | None,
    profile_directory: str | None,
    headed: bool,
    timeout_seconds: float = 15,
) -> tuple[subprocess.Popen, str, Path | None]:
    """Start Chromium without SingleFile CLI's Edge-incompatible --single-process flag."""
    temporary_profile = None
    if user_data_dir:
        profile_root = Path(user_data_dir).resolve()
        profile_root.mkdir(parents=True, exist_ok=True)
    else:
        temporary_profile = Path(tempfile.mkdtemp(prefix="singlefile-browser-")).resolve()
        profile_root = temporary_profile

    port = free_loopback_port()
    command = [
        str(browser),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_root}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--window-size=1440,1000",
    ]
    if "msedge" in browser.name.casefold():
        command.extend([
            "--edge-skip-compat-layer-relaunch",
            "--disable-features=msEdgeUpdateLaunchServicesPreferredVersion",
        ])
    if profile_directory:
        command.append(f"--profile-directory={profile_directory}")
    command.append("--start-maximized" if headed else "--headless=new")
    command.append("about:blank")

    creationflags = 0
    if os.name == "nt" and not headed:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = None
    server = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout_seconds
    try:
        process = spawn_managed(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Browser exited before CDP was ready (code {process.returncode})")
            try:
                with urllib.request.urlopen(f"{server}/json/version", timeout=0.5) as response:
                    if response.status == 200:
                        return process, server, temporary_profile
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        raise RuntimeError("Timed out waiting for the browser CDP endpoint")
    except BaseException as exc:
        cleanup_errors = []
        try:
            stop_browser_server(process)
        except Exception as cleanup_exc:
            cleanup_errors.append(f"browser_cleanup_failed: {cleanup_exc}")
        try:
            if temporary_profile:
                cleanup_temporary_profile(temporary_profile)
        except Exception as cleanup_exc:
            cleanup_errors.append(f"profile_cleanup_failed: {cleanup_exc}")
        if cleanup_errors and hasattr(exc, "add_note"):
            exc.add_note("; ".join(cleanup_errors))
        raise


def stop_browser_server(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    terminate_process_tree(process, grace_seconds=5)


def cleanup_temporary_profile(profile_root: Path) -> None:
    """Wait briefly for Windows browser handles before deleting the temporary profile."""
    last_error = None
    for _ in range(30):
        try:
            shutil.rmtree(profile_root)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    try:
        shutil.rmtree(profile_root)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"temporary_profile_cleanup_failed: {profile_root}: {exc}") from (last_error or exc)


def make_safe_replay(raw_path: Path, output: Path) -> None:
    """Preserve the raw SingleFile separately and create a no-script replay copy."""
    text = raw_path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"<script\b[^>]*>[\s\S]*?</script\s*>", "", text, flags=re.I)
    text = re.sub(r"\s+on[a-z]+\s*=\s*([\"']).*?\1", "", text, flags=re.I | re.S)
    text = re.sub(r"<meta\b[^>]*http-equiv\s*=\s*([\"'])?content-security-policy\1?[^>]*>", "", text, flags=re.I)
    csp = (
        "<meta http-equiv=\"Content-Security-Policy\" "
        "content=\"default-src 'none'; img-src data: blob:; style-src 'unsafe-inline' data:; "
        "font-src data:; media-src data: blob:; frame-src data: blob:; connect-src 'none'; script-src 'none'\">"
    )
    if re.search(r"<head\b[^>]*>", text, flags=re.I):
        text = re.sub(r"(<head\b[^>]*>)", r"\1" + csp, text, count=1, flags=re.I)
    else:
        text = csp + text
    output.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Save one rendered page as a self-contained SingleFile HTML archive")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--browser-executable")
    parser.add_argument("--storage-state", help="Optional Playwright storage-state JSON; cookies are copied to a temporary file only")
    parser.add_argument("--user-data-dir", help="Optional dedicated Edge/Chrome profile directory, reused by SingleFile for the archival load")
    parser.add_argument("--profile-directory", help="Profile name inside --user-data-dir, for example Default or Profile 1")
    parser.add_argument("--headed", action="store_true", help="Show the SingleFile browser window")
    parser.add_argument(
        "--wait-for-unblock-ms", type=int, default=0,
        help="With --headed, keep the page open this long before SingleFile captures it",
    )
    parser.add_argument("--timeout-ms", type=int, default=90000)
    args = parser.parse_args()

    if args.wait_for_unblock_ms < 0:
        raise ValueError("wait-for-unblock-ms cannot be negative")
    if args.timeout_ms < 1:
        raise ValueError("timeout-ms must be a positive integer")
    if args.wait_for_unblock_ms and not args.headed:
        raise ValueError("--wait-for-unblock-ms requires --headed")

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    raw_output = output.with_name(output.stem + ".raw" + output.suffix)
    raw_output.unlink(missing_ok=True)
    browser = find_browser(args.browser_executable)
    singlefile = local_singlefile_command()
    if not singlefile:
        raise RuntimeError(
            f"Local single-file-cli {VERSION} is missing; run npm install --omit=dev --ignore-scripts in the skill directory"
        )
    if not browser:
        raise RuntimeError("Chrome/Edge executable was not found")

    browser_process = None
    browser_profile = None
    browser_server = None
    effective_timeout_ms = max(MIN_BACKEND_TIMEOUT_MS, args.timeout_ms)
    wait_delay_ms = max(1500, args.wait_for_unblock_ms) if args.wait_for_unblock_ms else 1500
    command = [
        *singlefile, args.url, str(raw_output),
        "--browser-width=1440", "--browser-height=1000",
        f"--browser-load-max-time={effective_timeout_ms}",
        f"--browser-capture-max-time={effective_timeout_ms}",
        "--browser-wait-until=networkIdle", f"--browser-wait-delay={wait_delay_ms}",
        "--load-deferred-images=true", "--block-scripts=false",
        "--self-extracting-archive=false", "--compress-content=false",
        "--compress-HTML=false", "--compress-CSS=false",
        "--remove-hidden-elements=false", "--remove-unused-styles=false",
        "--insert-meta-CSP=true", "--save-original-URLs=true", "--resolve-links=true",
        "--filename-conflict-action=overwrite",
    ]
    temporary_cookie_file = None
    completed = None
    operation_error = None
    try:
        browser_process, browser_server, browser_profile = start_browser_server(
            browser,
            user_data_dir=args.user_data_dir,
            profile_directory=args.profile_directory,
            headed=args.headed,
        )
        command.append(f"--browser-server={browser_server}")
        if args.storage_state:
            state_path = Path(args.storage_state).resolve()
            state = json.loads(state_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies") if isinstance(state, dict) else state
            if cookies:
                handle = tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False)
                json.dump(cookies, handle, ensure_ascii=False)
                handle.close()
                temporary_cookie_file = Path(handle.name)
                command.append(f"--browser-cookies-file={temporary_cookie_file}")

        completed = run_bounded(
            command,
            cwd=output.parent,
            timeout=singlefile_wall_timeout_seconds(args.timeout_ms, args.wait_for_unblock_ms),
        )
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        cleanup_errors = []
        try:
            stop_browser_server(browser_process)
        except Exception as exc:
            cleanup_errors.append(f"browser_cleanup_failed: {exc}")
        try:
            if temporary_cookie_file:
                temporary_cookie_file.unlink(missing_ok=True)
        except Exception as exc:
            cleanup_errors.append(f"cookie_cleanup_failed: {exc}")
        try:
            if browser_profile:
                cleanup_temporary_profile(browser_profile)
        except Exception as exc:
            cleanup_errors.append(f"profile_cleanup_failed: {exc}")
        if cleanup_errors and operation_error is not None and hasattr(operation_error, "add_note"):
            operation_error.add_note("; ".join(cleanup_errors))

    if cleanup_errors:
        assert completed is not None
        stderr = "\n".join(value for value in (completed.stderr, *cleanup_errors) if value)
        completed = subprocess.CompletedProcess(completed.args, 125, completed.stdout, stderr)

    assert completed is not None

    if completed.returncode == 0 and raw_output.is_file() and raw_output.stat().st_size >= 1000:
        make_safe_replay(raw_output, output)

    report = {
        "schema_version": "2.0",
        "backend": "single-file-cli",
        "backend_version": VERSION,
        "external_process": True,
        "license": "AGPL-3.0 (external unmodified CLI)",
        "profile_reused": bool(args.user_data_dir),
        "headed": bool(args.headed),
        "wait_for_unblock_ms": args.wait_for_unblock_ms,
        "backend_parameters": {
            "browser_executable_path_applied": True,
            "browser_user_data_dir_argument_applied": bool(args.user_data_dir),
            "browser_profile_directory_argument_applied": bool(args.profile_directory),
            "browser_launch_strategy": "external_cdp",
            "browser_headless": not args.headed,
            "browser_wait_delay_ms": wait_delay_ms,
            "browser_load_max_time_ms": effective_timeout_ms,
            "browser_capture_max_time_ms": effective_timeout_ms,
        },
        "output": str(output),
        "raw_output": str(raw_output),
        "exit_code": completed.returncode,
        "stderr_tail": completed.stderr[-2000:] if completed.stderr else None,
        "ok": completed.returncode == 0 and output.is_file() and output.stat().st_size >= 1000,
    }
    if output.is_file():
        report.update({"size_bytes": output.stat().st_size, "sha256": sha256(output)})
    if raw_output.is_file():
        report.update({"raw_size_bytes": raw_output.stat().st_size, "raw_sha256": sha256(raw_output)})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(3)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
