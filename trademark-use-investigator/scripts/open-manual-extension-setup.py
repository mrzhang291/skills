#!/usr/bin/env python3

"""Open the Chrome/Edge unpacked-extension UI and bundled extension folder."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edge_profile import (
    browser_profile_process_is_running,
    dedicated_browser_user_data,
    is_default_browser_user_data,
    resolve_browser_selection,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare one-time manual installation of the capture extension")
    parser.add_argument("--browser", choices=("auto", "edge", "chrome"), default="auto")
    parser.add_argument("--browser-executable")
    parser.add_argument("--browser-user-data")
    parser.add_argument("--chrome-executable")
    parser.add_argument("--chrome-user-data")
    parser.add_argument("--edge-executable")
    parser.add_argument("--edge-user-data")
    parser.add_argument("--profile-directory", default="Default")
    parser.add_argument("--extension-dir")
    args = parser.parse_args()

    browser_request = args.browser
    if args.edge_executable or args.edge_user_data:
        browser_request = "edge"
    elif args.chrome_executable or args.chrome_user_data:
        browser_request = "chrome"
    automatic_selection = browser_request == "auto"
    specific_executable = args.chrome_executable if browser_request == "chrome" else args.edge_executable
    browser, executable = resolve_browser_selection(
        browser_request, args.browser_executable or specific_executable,
    )
    specific_user_data = args.chrome_user_data if browser == "chrome" else args.edge_user_data
    user_data = Path(args.browser_user_data or specific_user_data or dedicated_browser_user_data(browser)).resolve()
    label = "Google Chrome" if browser == "chrome" else "Microsoft Edge"
    extension_dir = Path(args.extension_dir).resolve() if args.extension_dir else (
        Path(__file__).resolve().parent.parent / "assets" / "manual-capture-extension"
    ).resolve()
    manifest = extension_dir / "manifest.json"
    if not executable.is_file():
        raise FileNotFoundError(f"{label} executable not found: {executable}")
    if is_default_browser_user_data(browser, user_data):
        raise ValueError(f"Refusing to use the system default {label} profile")
    if browser_profile_process_is_running(browser, user_data):
        raise RuntimeError(f"Close the dedicated trademark {label} profile before extension setup")
    if not manifest.is_file():
        raise FileNotFoundError(f"Extension manifest not found: {manifest}")
    user_data.mkdir(parents=True, exist_ok=True)

    browser_process = subprocess.Popen([
        str(executable),
        f"--user-data-dir={user_data}",
        f"--profile-directory={args.profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        f"{browser}://extensions/",
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    subprocess.Popen(
        ["explorer.exe", f"/select,{manifest}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    print(json.dumps({
        "extension_setup_opened": True,
        "browser": browser,
        "browser_selection_policy": "edge_then_chrome",
        "browser_fallback_used": automatic_selection and browser == "chrome",
        "browser_pid": browser_process.pid,
        "browser_executable": str(executable),
        "browser_user_data": str(user_data),
        "edge_pid": browser_process.pid if browser == "edge" else None,
        "extension_dir": str(extension_dir),
        "instruction": f"In {label}, enable Developer mode, click Load unpacked, select extension_dir, pin the extension, then close the dedicated browser window.",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
