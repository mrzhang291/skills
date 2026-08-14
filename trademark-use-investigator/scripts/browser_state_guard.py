#!/usr/bin/env python3
"""Validate the immutable dedicated-browser identity recorded for one RUN."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from edge_profile import browser_family_from_executable, is_default_browser_user_data
from qcc_reference_guard import is_exact_qcc_brand_url, validate_qcc_reference


def _clean(value) -> str:
    return str(value or "").strip()


def _expected_public_query(config: dict) -> str:
    trademark = config.get("trademark") if isinstance(config, dict) else {}
    trademark = trademark if isinstance(trademark, dict) else {}
    return " ".join(dict.fromkeys(
        _clean(value) for value in (
            trademark.get("owner"), trademark.get("name"), trademark.get("registration_number"),
        ) if _clean(value)
    ))


def _same_path(left, right) -> bool:
    if not _clean(left) or not _clean(right):
        return False
    return os.path.normcase(str(Path(left).expanduser().resolve())) == os.path.normcase(
        str(Path(right).expanduser().resolve())
    )


def _cdp_product_matches(browser: str, product: object) -> bool:
    value = _clean(product).casefold()
    return (
        (browser == "edge" and ("edg/" in value or "microsoft edge" in value))
        or (browser == "chrome" and "chrome/" in value and "edg/" not in value)
    )


def _validated_endpoint_recovery(
    orchestration: dict, state: dict, browser: str, config_endpoint: str, state_endpoint: str,
) -> bool:
    recovery = state.get("cdp_endpoint_recovery")
    return bool(
        isinstance(recovery, dict)
        and recovery.get("schema_version") == "1.0"
        and recovery.get("source") == "same_dedicated_profile_loopback_cdp"
        and _clean(recovery.get("previous_endpoint")).rstrip("/") == config_endpoint
        and _clean(recovery.get("endpoint")).rstrip("/") == state_endpoint
        and _clean(recovery.get("expected_browser")).casefold() == browser
        and _cdp_product_matches(browser, recovery.get("detected_browser"))
        and recovery.get("same_browser_user_data") is True
        and recovery.get("same_browser_executable") is True
        and recovery.get("same_profile_directory") is True
    )


def _validated_qcc_upgrade(run_dir: Path, orchestration: dict, state: dict) -> bool:
    original_url = _clean(orchestration.get("qcc_url"))
    current_url = _clean(state.get("qcc_url"))
    initial_url = _clean(state.get("qcc_initial_url"))
    same_exact_url_upgrade = bool(
        original_url == current_url and is_exact_qcc_brand_url(original_url)
    )
    if (
        not original_url.startswith("https://www.qcc.com/")
        or not is_exact_qcc_brand_url(current_url)
        or state.get("qcc_opened") is not True
        or state.get("qcc_exact_detail_opened") is not True
        or state.get("qcc_target_kind") != "exact_brand_detail"
        or (initial_url != original_url and not (same_exact_url_upgrade and not initial_url))
    ):
        return False
    validation = validate_qcc_reference(run_dir)
    return bool(validation.get("ok") is True and _clean(validation.get("source_url")) == current_url)


def validate_locked_browser_state(
    run_dir: Path, config: dict, state: dict, *, require_qcc: bool = True,
    require_baidu: bool | None = None, require_so360: bool | None = None,
) -> dict:
    run_dir = Path(run_dir).resolve()
    orchestration = config.get("cherrystudio_orchestration") or {}
    errors: list[str] = []
    browser = _clean(orchestration.get("selected_browser")).casefold()
    state_browser = _clean(state.get("default_browser")).casefold()
    if orchestration.get("browser_selection_policy") != "edge_then_chrome":
        errors.append("browser_selection_policy_not_locked")
    if state.get("browser_selection_policy") != "edge_then_chrome":
        errors.append("state_browser_selection_policy_not_locked")
    if browser not in {"edge", "chrome"} or state_browser != browser:
        errors.append("browser_product_mismatch")

    config_executable = orchestration.get("browser_executable")
    state_executable = state.get("browser_executable")
    if not _same_path(config_executable, state_executable):
        errors.append("browser_executable_mismatch")
    elif not Path(state_executable).is_file():
        errors.append("browser_executable_missing")
    elif browser_family_from_executable(state_executable) != browser:
        errors.append("browser_executable_product_mismatch")

    config_user_data = orchestration.get("browser_user_data")
    state_user_data = state.get("browser_user_data") or state.get("edge_user_data")
    if not _same_path(config_user_data, state_user_data):
        errors.append("browser_user_data_mismatch")
    elif state_user_data:
        user_data = Path(state_user_data).resolve()
        if user_data == run_dir or user_data.is_relative_to(run_dir):
            errors.append("browser_user_data_inside_run")
        elif browser in {"edge", "chrome"} and is_default_browser_user_data(browser, user_data):
            errors.append("system_default_browser_profile_forbidden")

    config_profile = _clean(orchestration.get("profile_directory"))
    state_profile = _clean(state.get("profile_directory"))
    if not config_profile or config_profile != state_profile:
        errors.append("browser_profile_directory_mismatch")

    config_endpoint = _clean(orchestration.get("cdp_endpoint")).rstrip("/")
    state_endpoint = _clean(state.get("cdp_endpoint")).rstrip("/")
    endpoint_recovered = _validated_endpoint_recovery(
        orchestration, state, browser, config_endpoint, state_endpoint,
    )
    if not config_endpoint or (config_endpoint != state_endpoint and not endpoint_recovered):
        errors.append("cdp_endpoint_mismatch")
    try:
        parsed = urlsplit(state_endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            errors.append("cdp_endpoint_not_loopback_http")
    except (ValueError, TypeError):
        errors.append("cdp_endpoint_invalid")
    if state.get("cdp_loopback_only") is not True:
        errors.append("cdp_loopback_flag_missing")
    if state.get("handoff_mode") != "attach":
        errors.append("browser_handoff_mode_not_attach")

    config_cdp_browser = _clean(orchestration.get("cdp_browser"))
    state_cdp_browser = _clean(state.get("cdp_browser"))
    browser_version_changed_with_recovery = bool(
        endpoint_recovered
        and _cdp_product_matches(browser, config_cdp_browser)
        and _cdp_product_matches(browser, state_cdp_browser)
    )
    if (
        not config_cdp_browser
        or (config_cdp_browser != state_cdp_browser and not browser_version_changed_with_recovery)
    ):
        errors.append("cdp_browser_identity_mismatch")
    elif browser in {"edge", "chrome"} and not _cdp_product_matches(browser, state_cdp_browser):
        errors.append("cdp_browser_product_mismatch")

    expected_fallback = browser == "chrome"
    if orchestration.get("browser_fallback_used") is not expected_fallback:
        errors.append("config_browser_fallback_flag_mismatch")
    if state.get("browser_fallback_used") is not expected_fallback:
        errors.append("state_browser_fallback_flag_mismatch")

    if require_qcc:
        qcc_url = _clean(state.get("qcc_url"))
        if state.get("qcc_opened") is not True or not qcc_url.startswith("https://www.qcc.com/"):
            errors.append("qcc_login_tab_not_locked")
        qcc_upgraded = _validated_qcc_upgrade(run_dir, orchestration, state)
        if orchestration.get("qcc_url") != qcc_url and not qcc_upgraded:
            errors.append("qcc_url_mismatch")
        if orchestration.get("qcc_target_kind") != state.get("qcc_target_kind") and not qcc_upgraded:
            errors.append("qcc_target_kind_mismatch")

    if require_baidu is None:
        require_baidu = bool(_clean(orchestration.get("baidu_url")))
    if require_baidu:
        baidu_url = _clean(state.get("baidu_url"))
        if state.get("baidu_opened") is not True:
            errors.append("baidu_preflight_tab_not_opened")
        if orchestration.get("baidu_url") != baidu_url:
            errors.append("baidu_url_mismatch")
        try:
            parsed_baidu = urlsplit(baidu_url)
            expected_query = _expected_public_query(config)
            actual_query = (parse_qs(parsed_baidu.query).get("wd") or [""])[0]
            if (
                parsed_baidu.scheme != "https"
                or parsed_baidu.hostname != "www.baidu.com"
                or parsed_baidu.path != "/s"
                or actual_query != expected_query
            ):
                errors.append("baidu_preflight_url_identity_invalid")
        except (ValueError, TypeError):
            errors.append("baidu_preflight_url_invalid")

    if require_so360 is None:
        require_so360 = bool(_clean(orchestration.get("so360_url")))
    if require_so360:
        so360_url = _clean(state.get("so360_url"))
        if state.get("so360_opened") is not True:
            errors.append("so360_preflight_tab_not_opened")
        if orchestration.get("so360_url") != so360_url:
            errors.append("so360_url_mismatch")
        try:
            parsed_so360 = urlsplit(so360_url)
            expected_query = _expected_public_query(config)
            actual_query = (parse_qs(parsed_so360.query).get("q") or [""])[0]
            if (
                parsed_so360.scheme != "https"
                or parsed_so360.hostname != "www.so.com"
                or parsed_so360.path != "/s"
                or actual_query != expected_query
            ):
                errors.append("so360_preflight_url_identity_invalid")
        except (ValueError, TypeError):
            errors.append("so360_preflight_url_invalid")

    return {
        "schema_version": "1.0",
        "record_type": "locked_browser_state_validation",
        "ok": not errors,
        "browser": browser or None,
        "cdp_endpoint_recovered": endpoint_recovered,
        "errors": errors,
    }
