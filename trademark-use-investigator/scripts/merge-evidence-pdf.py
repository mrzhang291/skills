#!/usr/bin/env python3

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import fitz


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_manifest(args):
    if args.manifest:
        manifest_path = Path(args.manifest).resolve()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("Manifest must contain a non-empty 'items' array")
        base = manifest_path.parent
        normalized = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Manifest item {index} must be an object")
            pdf_path = Path(item["pdf"]) if item.get("pdf") else None
            if pdf_path and not pdf_path.is_absolute():
                pdf_path = base / pdf_path
            normalized.append({
                "id": item.get("id") or f"P{index:03d}",
                "label": item.get("label") or (pdf_path.stem if pdf_path else f"来源页 {index}"),
                "pdf": pdf_path.resolve() if pdf_path else None,
                "source_url": item.get("source_url"),
                "display_url": item.get("display_url") or item.get("source_url"),
                "captured_at": item.get("captured_at"),
                "page_state": item.get("page_state", "unknown"),
                "content_valid": bool(item.get("content_valid", False)),
                "failure_reason": item.get("failure_reason"),
            })
        return data.get("title") or args.title, data.get("cover") or {}, normalized, manifest_path

    if not args.pdf:
        raise ValueError("Provide --manifest or one or more --pdf arguments")
    items = []
    for index, raw_path in enumerate(args.pdf, start=1):
        pdf_path = Path(raw_path).resolve()
        items.append({
            "id": f"P{index:03d}", "label": pdf_path.stem, "pdf": pdf_path,
            "source_url": None, "display_url": None, "captured_at": None, "page_state": "normal",
            "content_valid": True, "failure_reason": None,
        })
    return args.title, {}, items, None


def relative_posix(path, base):
    return Path(os.path.relpath(Path(path).resolve(), start=Path(base).resolve())).as_posix()


def inspect_source_pdf(item, output_dir):
    path = item.get("pdf")
    source_id = item.get("id") or "<missing-id>"
    if not path:
        raise ValueError(f"Source {source_id} does not specify a PDF")
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Source PDF does not exist for {source_id}: {path}")

    source_hash = sha256(path)
    try:
        source = fitz.open(path)
    except Exception as exc:
        raise ValueError(f"Source PDF cannot be opened for {source_id}: {path}: {exc}") from exc
    try:
        if not source.is_pdf:
            raise ValueError(f"Source is not a PDF for {source_id}: {path}")
        if source.needs_pass:
            raise ValueError(f"Source PDF is encrypted for {source_id}: {path}")
        if source.page_count < 1:
            raise ValueError(f"Source PDF has no pages for {source_id}: {path}")
        for page_index in range(source.page_count):
            source.load_page(page_index)
        page_count = source.page_count
    finally:
        source.close()

    return {
        "path": path,
        "relative_path": relative_posix(path, output_dir),
        "sha256": source_hash,
        "page_count": page_count,
    }


def open_verified_source_pdf(item, prepared):
    path = prepared["path"]
    source_id = item.get("id") or "<missing-id>"
    current_hash = sha256(path)
    if current_hash != prepared["sha256"]:
        raise ValueError(f"Source PDF changed before merge for {source_id}: {path}")
    try:
        source = fitz.open(path)
    except Exception as exc:
        raise ValueError(f"Source PDF cannot be reopened for {source_id}: {path}: {exc}") from exc
    try:
        if not source.is_pdf or source.needs_pass:
            raise ValueError(f"Source PDF became unreadable before merge for {source_id}: {path}")
        if source.page_count != prepared["page_count"]:
            raise ValueError(
                f"Source PDF page count changed for {source_id}: "
                f"expected {prepared['page_count']}, got {source.page_count}"
            )
        for page_index in range(source.page_count):
            source.load_page(page_index)
    except Exception:
        source.close()
        raise
    return source


