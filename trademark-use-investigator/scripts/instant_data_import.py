#!/usr/bin/env python3

"""Import a local Instant Data Scraper CSV/XLSX export into a manual capture run."""

from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timezone
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sales_platforms import allowed_url


MAX_EXPORT_BYTES = 100 * 1024 * 1024
SUPPORTED_SUFFIXES = {".csv", ".xlsx"}
URL_PATTERN = re.compile(r"(?:https?:)?//[^\s\"'<>]+", re.IGNORECASE)
TRACKING_PARAMETERS = {
    "abbucket", "ali_refid", "ali_trackid", "clickid", "initiative_id", "ns", "pvid",
    "scm", "spm", "traceid", "ut_sk", "utparam", "wh_pid",
}
FIELD_ALIASES = {
    "title": {
        "title", "producttitle", "productname", "itemtitle", "itemname", "name",
        "标题", "商品标题", "商品名称", "宝贝标题", "宝贝名称", "名称",
    },
    "url": {
        "url", "link", "producturl", "productlink", "itemurl", "itemlink", "detailurl",
        "商品链接", "宝贝链接", "详情链接", "链接", "网址",
    },
    "price": {
        "price", "currentprice", "saleprice", "sellingprice", "finalprice", "discountprice",
        "价格", "售价", "现价", "到手价", "促销价", "优惠价",
    },
    "shop": {
        "shop", "shopname", "seller", "sellername", "store", "storename", "merchant",
        "店铺", "店铺名称", "卖家", "卖家名称", "商家", "商户",
    },
    "sales": {
        "sales", "salescount", "sold", "soldcount", "volume", "orders", "dealcount",
        "销量", "销售量", "已售", "付款人数", "成交量", "订单数",
    },
    "image_url": {
        "image", "images", "imageurl", "imageurls", "productimage", "picture", "picurl", "imgsrc",
        "图片", "商品图片", "主图", "图片链接", "主图链接",
    },
    "location": {
        "location", "origin", "shipfrom", "sellerlocation", "area",
        "地区", "所在地", "发货地", "产地", "位置",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path, base: Path) -> dict:
    return {
        "path": str(path.relative_to(base)).replace("\\", "/"),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip()


def normalized_header(value) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", clean(value).casefold())


def safe_filename(value: str, suffix: str) -> str:
    stem = Path(value or "instant-data-export").stem
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", stem).strip(".-")
    return (stem or "instant-data-export")[:100] + suffix


def decode_csv(data: bytes) -> tuple[list[list[str]], str, str]:
    last_error = None
    text = None
    encoding = None
    for candidate in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = data.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    if text is None or encoding is None:
        raise ValueError(f"CSV encoding is unsupported: {last_error}")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    rows = [[clean(cell) for cell in row] for row in csv.reader(StringIO(text), delimiter=delimiter)]
    return rows, encoding, delimiter


def decode_xlsx(data: bytes) -> tuple[list[list[str]], str, str]:
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError("openpyxl is required for XLSX imports; export CSV instead or install openpyxl") from exc
    workbook = openpyxl.load_workbook(BytesIO(data), read_only=False, data_only=True)
    try:
        sheet = workbook.active
        rows = []
        for row in sheet.iter_rows():
            values = []
            for cell in row:
                value = clean(cell.value)
                hyperlink = getattr(cell, "hyperlink", None)
                target = clean(getattr(hyperlink, "target", "")) if hyperlink else ""
                if target and target not in value:
                    value = f"{value} {target}".strip()
                values.append(value)
            rows.append(values)
        return rows, f"xlsx:{sheet.title}", ","
    finally:
        workbook.close()


def table_from_bytes(data: bytes, suffix: str) -> tuple[list[str], list[dict], dict]:
    rows, encoding, delimiter = decode_csv(data) if suffix == ".csv" else decode_xlsx(data)
    rows = [row for row in rows if any(clean(cell) for cell in row)]
    if len(rows) < 2:
        raise ValueError("The export does not contain a header and at least one data row")
    header_index = 0
    headers = []
    seen = {}
    for index, value in enumerate(rows[header_index]):
        name = clean(value) or f"column_{index + 1}"
        count = seen.get(name, 0) + 1
        seen[name] = count
        headers.append(name if count == 1 else f"{name}_{count}")
    records = []
    for row_number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        padded = [*row, *([""] * max(0, len(headers) - len(row)))]
        records.append({
            "row_number": row_number,
            "values": {headers[index]: clean(padded[index]) for index in range(len(headers))},
        })
    return headers, records, {"encoding": encoding, "delimiter": delimiter, "header_row": header_index + 1}


def mapping_for_headers(headers: list[str]) -> dict[str, str | None]:
    normalized = {header: normalized_header(header) for header in headers}
    mapping = {}
    for field, aliases in FIELD_ALIASES.items():
        exact = next((header for header, value in normalized.items() if value in aliases), None)
        if exact is not None:
            mapping[field] = exact
            continue
        contains = next(
            (header for header, value in normalized.items() if any(len(alias) >= 4 and alias in value for alias in aliases)),
            None,
        )
        mapping[field] = contains
    return mapping


def urls_in(value: str) -> list[str]:
    output = []
    for match in URL_PATTERN.findall(clean(value)):
        url = match.rstrip(".,;:)]}，。；：）】")
        if url.startswith("//"):
            url = "https:" + url
        output.append(url)
    return output


def is_image_url(url: str) -> bool:
    parsed = urlsplit(url)
    path = parsed.path.casefold()
    host = (parsed.hostname or "").casefold()
    return host.startswith("img.") or "alicdn" in host or path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"))


def canonical_url(url: str) -> str:
    parsed = urlsplit(clean(url))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold()
    port = f":{parsed.port}" if parsed.port else ""
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_folded = key.casefold()
        if key_folded.startswith("utm_") or key_folded in TRACKING_PARAMETERS:
            continue
        query.append((key, value))
    query.sort(key=lambda item: (item[0].casefold(), item[1]))
    return urlunsplit((
        "https", host + port, parsed.path or "/",
        urlencode(query, doseq=True, quote_via=quote), "",
    ))


def mapped_value(values: dict, mapping: dict, field: str) -> str:
    header = mapping.get(field)
    return clean(values.get(header)) if header else ""


def select_product_url(values: dict, mapping: dict, allowed_domains: list[str]) -> tuple[str, str | None]:
    candidates = []
    preferred = mapped_value(values, mapping, "url")
    if preferred:
        candidates.extend(urls_in(preferred))
    for value in values.values():
        candidates.extend(urls_in(value))
    seen = set()
    wrong_domain = None
    for candidate in candidates:
        normalized = canonical_url(candidate)
        if not normalized or normalized in seen or is_image_url(normalized):
            continue
        seen.add(normalized)
        if allowed_url(normalized, allowed_domains):
            return normalized, None
        wrong_domain = wrong_domain or normalized
    return "", wrong_domain


def select_image_url(values: dict, mapping: dict) -> str:
    preferred = mapped_value(values, mapping, "image_url")
    candidates = urls_in(preferred) if preferred else []
    for value in values.values():
        candidates.extend(urls_in(value))
    return next((canonical_url(url) for url in candidates if is_image_url(url) and canonical_url(url)), "")


def fallback_title(values: dict) -> str:
    candidates = []
    for value in values.values():
        text = clean(value)
        if not text or urls_in(text) or re.fullmatch(r"[￥$¥€]?\s*\d[\d,.]*", text):
            continue
        candidates.append(text)
    return max(candidates, key=len, default="")[:1000]


def item_key(item: dict) -> str:
    basis = item.get("url") or "|".join(
        clean(item.get(field)).casefold() for field in ("title", "shop", "price")
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def normalized_items(records: list[dict], mapping: dict, task: dict, trademark_name: str) -> tuple[list[dict], list[dict], int]:
    accepted = []
    rejected = []
    duplicate_count = 0
    seen = set()
    mark_folded = clean(trademark_name).casefold()
    good_folded = clean(task.get("target_good")).casefold()
    for record in records:
        values = record["values"]
        product_url, wrong_domain = select_product_url(values, mapping, task.get("allowed_domains") or [])
        title = mapped_value(values, mapping, "title") or fallback_title(values)
        shop = mapped_value(values, mapping, "shop")
        if wrong_domain and not product_url:
            rejected.append({
                "row_number": record["row_number"],
                "reason": "product_url_outside_task_platform",
                "url": wrong_domain,
                "raw": values,
            })
            continue
        if not title and not product_url:
            rejected.append({
                "row_number": record["row_number"],
                "reason": "no_product_title_or_url",
                "raw": values,
            })
            continue
        haystack = f"{title} {shop}".casefold()
        mark_match = bool(mark_folded and mark_folded in haystack)
        good_match = bool(good_folded and good_folded in haystack)
        item = {
            "schema_version": "1.0",
            "record_type": "manual_structured_sales_lead",
            "source_tool": "instant_data_scraper",
            "task_id": task.get("task_id"),
            "platform": task.get("platform"),
            "platform_label": task.get("platform_label"),
            "query": task.get("query"),
            "target_good": task.get("target_good"),
            "source_row_number": record["row_number"],
            "title": title[:1000],
            "url": product_url,
            "price": mapped_value(values, mapping, "price")[:200],
            "shop": shop[:500],
            "sales": mapped_value(values, mapping, "sales")[:200],
            "image_url": select_image_url(values, mapping),
            "location": mapped_value(values, mapping, "location")[:300],
            "trademark_text_match": mark_match,
            "target_good_text_match": good_match,
            "review_status": "text_match_needs_visual_review" if mark_match else (
                "unreviewed_target_good_candidate" if good_match else "unreviewed_export_item"
            ),
            "evidence_level": "structured_lead_not_formal_use_evidence",
        }
        key = item_key(item)
        item["candidate_key"] = key
        item["candidate_id"] = "MEC-" + key[:12].upper()
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        accepted.append(item)
    return accepted, rejected, duplicate_count


def write_csv(path: Path, items: list[dict]) -> None:
    fields = [
        "candidate_id", "task_id", "task_ids", "platform", "query", "queries", "target_good", "target_goods", "title", "url", "price",
        "shop", "sales", "image_url", "location", "trademark_text_match",
        "target_good_text_match", "review_status", "source_row_number", "source_count",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)


def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def existing_import(run_dir: Path, task_id: str, digest: str) -> dict | None:
    path = run_dir / "discovery" / "manual-export-imports.jsonl"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("task_id") == task_id and record.get("raw_sha256") == digest:
            manifest_path = run_dir / str(record.get("manifest_path") or "")
            if manifest_path.is_file():
                return read_json(manifest_path, {}) or {}
    return None


def rebuild_candidates(run_dir: Path) -> tuple[Path, Path, dict]:
    export_root = run_dir / "discovery" / "manual-exports"
    by_key = {}
    import_count = 0
    for path in sorted(export_root.glob("*/*/normalized-items.json")) if export_root.is_dir() else []:
        value = read_json(path, {}) or {}
        import_count += 1
        for item in value.get("items") or []:
            key = str(item.get("candidate_key") or item_key(item))
            if key not in by_key:
                merged = dict(item)
                merged["task_ids"] = [item.get("task_id")] if item.get("task_id") else []
                merged["queries"] = [item.get("query")] if item.get("query") else []
                merged["target_goods"] = [item.get("target_good")] if item.get("target_good") else []
                merged["source_count"] = 1
                by_key[key] = merged
                continue
            merged = by_key[key]
            merged["source_count"] = int(merged.get("source_count") or 1) + 1
            for source_field, aggregate_field in (
                ("task_id", "task_ids"), ("query", "queries"), ("target_good", "target_goods")
            ):
                value = item.get(source_field)
                if value and value not in merged[aggregate_field]:
                    merged[aggregate_field].append(value)
            merged["trademark_text_match"] = bool(
                merged.get("trademark_text_match") or item.get("trademark_text_match")
            )
            merged["target_good_text_match"] = bool(
                merged.get("target_good_text_match") or item.get("target_good_text_match")
            )
    items = sorted(by_key.values(), key=lambda item: (
        str(item.get("platform") or ""), str(item.get("task_id") or ""),
        str(item.get("title") or "").casefold(), str(item.get("url") or ""),
    ))
    output = {
        "schema_version": "1.0",
        "record_type": "manual_structured_sales_candidates",
        "generated_at": utc_now(),
        "evidence_level": "structured_lead_not_formal_use_evidence",
        "source_tool": "instant_data_scraper",
        "import_count": import_count,
        "candidate_count": len(items),
        "items": items,
    }
    json_path = run_dir / "discovery" / "manual-export-candidates.json"
    csv_path = run_dir / "discovery" / "manual-export-candidates.csv"
    atomic_write_json(json_path, output)
    write_csv(csv_path, items)
    return json_path, csv_path, output


def import_export_bytes(
    run_dir: Path,
    data: bytes,
    filename: str,
    task_id: str | None = None,
    force: bool = False,
) -> dict:
    run_dir = run_dir.resolve()
    if not data or len(data) > MAX_EXPORT_BYTES:
        raise ValueError(f"Export is empty or exceeds {MAX_EXPORT_BYTES} bytes")
    suffix = Path(filename or "").suffix.casefold()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError("Only Instant Data Scraper .csv and .xlsx exports are supported")
    queue_path = run_dir / "discovery" / "manual-capture-queue.json"
    state_path = run_dir / "discovery" / "manual-capture-state.json"
    config = read_json(run_dir / "run-config.json", {}) or {}
    queue = read_json(queue_path)
    state = read_json(state_path)
    if not isinstance(queue, dict) or not isinstance(state, dict):
        raise FileNotFoundError("Build the manual capture queue before importing a structured export")
    selected_task_id = clean(task_id) or clean(state.get("current_task_id"))
    tasks = {str(item.get("task_id")): item for item in queue.get("items") or []}
    task = tasks.get(selected_task_id)
    if task is None:
        raise ValueError(f"Unknown or unavailable task_id: {selected_task_id!r}")
    digest = sha256_bytes(data)
    duplicate = existing_import(run_dir, selected_task_id, digest)
    if duplicate and not force:
        duplicate = dict(duplicate)
        duplicate["duplicate_import"] = True
        return duplicate

    import_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + digest[:10]
    import_dir = run_dir / "discovery" / "manual-exports" / selected_task_id / import_id
    import_dir.mkdir(parents=True, exist_ok=False)
    raw_name = safe_filename(filename, suffix)
    raw_path = import_dir / raw_name
    raw_path.write_bytes(data)

    headers, records, parser = table_from_bytes(data, suffix)
    mapping = mapping_for_headers(headers)
    trademark_name = clean((config.get("trademark") or {}).get("name"))
    items, rejected, duplicate_count = normalized_items(records, mapping, task, trademark_name)
    normalized_json = import_dir / "normalized-items.json"
    normalized_csv = import_dir / "normalized-items.csv"
    rejected_json = import_dir / "rejected-rows.json"
    atomic_write_json(normalized_json, {"schema_version": "1.0", "items": items})
    write_csv(normalized_csv, items)
    atomic_write_json(rejected_json, {"schema_version": "1.0", "items": rejected})
    artifacts = {
        "raw_export": file_record(raw_path, import_dir),
        "normalized_json": file_record(normalized_json, import_dir),
        "normalized_csv": file_record(normalized_csv, import_dir),
        "rejected_rows_json": file_record(rejected_json, import_dir),
    }
    relative_dir = str(import_dir.relative_to(run_dir)).replace("\\", "/")
    manifest = {
        "schema_version": "1.0",
        "record_type": "manual_structured_export_import",
        "import_id": import_id,
        "imported_at": utc_now(),
        "run_id": queue.get("run_id"),
        "source_tool": "instant_data_scraper",
        "source_filename": filename,
        "source_format": suffix.lstrip("."),
        "raw_sha256": digest,
        "raw_size_bytes": len(data),
        "task_id": selected_task_id,
        "platform": task.get("platform"),
        "platform_label": task.get("platform_label"),
        "query": task.get("query"),
        "target_good": task.get("target_good"),
        "headers": headers,
        "column_mapping": mapping,
        "parser": parser,
        "input_row_count": len(records),
        "normalized_item_count": len(items),
        "duplicate_row_count": duplicate_count,
        "rejected_row_count": len(rejected),
        "trademark_text_match_count": sum(bool(item.get("trademark_text_match")) for item in items),
        "target_good_text_match_count": sum(bool(item.get("target_good_text_match")) for item in items),
        "evidence_level": "structured_lead_not_formal_use_evidence",
        "marks_task_complete": False,
        "import_dir": relative_dir,
        "manifest_path": f"{relative_dir}/import-manifest.json",
        "artifacts": artifacts,
        "warnings": [
            "结构化导出只作为调查线索，不替代搜索结果页固证。",
            "文本匹配不自动证明商标近似、商品对应、主体关系或实际使用。",
        ],
    }
    manifest_path = import_dir / "import-manifest.json"
    atomic_write_json(manifest_path, manifest)

    task.setdefault("data_exports", []).append({
        "import_id": import_id,
        "manifest_path": manifest["manifest_path"],
        "raw_sha256": digest,
        "item_count": len(items),
        "rejected_row_count": len(rejected),
        "imported_at": manifest["imported_at"],
    })
    task["structured_export_batch_count"] = len(task["data_exports"])
    task["structured_export_item_count"] = sum(int(item.get("item_count") or 0) for item in task["data_exports"])
    queue["updated_at"] = utc_now()
    state["updated_at"] = utc_now()
    state["structured_export_batch_count"] = sum(len(item.get("data_exports") or []) for item in tasks.values())
    state["structured_export_item_count"] = sum(
        sum(int(record.get("item_count") or 0) for record in item.get("data_exports") or [])
        for item in tasks.values()
    )
    atomic_write_json(queue_path, queue)
    atomic_write_json(state_path, state)
    append_jsonl(run_dir / "discovery" / "manual-export-imports.jsonl", {
        "import_id": import_id,
        "task_id": selected_task_id,
        "platform": task.get("platform"),
        "query": task.get("query"),
        "raw_sha256": digest,
        "item_count": len(items),
        "manifest_path": manifest["manifest_path"],
    })
    candidates_json, candidates_csv, candidates = rebuild_candidates(run_dir)
    manifest["candidate_catalog"] = {
        "json": str(candidates_json.relative_to(run_dir)).replace("\\", "/"),
        "csv": str(candidates_csv.relative_to(run_dir)).replace("\\", "/"),
        "candidate_count": candidates["candidate_count"],
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def import_export_file(run_dir: Path, input_path: Path, task_id: str | None = None, force: bool = False) -> dict:
    input_path = input_path.resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Export file not found: {input_path}")
    return import_export_bytes(run_dir, input_path.read_bytes(), input_path.name, task_id, force)


def decode_upload(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("content_base64 is required")
    if value.startswith("data:"):
        marker = value.find(",")
        if marker < 0:
            raise ValueError("Malformed data URL")
        value = value[marker + 1:]
    estimated = len(value) * 3 // 4
    if estimated > MAX_EXPORT_BYTES:
        raise ValueError(f"Export exceeds {MAX_EXPORT_BYTES} bytes")
    data = base64.b64decode(value, validate=True)
    if not data or len(data) > MAX_EXPORT_BYTES:
        raise ValueError(f"Export is empty or exceeds {MAX_EXPORT_BYTES} bytes")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a local Instant Data Scraper CSV/XLSX export")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        result = import_export_file(Path(args.run_dir), Path(args.input), args.task_id, args.force)
        print(json.dumps({
            "instant_data_export_imported": True,
            "duplicate_import": bool(result.get("duplicate_import")),
            "task_id": result.get("task_id"),
            "normalized_item_count": result.get("normalized_item_count"),
            "rejected_row_count": result.get("rejected_row_count"),
            "manifest": result.get("manifest_path"),
            "candidate_catalog": result.get("candidate_catalog"),
        }, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"instant_data_export_imported": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
