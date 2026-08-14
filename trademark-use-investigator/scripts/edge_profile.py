"""Resolve dedicated Chrome/Edge user-data roots and active profile locks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess


def default_edge_user_data() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local / "Microsoft" / "Edge" / "User Data"


def default_chrome_user_data() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local / "Google" / "Chrome" / "User Data"


def dedicated_edge_user_data() -> Path:
    configured = str(os.environ.get("TRADEMARK_EDGE_USER_DATA") or "").strip()
    if configured:
        return Path(configured).expanduser()
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local / "TrademarkUseInvestigator" / "Edge User Data"


def dedicated_chrome_user_data() -> Path:
    configured = str(os.environ.get("TRADEMARK_CHROME_USER_DATA") or "").strip()
    if configured:
        return Path(configured).expanduser()
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local / "TrademarkUseInvestigator" / "Chrome User Data"


def chromium_executable_candidates(browser: str) -> list[Path]:
    browser = str(browser or "").strip().casefold()
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    if browser == "chrome":
        candidates = [
            os.environ.get("CHROME_EXECUTABLE_PATH"),
            program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
            program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
            local / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    elif browser == "edge":
        candidates = [
            os.environ.get("EDGE_EXECUTABLE_PATH"),
            program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            local / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ]
    else:
        raise ValueError(f"Unsupported browser: {browser!r}")
    return [Path(value).expanduser() for value in candidates if value]


def installed_chromium_executable(browser: str) -> Path | None:
    browser = str(browser or "").strip().casefold()
    return next((
        path for path in chromium_executable_candidates(browser)
        if path.is_file() and browser_family_from_executable(path) == browser
    ), None)


def default_chromium_executable(browser: str) -> Path:
    paths = chromium_executable_candidates(browser)
    installed = installed_chromium_executable(browser)
    if installed is not None:
        return installed
    compatible = [path for path in paths if browser_family_from_executable(path) == browser]
    return compatible[0] if compatible else paths[0]


def preferred_chromium_browser() -> tuple[str, Path]:
    """Select dedicated Edge first and Chrome only when Edge is unavailable."""
    for browser in ("edge", "chrome"):
        executable = installed_chromium_executable(browser)
        if executable is not None:
            return browser, executable.resolve()
    raise FileNotFoundError("Microsoft Edge or Google Chrome executable was not found")


def browser_family_from_executable(executable: str | Path) -> str | None:
    name = Path(executable).name.casefold()
    if "msedge" in name:
        return "edge"
    if "chrome" in name:
        return "chrome"
    return None


def resolve_browser_selection(browser: str = "auto", executable: str | Path | None = None) -> tuple[str, Path]:
    """Resolve an automatic or explicit browser choice without mixing profiles."""
    requested = str(browser or "auto").strip().casefold()
    if requested not in {"auto", "edge", "chrome"}:
        raise ValueError(f"Unsupported browser: {requested!r}")
    if executable:
        path = Path(executable).expanduser().resolve()
        detected = browser_family_from_executable(path)
        if requested == "auto":
            if detected is None:
                raise ValueError("Cannot infer Edge or Chrome from --browser-executable; specify --browser")
            requested = detected
        elif detected is not None and detected != requested:
            raise ValueError(
                f"Browser product {requested!r} does not match executable {path.name!r}"
            )
        return requested, path
    if requested == "auto":
        return preferred_chromium_browser()
    return requested, default_chromium_executable(requested).resolve()


def dedicated_browser_user_data(browser: str) -> Path:
    browser = str(browser or "").strip().casefold()
    if browser == "chrome":
        return dedicated_chrome_user_data()
    if browser == "edge":
        return dedicated_edge_user_data()
    raise ValueError(f"Unsupported browser: {browser!r}")


def is_default_browser_user_data(browser: str, user_data: Path) -> bool:
    browser = str(browser or "").strip().casefold()
    if browser == "chrome":
        return Path(user_data).resolve() == default_chrome_user_data().resolve()
    if browser == "edge":
        return is_default_edge_user_data(user_data)
    raise ValueError(f"Unsupported browser: {browser!r}")


def resolve_profile_directory(user_data: Path, requested: str | None = "auto") -> str:
    user_data = Path(user_data).resolve()
    value = str(requested or "auto").strip()
    if value.casefold() != "auto":
        profile = value
    else:
        profile = "Default"
        local_state = user_data / "Local State"
        if local_state.is_file():
            try:
                state = json.loads(local_state.read_text(encoding="utf-8"))
                last_used = str((state.get("profile") or {}).get("last_used") or "").strip()
                if last_used:
                    profile = last_used
            except (OSError, json.JSONDecodeError):
                pass
        if not (user_data / profile).is_dir():
            available = [
                path.name for path in user_data.iterdir()
                if path.is_dir() and (path.name == "Default" or path.name.startswith("Profile "))
            ] if user_data.is_dir() else []
            if available:
                profile = sorted(available, key=lambda item: (item != "Default", item))[0]
    if not (user_data / profile).is_dir():
        raise FileNotFoundError(f"Browser profile directory not found: {user_data / profile}")
    return profile


def is_default_edge_user_data(user_data: Path) -> bool:
    return Path(user_data).resolve() == default_edge_user_data().resolve()


def edge_profile_process_is_running(user_data: Path) -> bool:
    """Return whether an Edge process is using this exact user-data root.

    A normal Edge window that uses the system profile must not block the
    dedicated trademark profile.  Only the profile that can be locked matters.
    """
    return browser_profile_process_is_running("edge", user_data)


def browser_profile_process_is_running(browser: str, user_data: Path) -> bool:
    """Return whether Chrome/Edge is using this exact user-data root."""
    if os.name != "nt":
        return False
    browser = str(browser or "").strip().casefold()
    process_name = {"chrome": "chrome.exe", "edge": "msedge.exe"}.get(browser)
    if process_name is None:
        raise ValueError(f"Unsupported browser: {browser!r}")
    completed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference='SilentlyContinue'; "
            f"Get-CimInstance Win32_Process -Filter \"Name='{process_name}'\" | "
            "ForEach-Object { $_.CommandLine }",
        ],
        text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=15,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Unable to determine whether the dedicated {browser} profile is already running; "
            "refusing to launch another browser instance"
        )
    needle = str(Path(user_data).resolve()).casefold()
    return bool(needle and needle in (completed.stdout or "").casefold())


def running_browser_cdp_sessions(browser: str, user_data: Path) -> list[dict]:
    """Return live-looking loopback CDP origins advertised by this profile's main process.

    Edge can preserve or relaunch the dedicated process while the persisted RUN
    still contains an older debugger port.  Recover only an explicitly
    loopback-bound port from a process using the exact dedicated user-data root.
    The caller must still probe `/json/version` and validate the browser product.
    """
    if os.name != "nt":
        return []
    browser = str(browser or "").strip().casefold()
    process_name = {"chrome": "chrome.exe", "edge": "msedge.exe"}.get(browser)
    if process_name is None:
        raise ValueError(f"Unsupported browser: {browser!r}")
    completed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference='Stop'; "
            f"Get-CimInstance Win32_Process -Filter \"Name='{process_name}'\" | "
            "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress",
        ],
        text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=15,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Unable to inspect the dedicated {browser} profile CDP endpoint; refusing endpoint recovery"
        )
    raw = (completed.stdout or "").strip()
    if not raw:
        return []
    try:
        records = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Dedicated browser process inventory was not valid JSON") from exc
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        raise RuntimeError("Dedicated browser process inventory had an invalid shape")
    needle = str(Path(user_data).resolve()).casefold()
    sessions = []
    for record in records:
        if not isinstance(record, dict):
            continue
        command_line = str(record.get("CommandLine") or "")
        folded = command_line.casefold()
        if not needle or needle not in folded:
            continue
        port_match = re.search(r'--remote-debugging-port(?:=|\s+)"?(\d{2,5})', command_line, re.I)
        address_match = re.search(
            r'--remote-debugging-address(?:=|\s+)"?(127\.0\.0\.1|localhost|\[?::1\]?)',
            command_line,
            re.I,
        )
        if not port_match or not address_match:
            continue
        port = int(port_match.group(1))
        if not 1024 <= port <= 65535:
            continue
        host = "127.0.0.1" if address_match.group(1).casefold() == "localhost" else address_match.group(1)
        if host in {"::1", "[::1]"}:
            host = "[::1]"
        session = {
            "endpoint": f"http://{host}:{port}",
            "process_id": int(record.get("ProcessId") or 0) or None,
            "parent_process_id": int(record.get("ParentProcessId") or 0) or None,
        }
        if session["endpoint"] not in {item["endpoint"] for item in sessions}:
            sessions.append(session)
    return sessions