def add_cover(result, title, cover, items, ranges):
    a4 = fitz.paper_rect("a4")
    page = result.new_page(width=a4.width, height=a4.height)
    title_rect = fitz.Rect(45, 55, a4.width - 45, 145)
    title_written = False
    for font_size in (20, 18, 16):
        if page.insert_textbox(
            title_rect, title, fontname="china-s", fontsize=font_size,
            lineheight=1.2, align=fitz.TEXT_ALIGN_CENTER,
        ) >= 0:
            title_written = True
            break
    if not title_written:
        raise RuntimeError("Cover title did not fit")
    fields = [
        ("商标", cover.get("trademark_name")),
        ("注册号", cover.get("registration_number")),
        ("权利人", cover.get("owner")),
        ("调查期间", cover.get("period")),
        ("生成时间", datetime.now(timezone.utc).isoformat()),
    ]
    y = 170
    for label, value in fields:
        page.insert_text((55, y), f"{label}：{value or '未提供'}", fontname="china-s", fontsize=11)
        y += 24
    page.insert_text((55, y + 8), "调查摘要", fontname="china-s", fontsize=14)
    summary = str(cover.get("summary") or "本材料汇编本次实际访问的公开 HTML 网页及采集状态。")
    page.insert_textbox(fitz.Rect(55, y + 24, a4.width - 55, y + 105), summary, fontname="china-s", fontsize=10, lineheight=1.4)
    limitations = cover.get("limitations") or []
    if isinstance(limitations, str):
        limitations = [limitations]
    page.insert_text((55, y + 125), "范围与限制", fontname="china-s", fontsize=14)
    limit_text = "\n".join(f"• {item}" for item in limitations[:8]) or "• 搜索结果仅代表本次公开网络和工具覆盖范围。"
    page.insert_textbox(fitz.Rect(55, y + 140, a4.width - 55, a4.height - 55), limit_text, fontname="china-s", fontsize=9.5, lineheight=1.35)

    per_page = 22
    for group_index in range(math.ceil(len(items) / per_page)):
        page = result.new_page(width=a4.width, height=a4.height)
        page.insert_text((45, 45), "网页证据目录", fontname="china-s", fontsize=16)
        page.insert_text((45, 70), "ID", fontname="china-s", fontsize=9)
        page.insert_text((90, 70), "页面标题 / 状态", fontname="china-s", fontsize=9)
        page.insert_text((480, 70), "总PDF页码", fontname="china-s", fontsize=9)
        y = 92
        start = group_index * per_page
        for item, page_range in zip(items[start:start + per_page], ranges[start:start + per_page]):
            state = item.get("page_state") or "unknown"
            raw_label = f"{item['label']} [{state}]"
            label = raw_label if len(raw_label) <= 46 else raw_label[:45] + "…"
            pages = str(page_range["start_page"]) if page_range["start_page"] == page_range["end_page"] else f"{page_range['start_page']}-{page_range['end_page']}"
            page.insert_text((45, y), str(item["id"])[:12], fontname="china-s", fontsize=8)
            page.insert_text((90, y), label, fontname="china-s", fontsize=8)
            page.insert_text((500, y), pages, fontname="china-s", fontsize=8)
            url = item.get("display_url") or item.get("source_url") or ""
            short_url = url if len(url) <= 90 else url[:89] + "…"
            page.insert_text((90, y + 11), short_url, fontname="helv", fontsize=5.8, color=(0.35, 0.35, 0.35))
            y += 31


def add_placeholder(result, item):
    a4 = fitz.paper_rect("a4")
    page = result.new_page(width=a4.width, height=a4.height)
    page.insert_text((55, 75), "网页采集失败或受限", fontname="china-s", fontsize=18, color=(0.65, 0.12, 0.12))
    lines = [
        f"来源ID：{item['id']}", f"标题：{item['label']}",
        f"页面状态：{item.get('page_state') or 'unknown'}",
        f"URL：{item.get('display_url') or item.get('source_url') or '未记录'}",
        f"采集时间：{item.get('captured_at') or '未记录'}",
        f"原因：{item.get('failure_reason') or '来源 PDF 不存在'}",
    ]
    page.insert_textbox(fitz.Rect(55, 115, a4.width - 55, a4.height - 65), "\n\n".join(lines), fontname="china-s", fontsize=10, lineheight=1.35)


