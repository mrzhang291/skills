#!/usr/bin/env python3

"""Build a bounded discovery plan without network or browser activity."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sales_platforms import API_DISCOVERY_PLATFORM_ORDER, PLATFORMS

FORENSIC_PLATFORMS = ("jd.com", "taobao.com", "tmall.com", "1688.com", "douyin.com", "xiaohongshu.com")


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def add_query(items: list[dict], seen: set[str], family: str, parts: list[str]) -> None:
    query = clean(" ".join(clean(item) for item in parts if clean(item)))
    if not query or query.casefold() in seen:
        return
    seen.add(query.casefold())
    items.append({"query_id": f"Q{len(items) + 1:03d}", "family": family, "query": query})


def quick_queries(config: dict) -> list[dict]:
    trademark = config.get("trademark") or {}
    mark = clean(trademark.get("name"))
    registration = clean(trademark.get("registration_number"))
    owner = clean(trademark.get("owner"))
    goods = [clean(item) for item in trademark.get("goods_services") or [] if clean(item)]
    items: list[dict] = []
    seen: set[str] = set()
    add_query(items, seen, "registration", [registration, mark])
    add_query(items, seen, "owner_mark", [f'"{owner}"' if owner else "", f'"{mark}"' if mark else ""])
    midpoint = max(1, (len(goods) + 1) // 2)
    add_query(items, seen, "goods_group_a", [mark, *goods[:midpoint]])
    add_query(items, seen, "goods_group_b", [mark, *goods[midpoint:]])
    add_query(items, seen, "commercial", [mark, "价格 购买 店铺 包装"])
    focus_a = goods[-1] if goods else "商品"
    focus_b = goods[min(4, len(goods) - 1)] if goods else "商品"
    add_query(items, seen, "platform_jd", ["site:jd.com", mark, focus_a])
    add_query(items, seen, "platform_taobao", ["site:taobao.com", mark, focus_b])
    return items


def sales_platform_queries(config: dict, platforms: list[str] | None = None) -> list[dict]:
    trademark = config.get("trademark") or {}
    mark = clean(trademark.get("name"))
    goods = [clean(item) for item in trademark.get("goods_services") or [] if clean(item)]
    goods = [re.sub(r"[（(][一二三四五六七八九十0-9]+[）)]$", "", item).strip() for item in goods]
    goods = list(dict.fromkeys(item for item in goods if item))
    fallback = "商品"
    items: list[dict] = []
    focus_goods = goods or [fallback]
    selected = list(dict.fromkeys(platforms or API_DISCOVERY_PLATFORM_ORDER))
    unknown = [platform for platform in selected if platform not in PLATFORMS]
    if unknown:
        raise ValueError(f"Unsupported sales platform(s): {', '.join(unknown)}")
    for platform in selected:
        config_item = PLATFORMS[platform]
        domain = config_item["domains"][0]
        for good in focus_goods:
            api_query = clean(" ".join(item for item in (mark, good) if item))
            items.append({
                "query_id": f"Q{len(items) + 1:03d}",
                "family": f"sales_{platform}",
                "query": api_query,
                "target_platform": platform,
                "target_platform_label": config_item["label"],
                "allowed_domains": list(config_item["domains"]),
                "target_good": good,
                "platform_search_query": api_query,
                "public_index_query": clean(f'site:{domain} "{mark}" {good}'),
                "discovery_channel": "official_api",
                "api_name": config_item.get("api_method"),
                "api_scope": config_item.get("api_scope", "permission_specific"),
            })
    return items


def forensic_queries(config: dict, reference: dict) -> list[dict]:
    trademark = config.get("trademark") or {}
    mark = clean(trademark.get("name"))
    registration = clean(trademark.get("registration_number"))
    owner = clean(trademark.get("owner"))
    goods = [clean(item) for item in trademark.get("goods_services") or [] if clean(item)]
    historical = ((reference.get("owner_names") or {}).get("historical") or [])
    items: list[dict] = []
    seen: set[str] = set()
    add_query(items, seen, "registration", [registration, mark])
    add_query(items, seen, "owner_mark", [f'"{owner}"' if owner else "", f'"{mark}"' if mark else ""])
    for name in historical:
        add_query(items, seen, "historical_owner", [f'"{clean(name)}"', f'"{mark}"' if mark else ""])
    for goods_item in goods:
        add_query(items, seen, "goods", [mark, goods_item])
    add_query(items, seen, "commercial", [mark, "价格 购买 店铺 包装 标签 厂家"])
    owner_goods = " ".join(goods[:3]) if goods else "商品"
    add_query(items, seen, "owner_goods", [f'"{owner}"' if owner else "", owner_goods])
    focus = goods[-1] if goods else "商品"
    for platform in FORENSIC_PLATFORMS:
        add_query(items, seen, "platform", [f"site:{platform}", mark, focus])
    return items


def build(run_dir: Path, scope: str | None = None, platforms: list[str] | None = None) -> tuple[Path, dict]:
    run_dir = run_dir.resolve()
    config = read_json(run_dir / "run-config.json")
    if not isinstance(config, dict):
        raise FileNotFoundError(f"run-config.json not found or invalid: {run_dir}")
    reference = read_json(run_dir / "reference" / "reference.json", {}) or {}
    profile = config.get("execution_profile") or "forensic"
    if profile not in {"quick", "forensic"}:
        raise ValueError(f"Unsupported execution_profile: {profile!r}")
    scope = clean(scope or "evidence").lower().replace("-", "_")
    if scope not in {"evidence", "sales_platforms"}:
        raise ValueError(f"Unsupported discovery scope: {scope!r}")
    if scope == "sales_platforms":
        if profile != "quick":
            raise ValueError("sales_platforms scope currently requires execution_profile=quick")
        queries = sales_platform_queries(config, platforms)
    else:
        queries = quick_queries(config) if profile == "quick" else forensic_queries(config, reference)
    configured_max_queries = int((config.get("budgets") or {}).get("max_query_count", 36))
    # Sales-platform searching is an explicit mark × goods × platform matrix.
    # Do not truncate it with the general web-discovery query budget, otherwise
    # later goods or platforms silently disappear from the investigation.
    max_queries = len(queries) if scope == "sales_platforms" else configured_max_queries
    queries = queries[:max_queries]
    output = {
        "schema_version": "1.0",
        "record_type": "trademark_discovery_query_plan",
        "run_id": config.get("run_id"),
        "execution_profile": profile,
        "discovery_scope": scope,
        "network_used": False,
        "browser_used": False,
        "query_count": len(queries),
        "max_query_count": max_queries,
        "query_strategy": "official_api_mark_plus_each_good" if scope == "sales_platforms" else "bounded_web_discovery",
        "provider_goal": int((config.get("coverage_requirements") or {}).get("min_discovery_providers", 1)),
        "items": queries,
    }
    path = run_dir / "discovery" / "query-plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a bounded trademark discovery query plan")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--scope", choices=("evidence", "sales-platforms"), default="evidence")
    parser.add_argument("--platform", action="append", default=[], help="Limit sales-platform plan to one or more platform keys")
    args = parser.parse_args()
    path, output = build(Path(args.run_dir), args.scope, args.platform or None)
    print(json.dumps({
        "planned": True,
        "output": str(path),
        "execution_profile": output["execution_profile"],
        "discovery_scope": output["discovery_scope"],
        "query_count": output["query_count"],
        "network_used": False,
        "browser_used": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
