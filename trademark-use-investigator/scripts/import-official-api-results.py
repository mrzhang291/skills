#!/usr/bin/env python3

"""Normalize a saved official sales-platform API response into discovery records."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sales_platforms import PLATFORMS


ALLOWED_METHODS = {
    "taobao": {"taobao.tbk.dg.material.optional.upgrade", "taobao.tbk.item.info.get"},
    "tmall": {"tmall.selected.items.search", "tmall.items.extend.search"},
    "jd": {"jd.union.open.goods.query", "jd.union.open.goods.bigfield.query", "jd.union.open.goods.promotiongoodsinfo.query"},
    "1688": {"alibaba.product.search", "alibaba.product.get"},
    "pinduoduo": {"pdd.ddk.goods.search", "pdd.ddk.goods.detail", "pdd.goods.list.get", "pdd.goods.information.get"},
    "vip": {"UnionGoodsService.goodsList", "UnionGoodsService.goodsListV2"},
    "douyin": {"/product/listV2", "/product/detail"},
    "kuaishou": {"商品管理接口族"},
    "xiaohongshu": {"商家商品管理接口族"},
}

ID_ALIASES = (
    "product_id", "productid", "product_id_str", "item_id", "itemid", "num_iid",
    "goods_id", "goodsid", "sku_id", "skuid", "ware_id", "offer_id", "offerid",
)
TITLE_ALIASES = ("title", "product_name", "productname", "goods_name", "goodsname", "item_name", "itemname", "sku_name", "skuname", "subject")
URL_ALIASES = ("product_url", "producturl", "item_url", "itemurl", "goods_url", "goodsurl", "detail_url", "detailurl", "click_url", "clickurl", "url")
SHOP_ALIASES = ("shop_name", "shopname", "mall_name", "mallname", "store_name", "storename", "seller_name", "sellername", "supplier_name", "suppliername", "company_name", "companyname")
LIST_KEYS = ("items", "goods_list", "goodslist", "map_data", "mapdata", "resultlist", "goodsinfolist", "productlist", "skulist", "list")


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_http_url(value) -> str | None:
    text = clean(value)
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else None


def decoded(value):
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def candidate_lists(value, path: str = "root") -> list[tuple[int, str, list[dict]]]:
    value = decoded(value)
    output = []
    if isinstance(value, list):
        records = [item for item in value if isinstance(decoded(item), dict)]
        if records:
            score = sum(
                any(clean(key).casefold() in set(ID_ALIASES + TITLE_ALIASES + URL_ALIASES) for key in record)
                for record in records
            )
            output.append((score, path, [decoded(item) for item in records]))
        for index, item in enumerate(value):
            output.extend(candidate_lists(item, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            bonus = 10 if clean(key).casefold() in LIST_KEYS else 0
            nested = candidate_lists(item, f"{path}.{key}")
            output.extend((score + bonus, nested_path, records) for score, nested_path, records in nested)
    return output


def flatten(record: dict) -> dict[str, list]:
    output: dict[str, list] = {}

    def walk(value, key_hint: str = "") -> None:
        value = decoded(value)
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, clean(key).casefold())
        elif isinstance(value, list):
            for item in value:
                walk(item, key_hint)
        elif value is not None and key_hint:
            output.setdefault(key_hint, []).append(value)

    walk(record)
    return output


def first(flat: dict[str, list], aliases) -> str:
    for alias in aliases:
        for value in flat.get(alias, []):
            text = clean(value)
            if text:
                return text
    return ""


def product_url(platform: str, flat: dict[str, list], product_id: str) -> str | None:
    for alias in URL_ALIASES:
        for value in flat.get(alias, []):
            url = safe_http_url(value)
            if url:
                return url
    if not product_id:
        return None
    if platform in {"taobao", "tmall"}:
        return f"https://item.taobao.com/item.htm?id={product_id}"
    if platform == "jd":
        return f"https://item.jd.com/{product_id}.html"
    if platform == "1688":
        return f"https://detail.1688.com/offer/{product_id}.html"
    if platform == "pinduoduo":
        return f"https://mobile.yangkeduo.com/goods.html?goods_id={product_id}"
    return None


def image_urls(flat: dict[str, list]) -> list[str]:
    output = []
    for key, values in flat.items():
        folded = key.casefold()
        if not any(token in folded for token in ("image", "img", "pic", "thumb", "material")):
            continue
        for value in values:
            url = safe_http_url(value)
            if url and url not in output:
                output.append(url)
    return output[:20]


def normalize_records(platform: str, raw, query_id: str, query: str, target_good: str, api_name: str, raw_sha256: str) -> tuple[list[dict], dict]:
    lists = candidate_lists(raw)
    if not lists:
        return [], {"record_list_path": None, "raw_record_count": 0, "excluded_without_url": 0}
    _, record_path, records = max(lists, key=lambda item: (item[0], len(item[2])))
    normalized = []
    excluded_without_url = 0
    for rank, record in enumerate(records, start=1):
        flat = flatten(record)
        product_id = first(flat, ID_ALIASES)
        url = product_url(platform, flat, product_id)
        if not url:
            excluded_without_url += 1
            continue
        title = first(flat, TITLE_ALIASES)
        shop_name = first(flat, SHOP_ALIASES)
        normalized.append({
            "query_id": query_id,
            "query": query,
            "target_good": target_good,
            "provider": "official-api",
            "source_kind": "official_platform_api",
            "api_name": api_name,
            "api_scope": PLATFORMS[platform].get("api_scope", "permission_specific"),
            "platform": platform,
            "rank": rank,
            "title": title,
            "snippet": clean(" | ".join(item for item in (shop_name, target_good) if item)),
            "url": url,
            "normalized_url": url,
            "product_id": product_id or None,
            "shop_name": shop_name or None,
            "image_urls": image_urls(flat),
            "raw_response_sha256": raw_sha256,
            "raw_record_index": rank - 1,
        })
    return normalized, {
        "record_list_path": record_path,
        "raw_record_count": len(records),
        "excluded_without_url": excluded_without_url,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a saved response from an official sales-platform API")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--platform", required=True, choices=sorted(ALLOWED_METHODS))
    parser.add_argument("--api-name", required=True)
    parser.add_argument("--raw-file", required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--target-good", default="")
    parser.add_argument("--allow-zero-results", action="store_true")
    args = parser.parse_args()

    if args.api_name not in ALLOWED_METHODS[args.platform]:
        allowed = ", ".join(sorted(ALLOWED_METHODS[args.platform]))
        raise ValueError(f"Unsupported official API method for {args.platform}: {args.api_name}; allowed: {allowed}")
    run_dir = Path(args.run_dir).resolve()
    raw_file = Path(args.raw_file).resolve()
    if not raw_file.is_file():
        raise FileNotFoundError(raw_file)
    raw_bytes = raw_file.read_bytes()
    raw_sha = sha256_bytes(raw_bytes)
    raw = json.loads(raw_bytes.decode("utf-8-sig"))
    records, stats = normalize_records(
        args.platform, raw, args.query_id, args.query, args.target_good, args.api_name, raw_sha
    )
    if not records and not args.allow_zero_results:
        raise ValueError("Official API response contained no product record with a direct product URL")

    discovery = run_dir / "discovery"
    raw_dir = discovery / "raw" / "official-api"
    raw_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{args.query_id}-{args.platform}-{args.api_name}") + ".json"
    archived_raw = raw_dir / safe_name
    if raw_file != archived_raw:
        shutil.copy2(raw_file, archived_raw)

    results_path = discovery / "official-api-results.jsonl"
    existing = []
    if results_path.is_file():
        existing = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    existing = [
        item for item in existing
        if not (item.get("query_id") == args.query_id and item.get("platform") == args.platform and item.get("api_name") == args.api_name)
    ]
    existing.extend(records)
    results_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in existing), encoding="utf-8")

    import_record = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "query_id": args.query_id,
        "query": args.query,
        "target_good": args.target_good,
        "platform": args.platform,
        "api_name": args.api_name,
        "api_scope": PLATFORMS[args.platform].get("api_scope", "permission_specific"),
        "raw_file": str(archived_raw.relative_to(run_dir)).replace("\\", "/"),
        "raw_sha256": raw_sha,
        "normalized_count": len(records),
        **stats,
    }
    imports_path = discovery / "official-api-imports.jsonl"
    with imports_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(import_record, ensure_ascii=False) + "\n")
    print(json.dumps({"imported": True, **import_record, "results_file": str(results_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
