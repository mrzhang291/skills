#!/usr/bin/env python3

"""Build a sales-platform candidate list without visiting destination pages."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sales_platforms import platform_for_url, product_page_kind


KEEP_QUERY_KEYS = {"id", "skuid", "goods_id", "itemid", "productid", "offerid"}


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def folded(value) -> str:
    return unicodedata.normalize("NFKC", clean(value)).casefold()


def normalized_goods(values) -> list[str]:
    output = []
    for value in values or []:
        item = re.sub(r"[（(][一二三四五六七八九十0-9]+[）)]$", "", clean(value)).strip()
        if len(item) >= 2 and item not in output:
            output.append(item)
    return output


def canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    query = [
        (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=False)
        if key.casefold() in KEEP_QUERY_KEYS
    ]
    return urlunsplit((
        parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
        urlencode(query, quote_via=quote), "",
    ))


def matched_terms(sample: str, values: list[str]) -> list[str]:
    return [value for value in values if folded(value) and folded(value) in sample]


def build(run_dir: Path, limit: int = 50) -> tuple[Path, Path, dict]:
    run_dir = run_dir.resolve()
    config = read_json(run_dir / "run-config.json")
    plan = read_json(run_dir / "discovery" / "query-plan.json")
    if not isinstance(config, dict) or not isinstance(plan, dict):
        raise FileNotFoundError("run-config.json and discovery/query-plan.json are required")
    families = [str(item.get("family") or "") for item in plan.get("items") or []]
    legacy_sales_plan = bool(families) and all(
        family.startswith(("sales_", "platform_")) or family == "general_ecommerce"
        for family in families
    )
    if plan.get("discovery_scope") != "sales_platforms" and not legacy_sales_plan:
        raise ValueError("query-plan.json must use discovery_scope=sales_platforms")

    trademark = config.get("trademark") or {}
    mark = clean(trademark.get("name"))
    owner = clean(trademark.get("owner"))
    registration = clean(trademark.get("registration_number"))
    goods = normalized_goods(trademark.get("goods_services") or [])
    plan_items = {str(item.get("query_id")): item for item in plan.get("items") or []}

    source_records = read_jsonl(run_dir / "discovery" / "results.jsonl")
    source_records.extend(read_jsonl(run_dir / "discovery" / "assisted-sales-results.jsonl"))
    source_records.extend(read_jsonl(run_dir / "discovery" / "official-api-results.jsonl"))
    candidates: dict[str, dict] = {}
    excluded = Counter()
    for record in source_records:
        url = clean(record.get("normalized_url") or record.get("result_url") or record.get("url"))
        platform, platform_config = platform_for_url(url)
        if not platform:
            excluded["not_a_supported_sales_platform"] += 1
            continue
        page_kind = product_page_kind(platform, url)
        if not page_kind:
            excluded["not_a_product_shop_or_category_page"] += 1
            continue
        title = clean(record.get("title"))
        snippet = clean(record.get("snippet"))
        sample = folded(f"{title}\n{snippet}")
        mark_match = bool(folded(mark) and folded(mark) in sample)
        owner_match = bool(folded(owner) and folded(owner) in sample)
        registration_match = bool(folded(registration) and folded(registration) in sample)
        goods_matches = matched_terms(sample, goods)
        identity_match = mark_match or owner_match or registration_match
        assisted_product = record.get("provider") in {"platform-assisted", "platform-browser"} and page_kind == "product"
        official_api_product = record.get("provider") == "official-api" and page_kind == "product"
        if not identity_match and not goods_matches and not assisted_product and not official_api_product:
            excluded["no_identity_or_goods_signal_in_search_result"] += 1
            continue

        if identity_match and goods_matches:
            match_level = "identity_and_goods"
            verification_state = "search_snippet_match"
        elif identity_match:
            match_level = "identity_only"
            verification_state = "search_snippet_match_needs_goods_review"
        elif goods_matches:
            match_level = "query_context_and_goods_only"
            verification_state = "unverified_query_hit"
        elif assisted_product:
            match_level = "direct_platform_query_only"
            verification_state = "unverified_direct_search_hit"
        else:
            match_level = "official_api_query_context"
            verification_state = "official_api_hit_needs_visual_review"

        rank = max(1, int(record.get("rank") or 999))
        score = (
            (80 if registration_match else 0)
            + (70 if owner_match else 0)
            + (45 if mark_match else 0)
            + min(30, len(goods_matches) * 10)
            + (18 if official_api_product else 0)
            + (12 if assisted_product else 0)
            + (8 if page_kind == "product" else 3)
            + max(0, 7 - min(rank, 7))
        )
        canonical = canonical_url(url)
        item = {
            "platform": platform,
            "platform_label": platform_config["label"],
            "page_kind": page_kind,
            "url": canonical,
            "title": title,
            "snippet": snippet[:1000],
            "match_level": match_level,
            "verification_state": verification_state,
            "is_formal_evidence": False,
            "discovery_channel": "official_api" if official_api_product else "web_or_platform_search",
            "api_name": record.get("api_name"),
            "api_scope": record.get("api_scope"),
            "product_id": record.get("product_id"),
            "shop_name": record.get("shop_name"),
            "image_urls": record.get("image_urls") or [],
            "raw_response_sha256": record.get("raw_response_sha256"),
            "score": score,
            "matched_signals": {
                "trademark": mark if mark_match else None,
                "owner": owner if owner_match else None,
                "registration_number": registration if registration_match else None,
                "goods": goods_matches,
            },
            "discovery": [{
                "query_id": record.get("query_id"),
                "query": record.get("query"),
                "provider": record.get("provider"),
                "rank": record.get("rank"),
            }],
        }
        prior = candidates.get(canonical)
        if prior:
            prior["discovery"].extend(item["discovery"])
            if score > prior["score"]:
                item["discovery"] = prior["discovery"]
                candidates[canonical] = item
        else:
            candidates[canonical] = item

    ordered = sorted(
        candidates.values(),
        key=lambda item: (-item["score"], item["platform"], item["url"]),
    )[:max(1, limit)]
    for index, item in enumerate(ordered, start=1):
        item["candidate_id"] = f"SP{index:03d}"

    counts = Counter(item["platform_label"] for item in ordered)
    verification_counts = Counter(item["verification_state"] for item in ordered)
    output = {
        "schema_version": "1.0",
        "record_type": "trademark_sales_platform_search_results",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": config.get("run_id"),
        "execution_profile": config.get("execution_profile"),
        "discovery_scope": "sales_platforms",
        "trademark": {"name": mark, "registration_number": registration, "owner": owner, "goods_services": goods},
        "result_count": len(ordered),
        "platform_count": len(counts),
        "counts_by_platform": dict(sorted(counts.items())),
        "counts_by_verification_state": dict(sorted(verification_counts.items())),
        "excluded_counts": dict(sorted(excluded.items())),
        "limitations": [
            "这些是官方API或网页索引发现的销售平台候选链接，不是已完成网页取证的正式证据。",
            "联盟目录、精选目录或授权店铺接口均可能不是平台全量目录；API未返回不等于平台不存在。",
            "unverified_query_hit 只在商品词上命中，商标来自查询上下文，必须人工打开复核。",
            "目标平台的登录页、验证码或访问限制不会被自动绕过，也不会导致候选链接从本清单消失。",
        ],
        "items": ordered,
    }
    discovery_dir = run_dir / "discovery"
    json_path = discovery_dir / "sales-platform-results.json"
    md_path = discovery_dir / "sales-platform-results.md"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# {mark or '商标'}销售平台搜索结果",
        "",
        f"共找到 {len(ordered)} 个候选链接，覆盖 {len(counts)} 个销售平台。",
        "",
        "> 候选链接来自搜索引擎索引，不等同于已核验的商标使用证据；目标站验证码不会被自动绕过。",
        "",
        "| 序号 | 平台 | 页面 | 匹配状态 | 标题 | 链接 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in ordered:
        title = item["title"].replace("|", "\\|") or "（无标题）"
        lines.append(
            f"| {item['candidate_id']} | {item['platform_label']} | {item['page_kind']} | "
            f"{item['verification_state']} | {title} | [打开]({item['url']}) |"
        )
    if not ordered:
        lines.extend(["", "本次没有保留下可复核的销售平台候选链接。"])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path, output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a sales-platform-only candidate report")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    json_path, md_path, output = build(Path(args.run_dir), args.limit)
    print(json.dumps({
        "built": True,
        "result_count": output["result_count"],
        "platform_count": output["platform_count"],
        "json": str(json_path),
        "markdown": str(md_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
