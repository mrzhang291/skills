#!/usr/bin/env python3

"""Create a cross-turn manual-login handoff for sales-platform workflows."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edge_profile import (
    dedicated_browser_user_data,
    installed_chromium_executable,
    preferred_chromium_browser,
)
from sales_platforms import PLATFORMS


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(
    run_dir: Path,
    selected_platforms: list[str] | None = None,
    per_platform: int = 5,
    browser: str = "auto",
):
    run_dir = run_dir.resolve()
    browser = str(browser or "auto").strip().casefold()
    automatic_selection = browser == "auto"
    if browser == "auto":
        browser, browser_executable = preferred_chromium_browser()
    elif browser in {"chrome", "edge"}:
        browser_executable = installed_chromium_executable(browser)
    else:
        raise ValueError(f"Unsupported browser: {browser!r}")
    config = read_json(run_dir / "run-config.json")
    plan = read_json(run_dir / "discovery" / "query-plan.json")
    if not isinstance(config, dict) or not isinstance(plan, dict):
        raise FileNotFoundError("run-config.json and discovery/query-plan.json are required")
    if plan.get("discovery_scope") != "sales_platforms":
        raise ValueError("query-plan.json must use discovery_scope=sales_platforms")

    requested = selected_platforms or [
        str(item.get("target_platform") or "") for item in plan.get("items") or []
    ]
    requested = list(dict.fromkeys(item for item in requested if item in PLATFORMS))
    results = read_json(run_dir / "discovery" / "sales-platform-results.json", {}) or {}
    by_platform: dict[str, list[dict]] = {item: [] for item in requested}
    for candidate in results.get("items") or []:
        platform = str(candidate.get("platform") or "")
        if platform in by_platform and len(by_platform[platform]) < max(1, per_platform):
            by_platform[platform].append({
                "candidate_id": candidate.get("candidate_id"),
                "title": candidate.get("title"),
                "url": candidate.get("url"),
                "verification_state": candidate.get("verification_state"),
            })

    platform_items = []
    for platform in requested:
        platform_config = PLATFORMS[platform]
        candidates = by_platform[platform]
        platform_items.append({
            "platform": platform,
            "platform_label": platform_config["label"],
            "login_url": platform_config["home_url"],
            "candidate_count": len(candidates),
            "candidates": candidates,
            "login_status": "pending",
        })

    now = datetime.now(timezone.utc).isoformat()
    browser_user_data = dedicated_browser_user_data(browser).resolve()
    browser_label = "Chrome" if browser == "chrome" else "Edge"
    trademark = config.get("trademark") or {}
    baidu_query = " ".join(dict.fromkeys(str(value or "").strip() for value in (
        trademark.get("owner"), trademark.get("name"), trademark.get("registration_number")
    ) if str(value or "").strip())) or "商标使用"
    baidu_url = "https://www.baidu.com/s?" + urlencode(
        {"wd": baidu_query, "rn": "10"}, quote_via=quote
    )
    qcc_query = " ".join(dict.fromkeys(str(value or "").strip() for value in (
        trademark.get("registration_number"), trademark.get("name"), trademark.get("owner")
    ) if str(value or "").strip())) or "商标"
    qcc_url = "https://www.qcc.com/web_searchBrand?" + urlencode(
        {"searchKey": qcc_query}, quote_via=quote
    )
    orchestration = config.get("cherrystudio_orchestration") or {}
    qcc_exact_hint = str(orchestration.get("qcc_brand_url_hint") or "").strip()
    if qcc_exact_hint:
        qcc_url = qcc_exact_hint
    qcc_target_kind = "exact_brand_detail_hint" if qcc_exact_hint else "trademark_search"
    queue = {
        "schema_version": "1.0",
        "record_type": "sales_platform_manual_login_queue",
        "run_id": config.get("run_id"),
        "created_at": now,
        "phase": "prepared_browser_launch",
        "default_browser": browser,
        "browser_selection_policy": "edge_then_chrome",
        "browser_fallback_used": automatic_selection and browser == "chrome",
        "browser_executable": str(browser_executable) if browser_executable else None,
        "profile_kind": "dedicated_non_default",
        "browser_user_data": str(browser_user_data),
        "edge_user_data": str(browser_user_data) if browser == "edge" else None,
        "profile_directory": "Default",
        "handoff_mode": "attach",
        "requires_browser_closed_before_resume": False,
        "requires_edge_closed_before_resume": False,
        "cookie_export_required": False,
        "platform_count": len(platform_items),
        "platforms": platform_items,
        "qcc_preflight": {
            "opened_by_default": True,
            "url": qcc_url,
            "fallback_url": qcc_url,
            "target_kind": qcc_target_kind,
            "status": "pending_login_or_validation" if qcc_exact_hint else "pending_login_or_automatic_detail_resolution",
            "manual_trademark_search_required": False,
        },
        "baidu_preflight": {"opened_by_default": True, "url": baidu_url, "status": "pending_manual_verification"},
        "resume_instruction": f"请在商标调查专用 {browser_label} Profile 中完成平台与企查查登录/验证，并处理百度安全验证；无需手工搜索企查查商标。完成后保持 {browser_label} 打开，再回复：已登录并保持打开。",
    }
    discovery_dir = run_dir / "discovery"
    queue_path = discovery_dir / "sales-login-queue.json"
    markdown_path = discovery_dir / "sales-login-queue.md"
    state_path = discovery_dir / "sales-workflow-state.json"
    write_json(queue_path, queue)
    write_json(state_path, {
        "schema_version": "1.0",
        "run_id": config.get("run_id"),
        "phase": "prepared_browser_launch",
        "updated_at": now,
        "login_queue": "discovery/sales-login-queue.json",
        "default_browser": browser,
        "browser_selection_policy": "edge_then_chrome",
        "browser_fallback_used": automatic_selection and browser == "chrome",
        "browser_executable": str(browser_executable) if browser_executable else None,
        "profile_kind": "dedicated_non_default",
        "browser_user_data": str(browser_user_data),
        "edge_user_data": str(browser_user_data) if browser == "edge" else None,
        "profile_directory": "Default",
        "handoff_mode": "attach",
        "resume_requires": ["manual_login_completed", "browser_remains_open"],
    })

    lines = [
        f"# {config.get('trademark', {}).get('name') or '商标'}：销售平台登录清单",
        "",
        f"请使用 Skill 的 open-sales-login-profile.py 启动商标调查专用 {browser_label} Profile。启动器会同时打开企查查、百度预检页和销售平台首页；只需完成平台与企查查登录/验证及百度安全验证，无需手工搜索企查查商标。随后保持 {browser_label} 打开，再回复“已登录并保持打开”。",
        "",
        f"专用 User Data：`{browser_user_data}`（位于 RUN_DIR 外，不导出 Cookie）。",
        "",
        "| 平台 | 登录入口 | 已发现候选 |",
        "| --- | --- | ---: |",
        f"| 企查查商标参考 | [启动器自动打开的商标页]({qcc_url}) | {'精确详情页' if qcc_exact_hint else '登录后系统自动定位详情'} |",
        f"| 百度辅助检索 | [打开并处理验证]({baidu_url}) | 不计入销售平台数量 |",
    ]
    for item in platform_items:
        lines.append(
            f"| {item['platform_label']} | [打开并登录]({item['login_url']}) | {item['candidate_count']} |"
        )
        for candidate in item["candidates"]:
            title = str(candidate.get("title") or "候选页面").replace("|", "\\|")
            lines.append(f"| ↳ | [{title}]({candidate['url']}) | 待登录后抓取 |")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return queue_path, markdown_path, state_path, queue


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the manual Chrome/Edge login handoff queue")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--per-platform", type=int, default=5)
    parser.add_argument("--browser", choices=("auto", "edge", "chrome"), default="auto")
    args = parser.parse_args()
    queue_path, markdown_path, state_path, queue = build(
        Path(args.run_dir), args.platform or None, args.per_platform, args.browser
    )
    print(json.dumps({
        "browser_launch_prepared": True,
        "awaiting_manual_login": False,
        "platform_count": queue["platform_count"],
        "queue": str(queue_path),
        "markdown": str(markdown_path),
        "state": str(state_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
