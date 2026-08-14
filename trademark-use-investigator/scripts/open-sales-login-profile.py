#!/usr/bin/env python3

"""Open requested login pages in a persistent dedicated Chrome/Edge profile."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edge_profile import (
    browser_profile_process_is_running,
    dedicated_browser_user_data,
    is_default_browser_user_data,
    resolve_browser_selection,
    running_browser_cdp_sessions,
)
from qcc_reference_guard import diagnostic_qcc_navigation_candidate, validate_qcc_reference
from sales_platforms import PLATFORMS
from process_utils import terminate_process_tree


QCC_BRAND_DETAIL_RE = re.compile(r"^https://www\.qcc\.com/brandDetail/[a-f0-9]{32}\.html$", re.I)
MANAGED_TAB_DOMAINS = {
    "qcc": ("qcc.com",),
    "baidu": ("baidu.com",),
    "so360": ("so.com", "360.cn"),
    "taobao": ("taobao.com", "tmall.com"),
    "jd": ("jd.com",),
    "1688": ("1688.com",),
}
PROTECTED_TAB_RE = re.compile(
    r"login|signin|passport|auth|verify|captcha|risk|punish|security|wappass|"
    r"_____tmd_____|登录|安全验证|验证码|滑块|风控|访问受限|访问频繁|账号验证|"
    r"access denied|forbidden",
    re.I,
)


def public_baseline_query(config: dict) -> str:
    trademark = config.get("trademark") if isinstance(config, dict) else {}
    trademark = trademark if isinstance(trademark, dict) else {}
    values = [
        str(trademark.get("owner") or "").strip(),
        str(trademark.get("name") or "").strip(),
        str(trademark.get("registration_number") or "").strip(),
    ]
    query = " ".join(dict.fromkeys(value for value in values if value))
    return query or "商标使用"


def baidu_search_url(config: dict) -> str:
    query = public_baseline_query(config)
    return "https://www.baidu.com/s?" + urlencode({"wd": query, "rn": "10"}, quote_via=quote)


def so360_search_url(config: dict) -> str:
    query = public_baseline_query(config)
    return "https://www.so.com/s?" + urlencode({"q": query}, quote_via=quote)


def qcc_navigation_url(run_dir: Path) -> tuple[str, str]:
    config = read_json(run_dir / "run-config.json", {}) or {}
    trademark = config.get("trademark") if isinstance(config, dict) else {}
    trademark = trademark if isinstance(trademark, dict) else {}
    orchestration = config.get("cherrystudio_orchestration") if isinstance(config, dict) else {}
    orchestration = orchestration if isinstance(orchestration, dict) else {}
    exact_hint = str(orchestration.get("qcc_brand_url_hint") or "").strip()
    if QCC_BRAND_DETAIL_RE.fullmatch(exact_hint):
        return exact_hint, "exact_brand_detail_hint"
    validation = validate_qcc_reference(run_dir)
    source_url = str(validation.get("source_url") or "").strip()
    if validation.get("ok") is True and QCC_BRAND_DETAIL_RE.fullmatch(source_url):
        return source_url, "exact_brand_detail"
    diagnostic_url = diagnostic_qcc_navigation_candidate(run_dir)
    if diagnostic_url:
        return diagnostic_url, "exact_brand_detail_candidate"
    values = [
        str(trademark.get("registration_number") or "").strip(),
        str(trademark.get("name") or "").strip(),
        str(trademark.get("owner") or "").strip(),
    ]
    query = " ".join(dict.fromkeys(value for value in values if value)) or "商标"
    return "https://www.qcc.com/web_searchBrand?" + urlencode({"searchKey": query}, quote_via=quote), "trademark_search"


def managed_tab_kind(url: str) -> str | None:
    try:
        host = (urlsplit(str(url or "")).hostname or "").casefold()
    except ValueError:
        return None
    for kind, domains in MANAGED_TAB_DOMAINS.items():
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return kind
    return None


def normalized_tab_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return ""
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/') or '/'}?{parsed.query}"


def normalized_qcc_search_term(url: str) -> str | None:
    """Return the semantic QCC trademark-search term, independent of URL rewriting."""
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold()
    if not (host == "qcc.com" or host.endswith(".qcc.com")):
        return None
    if parsed.path.rstrip("/") != "/web_searchBrand":
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    values = [
        str(value)
        for name, items in query.items()
        if name.casefold() in {"searchkey", "key"}
        for value in items
    ]
    normalized = {" ".join(value.split()) for value in values if " ".join(value.split())}
    if len(normalized) != 1:
        return None
    return next(iter(normalized))


def normalized_public_search_term(url: str, kind: str) -> str | None:
    """Return the bound Stage-A query while tolerating provider-added URL parameters."""
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold()
    provider = {
        "baidu": ("www.baidu.com", "/s", "wd"),
        "so360": ("www.so.com", "/s", "q"),
    }.get(kind)
    if provider is None:
        return None
    expected_host, expected_path, query_name = provider
    if parsed.scheme.casefold() != "https" or host != expected_host or parsed.path.rstrip("/") != expected_path:
        return None
    values = parse_qs(parsed.query, keep_blank_values=True).get(query_name) or []
    normalized = {" ".join(str(value).split()) for value in values if " ".join(str(value).split())}
    if len(normalized) != 1:
        return None
    return next(iter(normalized))


def tab_url_matches_desired(actual_url: str, desired_url: str) -> bool:
    """Match QCC search redirects semantically while keeping all other URLs exact."""
    desired_qcc_term = normalized_qcc_search_term(desired_url)
    if desired_qcc_term is not None:
        return normalized_qcc_search_term(actual_url) == desired_qcc_term
    desired_kind = managed_tab_kind(desired_url)
    if desired_kind in {"baidu", "so360"}:
        desired_term = normalized_public_search_term(desired_url, desired_kind)
        return bool(
            desired_term is not None
            and normalized_public_search_term(actual_url, desired_kind) == desired_term
        )
    return normalized_tab_url(actual_url) == normalized_tab_url(desired_url)


def protected_tab_reason(target: dict) -> str | None:
    sample = f"{target.get('url') or ''}\n{target.get('title') or ''}"
    return "login_or_verification" if PROTECTED_TAB_RE.search(sample) else None


def stale_double_encoded_taobao(target: dict) -> bool:
    url = str(target.get("url") or "")
    return managed_tab_kind(url) == "taobao" and "s.taobao.com/search" in url and "%25" in url.casefold()


def cdp_json(endpoint: str, action: str = "list", target_id: str | None = None):
    suffix = "/json/list" if action == "list" else f"/json/{action}/{target_id}"
    with urlopen(endpoint.rstrip("/") + suffix, timeout=4) as response:
        payload = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


def create_missing_login_tabs(endpoint: str, desired_urls: list[str]) -> list[dict]:
    """Create only absent managed tabs when attaching to an already-running profile."""
    targets = [value for value in cdp_json(endpoint) if value.get("type") == "page"]
    created = []
    for desired_url in desired_urls:
        kind = managed_tab_kind(desired_url)
        if not kind:
            continue
        candidates = [value for value in targets if managed_tab_kind(value.get("url")) == kind]
        if any(tab_url_matches_desired(value.get("url"), desired_url) for value in candidates):
            continue
        if any(protected_tab_reason(value) for value in candidates):
            continue
        request = Request(
            endpoint.rstrip("/") + "/json/new?" + quote(desired_url, safe=""),
            method="PUT",
        )
        with urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        created.append({"kind": kind, "url": desired_url, "id": payload.get("id")})
        targets.append(payload)
    if created:
        time.sleep(0.5)
    return created


def reconcile_login_tabs(endpoint: str, desired_urls: list[str]) -> dict:
    """Keep at most one baseline and one visible login/risk page per managed site."""
    desired_by_kind = {
        kind: url for url in desired_urls if (kind := managed_tab_kind(url))
    }
    targets = [value for value in cdp_json(endpoint) if value.get("type") == "page"]
    kept: dict[str, dict] = {}
    closed = []
    close_errors = []
    close_ids: set[str] = set()
    preserved = []
    protected_ids: dict[str, str] = {}
    for kind, desired_url in desired_by_kind.items():
        candidates = [value for value in targets if managed_tab_kind(value.get("url")) == kind]
        exact = [value for value in candidates if tab_url_matches_desired(value.get("url"), desired_url)]
        protected = [value for value in candidates if protected_tab_reason(value)]
        protected_keep = protected[0] if protected else None
        exact_keep = next((value for value in exact if value is not protected_keep), None)
        keep = protected_keep or exact_keep
        if keep is not None:
            kept[kind] = keep
        allowed_ids = {
            str(value.get("id") or "") for value in (protected_keep, exact_keep) if value is not None
        }
        if protected_keep is not None and protected_keep.get("id"):
            protected_ids[str(protected_keep["id"])] = str(protected_tab_reason(protected_keep))
            preserved.append({
                "id": protected_keep.get("id"), "url": protected_keep.get("url"),
                "kind": kind, "reason": protected_tab_reason(protected_keep),
            })
        close_candidates = [
            value for value in candidates
            if str(value.get("id") or "") not in allowed_ids
        ]
        for candidate in close_candidates:
            target_id = str(candidate.get("id") or "")
            if not target_id:
                continue
            try:
                cdp_json(endpoint, "close", target_id)
                close_ids.add(target_id)
                closed.append({"kind": kind, "url": candidate.get("url"), "id": target_id})
            except Exception as exc:
                close_errors.append({
                    "kind": kind, "url": candidate.get("url"), "id": target_id, "error": str(exc),
                })

    # A utility caller may explicitly omit the 360 baseline. Preserve one
    # unresolved verification page in that exceptional mode, matching the
    # historical safety behavior, while normal Stage A treats 360 exactly like
    # Baidu and the three sales-platform baselines above.
    if "so360" not in desired_by_kind:
        so360_candidates = [
            value for value in targets if managed_tab_kind(value.get("url")) == "so360"
        ]
        so360_protected = [value for value in so360_candidates if protected_tab_reason(value)]
        so360_keep = so360_protected[0] if so360_protected else None
        if so360_keep is not None and so360_keep.get("id"):
            protected_ids[str(so360_keep["id"])] = str(protected_tab_reason(so360_keep))
            preserved.append({
                "id": so360_keep.get("id"), "url": so360_keep.get("url"),
                "kind": "so360", "reason": protected_tab_reason(so360_keep),
            })
        for candidate in so360_candidates:
            if candidate is so360_keep:
                continue
            target_id = str(candidate.get("id") or "")
            if not target_id:
                continue
            try:
                cdp_json(endpoint, "close", target_id)
                close_ids.add(target_id)
                closed.append({"kind": "so360", "url": candidate.get("url"), "id": target_id})
            except Exception as exc:
                close_errors.append({
                    "kind": "so360", "url": candidate.get("url"), "id": target_id, "error": str(exc),
                })

    if close_ids:
        time.sleep(0.2)
    final_targets = [value for value in cdp_json(endpoint) if value.get("type") == "page"]
    final_ids = {str(value.get("id") or "") for value in final_targets}
    remaining_close_targets = sorted(close_ids & final_ids)
    lost_protected_targets = sorted(set(protected_ids) - final_ids)
    final_kept: dict[str, dict] = {}
    satisfaction: dict[str, str] = {}
    missing = []
    remaining_duplicates = []
    for kind, desired_url in desired_by_kind.items():
        candidates = [value for value in final_targets if managed_tab_kind(value.get("url")) == kind]
        exact = [value for value in candidates if tab_url_matches_desired(value.get("url"), desired_url)]
        protected = [value for value in candidates if protected_tab_reason(value)]
        if protected:
            final_kept[kind] = protected[0]
            satisfaction[kind] = "pending_verification"
            if len(protected) > 1:
                remaining_duplicates.extend(str(value.get("id") or "") for value in protected[1:])
        elif exact:
            final_kept[kind] = exact[0]
            satisfaction[kind] = "exact"
            if len(exact) > 1:
                remaining_duplicates.extend(str(value.get("id") or "") for value in exact[1:])
        else:
            satisfaction[kind] = "missing"
            missing.append(kind)
    remaining_bad_taobao = [
        str(value.get("id") or "") for value in final_targets
        if stale_double_encoded_taobao(value) and not protected_tab_reason(value)
    ]
    remaining_duplicates.extend(remaining_bad_taobao)
    if "so360" not in desired_by_kind:
        remaining_duplicates.extend(
            str(value.get("id") or "") for value in final_targets
            if managed_tab_kind(value.get("url")) == "so360" and not protected_tab_reason(value)
        )
    desired_qcc = desired_by_kind.get("qcc") or ""
    if QCC_BRAND_DETAIL_RE.fullmatch(desired_qcc):
        remaining_duplicates.extend(
            str(value.get("id") or "") for value in final_targets
            if managed_tab_kind(value.get("url")) == "qcc"
            and "/web_searchBrand" in str(value.get("url") or "")
            and not protected_tab_reason(value)
        )
    kept = final_kept
    focus = kept.get("qcc") or kept.get("taobao") or next(iter(kept.values()), None)
    if focus:
        try:
            cdp_json(endpoint, "activate", str(focus.get("id")))
        except Exception:
            pass
    return {
        "schema_version": "1.0",
        "ok": not (
            missing or close_errors or remaining_close_targets
            or lost_protected_targets or remaining_duplicates
        ),
        "desired_kinds": list(desired_by_kind),
        "kept": {key: {"id": value.get("id"), "url": value.get("url")} for key, value in kept.items()},
        "closed": closed,
        "close_errors": close_errors,
        "preserved": preserved,
        "missing": missing,
        "satisfaction": satisfaction,
        "remaining_close_targets": remaining_close_targets,
        "remaining_duplicates": sorted(set(remaining_duplicates)),
        "lost_protected_targets": lost_protected_targets,
        "final_target_count": len(final_targets),
        "focused_kind": managed_tab_kind(focus.get("url")) if focus else None,
        "qcc_exact_detail_opened": bool(
            any(
                managed_tab_kind(value.get("url")) == "qcc"
                and QCC_BRAND_DETAIL_RE.fullmatch(str(value.get("url") or ""))
                for value in final_targets
            )
        ),
    }


def launch_urls(
    run_dir: Path, platforms: list[str], *, include_qcc: bool, include_baidu: bool,
    include_so360: bool,
) -> tuple[list[str], str | None, str | None, str | None]:
    urls: list[str] = []
    qcc_url = None
    baidu_url = None
    so360_url = None
    if include_qcc:
        qcc_url, _ = qcc_navigation_url(run_dir)
        urls.append(qcc_url)
    if include_baidu:
        config = read_json(run_dir / "run-config.json", {}) or {}
        baidu_url = baidu_search_url(config)
        urls.append(baidu_url)
    if include_so360:
        config = read_json(run_dir / "run-config.json", {}) or {}
        so360_url = so360_search_url(config)
        urls.append(so360_url)
    urls.extend(PLATFORMS[item]["home_url"] for item in platforms)
    return urls, qcc_url, baidu_url, so360_url


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def same_resolved_path(left: object, right: object) -> bool:
    try:
        return str(Path(str(left)).resolve()).casefold() == str(Path(str(right)).resolve()).casefold()
    except (OSError, RuntimeError, ValueError):
        return False


def available_loopback_port(requested: int = 0) -> int:
    if requested and not 1024 <= requested <= 65535:
        raise ValueError("cdp-port must be between 1024 and 65535")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", requested))
        return int(server.getsockname()[1])


def wait_for_cdp(endpoint: str, seconds: float = 12.0) -> dict:
    deadline = time.monotonic() + seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urlopen(endpoint.rstrip("/") + "/json/version", timeout=1) as response:
                value = json.loads(response.read().decode("utf-8"))
            if value.get("webSocketDebuggerUrl"):
                return value
        except Exception as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"Chromium CDP endpoint did not become ready: {last_error}")


def validate_cdp_browser(browser: str, version: dict) -> None:
    product = str(version.get("Browser") or "").casefold()
    matches = (
        (browser == "edge" and ("edg/" in product or "microsoft edge" in product))
        or (browser == "chrome" and "chrome/" in product and "edg/" not in product)
    )
    if not matches:
        raise RuntimeError(f"CDP browser product mismatch: expected {browser}, got {version.get('Browser')!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Open the dedicated Chrome/Edge profile for manual platform login")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--browser", choices=("auto", "edge", "chrome"), default="auto")
    parser.add_argument("--browser-user-data")
    parser.add_argument("--browser-executable")
    parser.add_argument("--edge-user-data")
    parser.add_argument("--edge-executable")
    parser.add_argument("--profile-directory", default="Default")
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--no-qcc", action="store_true")
    parser.add_argument("--no-baidu", action="store_true", help="Do not open the default Baidu preflight search tab")
    parser.add_argument("--no-so360", action="store_true", help="Do not open the default 360 Search preflight tab")
    parser.add_argument("--reuse-running", action="store_true", help="Open login tabs in the already-running dedicated profile")
    parser.add_argument("--handoff-mode", choices=("attach", "restart"), default="attach")
    parser.add_argument("--cdp-port", type=int, default=0)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    config = read_json(run_dir / "run-config.json", {}) or {}
    orchestration = config.get("cherrystudio_orchestration") or {}
    queue_path = run_dir / "discovery" / "sales-login-queue.json"
    state_path = run_dir / "discovery" / "sales-workflow-state.json"
    queue = read_json(queue_path, {}) or {}
    state = read_json(state_path, {}) or {}
    browser_request = "edge" if args.edge_user_data or args.edge_executable else args.browser
    automatic_selection = browser_request == "auto"
    recorded_browser = str(state.get("default_browser") or queue.get("default_browser") or "").strip().casefold()
    recorded_executable = state.get("browser_executable") or queue.get("browser_executable")
    recorded_user_data = state.get("browser_user_data") or queue.get("browser_user_data")
    reuse_recorded_selection = (
        automatic_selection and not args.browser_executable and not args.edge_executable
        and recorded_browser in {"edge", "chrome"} and recorded_executable
    )
    if reuse_recorded_selection:
        browser, executable = resolve_browser_selection(recorded_browser, recorded_executable)
        fallback_used = bool(state.get("browser_fallback_used", queue.get("browser_fallback_used", False)))
    else:
        browser, executable = resolve_browser_selection(
            browser_request, args.browser_executable or args.edge_executable,
        )
        fallback_used = automatic_selection and browser == "chrome"
    if recorded_user_data and recorded_browser in {"edge", "chrome"} and recorded_browser != browser:
        raise ValueError("Recorded browser user-data belongs to a different browser product")
    user_data = Path(
        args.browser_user_data or args.edge_user_data or recorded_user_data
        or dedicated_browser_user_data(browser)
    ).resolve()
    label = "Google Chrome" if browser == "chrome" else "Microsoft Edge"
    if user_data == run_dir or user_data.is_relative_to(run_dir):
        raise ValueError("Dedicated browser user data must stay outside RUN_DIR")
    if is_default_browser_user_data(browser, user_data):
        raise ValueError(f"Refusing to launch the system default {label} profile for automation")
    if not executable.is_file():
        raise FileNotFoundError(f"{label} executable not found: {executable}")
    profile_running = browser_profile_process_is_running(browser, user_data)
    running_session = None
    running_cdp_version = None
    if args.handoff_mode == "attach" and profile_running:
        live_sessions = []
        for candidate in running_browser_cdp_sessions(browser, user_data):
            try:
                version = wait_for_cdp(candidate["endpoint"], seconds=2.5)
                validate_cdp_browser(browser, version)
            except Exception:
                continue
            live_sessions.append((candidate, version))
        if len(live_sessions) != 1:
            raise RuntimeError(
                f"Dedicated trademark {label} is running but does not expose exactly one validated loopback CDP endpoint"
            )
        running_session, running_cdp_version = live_sessions[0]
    elif profile_running and not args.reuse_running:
        raise RuntimeError(f"Close the dedicated trademark {label} profile before opening it again")

    requested = args.platform or [str(item.get("platform") or "") for item in queue.get("platforms") or []]
    platforms = list(dict.fromkeys(item for item in requested if item in PLATFORMS))
    if not platforms:
        raise ValueError("No supported sales platforms are present in the login queue")

    urls, qcc_url, baidu_url, so360_url = launch_urls(
        run_dir, platforms, include_qcc=not args.no_qcc, include_baidu=not args.no_baidu,
        include_so360=not args.no_so360,
    )
    qcc_target_kind = qcc_navigation_url(run_dir)[1] if qcc_url else None

    user_data.mkdir(parents=True, exist_ok=True)
    process = None
    created_tabs = []
    if running_session:
        cdp_endpoint = running_session["endpoint"]
        cdp_version = running_cdp_version
        process_pid = running_session.get("process_id")
    else:
        command = [
            str(executable),
            f"--user-data-dir={user_data}",
            f"--profile-directory={args.profile_directory}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            *(
                ["--edge-skip-compat-layer-relaunch", "--disable-features=msEdgeUpdateLaunchServicesPreferredVersion"]
                if browser == "edge" else []
            ),
            *(
                [
                    f"--remote-debugging-port={available_loopback_port(args.cdp_port)}",
                    "--remote-debugging-address=127.0.0.1",
                ] if args.handoff_mode == "attach" else []
            ),
            *urls,
        ]
        cdp_port = next(
            (int(item.split("=", 1)[1]) for item in command if item.startswith("--remote-debugging-port=")),
            None,
        )
        cdp_endpoint = f"http://127.0.0.1:{cdp_port}" if cdp_port else None
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        process_pid = process.pid
        cdp_version = None
        time.sleep(1.5)
    reconciliation_failure = False
    try:
        cdp_version = cdp_version or (wait_for_cdp(cdp_endpoint) if cdp_endpoint else None)
        if cdp_version:
            validate_cdp_browser(browser, cdp_version)
        if running_session and cdp_endpoint:
            created_tabs = create_missing_login_tabs(cdp_endpoint, urls)
        else:
            time.sleep(1.5)
        tab_reconciliation = reconcile_login_tabs(cdp_endpoint, urls) if cdp_endpoint else None
        if tab_reconciliation and not tab_reconciliation.get("ok"):
            reconciliation_failure = True
            raise RuntimeError(f"Dedicated login tabs were not opened: {tab_reconciliation.get('missing')}")
    except Exception as exc:
        # A browser created by this launcher is not handed off until every
        # required tab is reconciled.  On any pre-handoff failure, close that
        # exact process tree so the dedicated profile is immediately reusable.
        # Never terminate a pre-existing session that we only attached to.
        if process is not None:
            terminate_process_tree(process, grace_seconds=3)
        browser_handed_off = bool(running_session)
        state.update({
            "phase": "tab_reconciliation_failed" if reconciliation_failure else "browser_launch_failed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "default_browser": browser,
            "browser_executable": str(executable),
            "browser_user_data": str(user_data),
            "login_browser_started": browser_handed_off,
            "login_browser_pid": process_pid if browser_handed_off else None,
            "cdp_endpoint": cdp_endpoint if browser_handed_off else None,
            "login_tab_reconciliation": tab_reconciliation if reconciliation_failure else None,
            "launch_error": str(exc),
        })
        write_json(state_path, state)
        raise
    now = datetime.now(timezone.utc).isoformat()
    qcc_visible = ((tab_reconciliation or {}).get("kept") or {}).get("qcc") or {}
    baidu_visible = ((tab_reconciliation or {}).get("kept") or {}).get("baidu") or {}
    so360_visible = ((tab_reconciliation or {}).get("kept") or {}).get("so360") or {}
    qcc_exact_detail_opened = bool((tab_reconciliation or {}).get("qcc_exact_detail_opened"))
    config_endpoint = str(orchestration.get("cdp_endpoint") or "").rstrip("/")
    current_endpoint = str(cdp_endpoint or "").rstrip("/")
    endpoint_recovery = None
    if config_endpoint and current_endpoint and config_endpoint != current_endpoint:
        endpoint_recovery = {
            "schema_version": "1.0",
            "record_type": "validated_cdp_endpoint_recovery",
            "source": "same_dedicated_profile_loopback_cdp",
            "recovered_at": now,
            "previous_endpoint": config_endpoint,
            "endpoint": current_endpoint,
            "expected_browser": browser,
            "detected_browser": cdp_version.get("Browser") if cdp_version else None,
            "process_id": process_pid,
            "same_browser_user_data": same_resolved_path(
                orchestration.get("browser_user_data"), user_data,
            ),
            "same_browser_executable": same_resolved_path(
                orchestration.get("browser_executable"), executable,
            ),
            "same_profile_directory": str(orchestration.get("profile_directory") or "").strip()
            == str(args.profile_directory or "").strip(),
        }
    state.update({
        "schema_version": "1.0",
        "phase": "awaiting_manual_login",
        "updated_at": now,
        "default_browser": browser,
        "browser_selection_policy": "edge_then_chrome",
        "browser_fallback_used": fallback_used,
        "profile_kind": "dedicated_non_default",
        "browser_user_data": str(user_data),
        "browser_executable": str(executable),
        "edge_user_data": str(user_data) if browser == "edge" else None,
        "profile_directory": args.profile_directory,
        "login_browser_started": True,
        "login_browser_reused": bool(running_session),
        "login_browser_pid": process_pid,
        "platforms": platforms,
        "qcc_opened": bool(qcc_visible) if tab_reconciliation else bool(qcc_url),
        "qcc_url": qcc_url,
        "qcc_visible_url": qcc_visible.get("url") or qcc_url,
        "qcc_target_kind": qcc_target_kind,
        "qcc_exact_detail_opened": qcc_exact_detail_opened,
        "qcc_automatic_detail_resolution_after_login": not qcc_exact_detail_opened,
        "baidu_opened": bool(baidu_visible) if tab_reconciliation else bool(baidu_url),
        "baidu_url": baidu_url,
        "baidu_visible_url": baidu_visible.get("url") or baidu_url,
        "so360_opened": bool(so360_visible) if tab_reconciliation else bool(so360_url),
        "so360_url": so360_url,
        "so360_visible_url": so360_visible.get("url") or so360_url,
        "handoff_mode": args.handoff_mode,
        "cdp_endpoint": cdp_endpoint,
        "cdp_browser": cdp_version.get("Browser") if cdp_version else None,
        "cdp_loopback_only": bool(cdp_endpoint),
        "login_tab_reconciliation": tab_reconciliation,
        "login_tabs_created_while_attached": created_tabs,
        **({"cdp_endpoint_recovery": endpoint_recovery} if endpoint_recovery else {}),
        "resume_requires": [
            "manual_login_completed",
            "browser_remains_open" if args.handoff_mode == "attach" else "all_browser_windows_closed",
        ],
    })
    write_json(state_path, state)
    print(json.dumps({
        "dedicated_login_profile_opened": True,
        "browser": browser,
        "browser_selection_policy": "edge_then_chrome",
        "browser_fallback_used": fallback_used,
        "browser_user_data": str(user_data),
        "browser_executable": str(executable),
        "edge_user_data": str(user_data) if browser == "edge" else None,
        "profile_directory": args.profile_directory,
        "platforms": platforms,
        "qcc_opened": bool(qcc_visible) if tab_reconciliation else bool(qcc_url),
        "qcc_url": qcc_url,
        "qcc_visible_url": qcc_visible.get("url") or qcc_url,
        "qcc_target_kind": qcc_target_kind,
        "qcc_exact_detail_opened": qcc_exact_detail_opened,
        "qcc_automatic_detail_resolution_after_login": not qcc_exact_detail_opened,
        "baidu_opened": bool(baidu_visible) if tab_reconciliation else bool(baidu_url),
        "baidu_url": baidu_url,
        "baidu_visible_url": baidu_visible.get("url") or baidu_url,
        "so360_opened": bool(so360_visible) if tab_reconciliation else bool(so360_url),
        "so360_url": so360_url,
        "so360_visible_url": so360_visible.get("url") or so360_url,
        "browser_pid": process_pid,
        "browser_reused": bool(running_session),
        "handoff_mode": args.handoff_mode,
        "cdp_endpoint": cdp_endpoint,
        "cdp_browser": cdp_version.get("Browser") if cdp_version else None,
        "login_tab_reconciliation": tab_reconciliation,
        "login_tabs_created_while_attached": created_tabs,
        "instruction": (
            f"Complete platform and QCC login/verification plus any Baidu/360 Search safety verification in this {label} window. "
            "Do not search QCC manually; keep the window open, then resume the same RUN."
            if args.handoff_mode == "attach" else
            f"Complete platform and QCC login/verification plus any Baidu/360 Search safety verification in this {label} window. "
            "Do not search QCC manually; close every dedicated browser window, then resume the same RUN."
        ),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
