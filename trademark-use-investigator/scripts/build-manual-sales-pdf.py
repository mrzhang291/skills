#!/usr/bin/env python3

"""Build a traceable PDF lead booklet from accepted manual platform captures."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validator_module():
    path = Path(__file__).resolve().parent / "validate-manual-capture.py"
    spec = importlib.util.spec_from_file_location("validate_manual_capture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load validate-manual-capture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_text(page, rect, text: str, size: float = 10, color=(0.10, 0.13, 0.18), align=0) -> float:
    return page.insert_textbox(rect, str(text), fontsize=size, fontname="china-s", color=color, align=align)


def add_cover(doc, config: dict, validation: dict) -> None:
    import fitz
    page = doc.new_page(width=fitz.paper_rect("a4").width, height=fitz.paper_rect("a4").height)
    add_text(page, fitz.Rect(55, 90, 540, 150), "销售平台人工检索与网页固证材料", 22, (0.10, 0.25, 0.55), 1)
    trademark = config.get("trademark") or {}
    lines = [
        f"商标：{trademark.get('name') or '-'}",
        f"注册号：{trademark.get('registration_number') or '-'}",
        f"权利人：{trademark.get('owner') or '-'}",
        f"任务完成：{validation.get('metrics', {}).get('accepted_task_count', 0)}/{validation.get('metrics', {}).get('task_count', 0)}",
        f"平台数量：{validation.get('metrics', {}).get('platform_count', 0)}",
        f"结构化筛查线索：{validation.get('metrics', {}).get('structured_export_batch_count', 0)} 批 / {validation.get('metrics', {}).get('structured_export_item_count', 0)} 条",
        "分页要求：不要求连续五页",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
    ]
    add_text(page, fitz.Rect(80, 190, 515, 410), "\n\n".join(lines), 13)
    notice = (
        "材料性质：本册记录真人在销售平台上执行查询时看到的正常结果页或明确零结果页，属于相关调查线索，"
        "不自动等同于目标商标在核定商品上的实际使用证据。验证/登录/拒绝页不进入本册。"
    )
    add_text(page, fitz.Rect(70, 520, 525, 690), notice, 11, (0.35, 0.20, 0.10))


def add_capture_summary(doc, item: dict, capture_dir: Path) -> int:
    import fitz
    page = doc.new_page(width=fitz.paper_rect("a4").width, height=fitz.paper_rect("a4").height)
    heading = f"{item.get('task_id')} · {item.get('platform')} · {item.get('capture_kind')}"
    add_text(page, fitz.Rect(45, 38, 550, 75), heading, 16, (0.10, 0.25, 0.55))
    fields = [
        f"查询词：{item.get('query') or '-'}",
        f"核定商品：{item.get('target_good') or '仅商标名'}",
        f"抓取时间：{item.get('captured_at') or '-'}",
        f"页面标题：{item.get('title') or '-'}",
        f"页面 URL：{item.get('url') or '-'}",
        f"页面 PDF SHA-256：{item.get('pdf_sha256') or '-'}",
        f"关联结构化线索：{item.get('structured_export_batch_count', 0)} 批 / {item.get('structured_export_item_count', 0)} 条（非正式证据）",
    ]
    add_text(page, fitz.Rect(45, 85, 550, 230), "\n".join(fields), 9)
    screenshot = capture_dir / "browser-window.png"
    if screenshot.is_file():
        image = fitz.Pixmap(str(screenshot))
        target = fitz.Rect(45, 250, 550, 760)
        scale = min(target.width / image.width, target.height / image.height)
        width = image.width * scale
        height = image.height * scale
        rect = fitz.Rect(
            target.x0 + (target.width - width) / 2,
            target.y0 + (target.height - height) / 2,
            target.x0 + (target.width + width) / 2,
            target.y0 + (target.height + height) / 2,
        )
        page.insert_image(rect, filename=str(screenshot), keep_proportion=True)
    return page.number


def add_index_pages(front, entries: list[dict], predicted_pages: int) -> None:
    import fitz
    for page_index in range(predicted_pages):
        page = front.new_page(width=fitz.paper_rect("a4").width, height=fitz.paper_rect("a4").height)
        add_text(page, fitz.Rect(45, 35, 550, 70), "目录", 18, (0.10, 0.25, 0.55))
        subset = entries[page_index * 24:(page_index + 1) * 24]
        y = 85
        for entry in subset:
            label = f"{entry['task_id']}  {entry['platform']}  {entry['query']}  ……  {entry['final_start_page']}"
            add_text(page, fitz.Rect(48, y, 548, y + 25), label, 9)
            y += 28


def build(run_dir: Path, min_platforms: int = 3, allow_partial: bool = False) -> tuple[Path, Path, dict]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required") from exc
    run_dir = run_dir.resolve()
    _, validation = validator_module().validate(run_dir, min_platforms)
    if not validation.get("ok") and not allow_partial:
        raise ValueError("Manual capture validation failed; complete or reopen blocked tasks before building the PDF")
    items = validation.get("accepted_items") or []
    if not items:
        raise ValueError("No accepted manual captures are available")
    config = read_json(run_dir / "run-config.json", {}) or {}

    body = fitz.open()
    entries = []
    for item in items:
        capture_dir = (run_dir / str(item["capture_path"])).resolve()
        body_start = body.page_count
        add_capture_summary(body, item, capture_dir)
        source_pdf = fitz.open(str(capture_dir / "page.pdf"))
        source_pages = source_pdf.page_count
        body.insert_pdf(source_pdf)
        source_pdf.close()
        entries.append({
            "task_id": item.get("task_id"),
            "platform": item.get("platform"),
            "query": item.get("query"),
            "target_good": item.get("target_good"),
            "capture_kind": item.get("capture_kind"),
            "capture_path": item.get("capture_path"),
            "url": item.get("url"),
            "captured_at": item.get("captured_at"),
            "source_pdf_sha256": item.get("pdf_sha256"),
            "body_start_page": body_start,
            "body_page_count": source_pages + 1,
        })

    index_page_count = max(1, math.ceil(len(entries) / 24))
    front_page_count = 1 + index_page_count
    for entry in entries:
        entry["final_start_page"] = front_page_count + entry["body_start_page"] + 1
        entry["final_end_page"] = entry["final_start_page"] + entry["body_page_count"] - 1

    final_doc = fitz.open()
    add_cover(final_doc, config, validation)
    add_index_pages(final_doc, entries, index_page_count)
    final_doc.insert_pdf(body)
    body.close()
    output_path = run_dir / "manual-sales-results.pdf"
    temporary = output_path.with_suffix(".pdf.tmp")
    final_doc.save(str(temporary), garbage=4, deflate=True)
    page_count = final_doc.page_count
    final_doc.close()
    temporary.replace(output_path)

    candidate_catalog = run_dir / "discovery" / "manual-export-candidates.json"
    structured_leads = {
        "batch_count": validation.get("metrics", {}).get("structured_export_batch_count", 0),
        "item_count": validation.get("metrics", {}).get("structured_export_item_count", 0),
        "rejected_row_count": validation.get("metrics", {}).get("structured_export_rejected_row_count", 0),
        "evidence_level": "structured_lead_not_formal_use_evidence",
        "candidate_catalog": None,
    }
    if candidate_catalog.is_file():
        structured_leads["candidate_catalog"] = {
            "path": str(candidate_catalog.relative_to(run_dir)).replace("\\", "/"),
            "size_bytes": candidate_catalog.stat().st_size,
            "sha256": sha256(candidate_catalog),
        }

    manifest = {
        "schema_version": "1.0",
        "record_type": "manual_sales_results_manifest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": config.get("run_id"),
        "evidence_level": "related_lead_not_formal_use_evidence",
        "continuous_pages_required": False,
        "validation_ok": bool(validation.get("ok")),
        "partial_preview": bool(not validation.get("ok")),
        "failed_or_blocked_pages_included": 0,
        "item_count": len(entries),
        "page_count": page_count,
        "output": output_path.name,
        "output_size_bytes": output_path.stat().st_size,
        "output_sha256": sha256(output_path),
        "structured_leads": structured_leads,
        "items": entries,
        "limitations": validation.get("limitations") or [],
    }
    manifest_path = run_dir / "manual-sales-results.manifest.json"
    atomic_write_json(manifest_path, manifest)
    return output_path, manifest_path, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the manual sales-results PDF booklet")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--min-platforms", type=int, default=3)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    try:
        output, manifest, value = build(Path(args.run_dir), args.min_platforms, args.allow_partial)
        print(json.dumps({
            "manual_sales_pdf_built": True,
            "output": str(output),
            "manifest": str(manifest),
            "page_count": value["page_count"],
            "item_count": value["item_count"],
            "validation_ok": value["validation_ok"],
        }, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"manual_sales_pdf_built": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