def main():
    parser = argparse.ArgumentParser(description="Merge printed HTML pages into one evidence PDF")
    parser.add_argument("--manifest")
    parser.add_argument("--pdf", action="append")
    parser.add_argument("--output", required=True)
    parser.add_argument("--title", default="商标公开网络调查证据材料")
    args = parser.parse_args()

    title, cover, items, manifest_path = load_manifest(args)
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prepared_sources = [inspect_source_pdf(item, output_path.parent) for item in items]
    source_counts = [item["page_count"] for item in prepared_sources]
    cover_pages = 1 + math.ceil(len(items) / 22)
    page_ranges = []
    cursor = cover_pages + 1
    for item, count, prepared in zip(items, source_counts, prepared_sources):
        pdf_path = item["pdf"]
        page_ranges.append({
            "id": item["id"], "label": item["label"],
            "source_pdf": str(pdf_path) if pdf_path else None,
            "source_pdf_relative": prepared["relative_path"],
            "source_pdf_sha256": prepared["sha256"],
            "source_page_count": count,
            "source_url": item["source_url"], "captured_at": item["captured_at"], "page_state": item["page_state"],
            "content_valid": item["content_valid"], "placeholder": False,
            "start_page": cursor, "end_page": cursor + count - 1,
        })
        cursor += count

    result = fitz.open()
    add_cover(result, title, cover, items, page_ranges)
    toc = [[1, "封面与目录", 1]]
    for item, page_range, prepared in zip(items, page_ranges, prepared_sources):
        toc.append([1, str(item["label"]), page_range["start_page"]])
        source = open_verified_source_pdf(item, prepared)
        before = result.page_count
        result.insert_pdf(source, links=True, annots=True)
        source.close()
        if result.page_count - before != prepared["page_count"]:
            raise RuntimeError(f"Merged page count is inconsistent for source {item['id']}")

    total_pages = result.page_count
    for index, page in enumerate(result, start=1):
        rect = page.rect
        footer_rect = fitz.Rect(rect.x0 + 36, rect.y1 - 38, rect.x1 - 36, rect.y1 - 25)
        remaining = page.insert_textbox(
            footer_rect, f"第 {index} 页，共 {total_pages} 页", fontname="china-s", fontsize=7.5,
            color=(0.32, 0.32, 0.32), align=fitz.TEXT_ALIGN_CENTER, overlay=True,
        )
        if remaining < 0:
            raise RuntimeError(f"Global page number did not fit on page {index}")

    result.set_toc(toc)
    result.set_metadata({
        "title": title, "subject": "商标公开网络目标网页证据汇编（PDF为离线HTML的派生阅读件）",
        "creator": "trademark-use-investigator", "producer": "PyMuPDF",
        "creationDate": datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ"),
    })
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()
    result.save(temp_path, garbage=4, deflate=True)
    result.close()
    temp_path.replace(output_path)

    verification = fitz.open(output_path)
    source_bindings = [{
        "id": item.get("id"),
        "source_pdf_relative": item.get("source_pdf_relative"),
        "source_pdf_sha256": item.get("source_pdf_sha256"),
        "source_page_count": item.get("source_page_count"),
        "start_page": item.get("start_page"),
        "end_page": item.get("end_page"),
        "placeholder": item.get("placeholder"),
    } for item in page_ranges]
    build = {
        "schema_version": "2.0", "title": title,
        "manifest": str(manifest_path) if manifest_path else None, "output": str(output_path),
        "capture_order_sha256": sha256(manifest_path) if manifest_path and manifest_path.is_file() else None,
        "source_bindings_sha256": canonical_json_sha256(source_bindings),
        "source_bindings": source_bindings,
        "page_count": verification.page_count,
        "selectable_text_chars": sum(len(page.get_text("text")) for page in verification),
        "link_count": sum(len(page.get_links()) for page in verification),
        "sha256": sha256(output_path), "created_at": datetime.now(timezone.utc).isoformat(),
        "cover_pages": cover_pages, "page_ranges": page_ranges,
    }
    verification.close()
    build_path = output_path.with_suffix(".build.json")
    build_path.write_text(json.dumps(build, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_path), "build_record": str(build_path), **{k: build[k] for k in ("page_count", "selectable_text_chars", "link_count", "sha256")}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
