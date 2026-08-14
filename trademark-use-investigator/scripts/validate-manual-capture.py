#!/usr/bin/env python3

"""Validate the human-operated sales-platform evidence queue and artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


REQUIRED_FILES = {
    "browser-window.png",
    "visible.png",
    "fullpage.png",
    "page.pdf",
    "page.mhtml",
    "rendered-dom.html",
    "body-text.txt",
    "metadata.json",
    "hashes.json",
}
FINAL_STATUSES = {"result", "zero"}


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


def validate_capture(run_dir: Path, task: dict, capture: dict) -> tuple[bool, list[str], dict]:
    errors = []
    relative = str(capture.get("path") or "")
    capture_dir = (run_dir / relative).resolve()
    allowed_root = (run_dir / "manual-capture" / "pages").resolve()
    if not capture_dir.is_relative_to(allowed_root):
        errors.append("accepted capture is outside manual-capture/pages")
        return False, errors, {}
    if not capture_dir.is_dir():
        errors.append("capture directory is missing")
        return False, errors, {}
    missing = sorted(name for name in REQUIRED_FILES if not (capture_dir / name).is_file())
    if missing:
        errors.append("missing files: " + ", ".join(missing))

    metadata = read_json(capture_dir / "metadata.json", {}) or {}
    if metadata.get("accepted") is not True:
        errors.append("metadata.accepted is not true")
    if metadata.get("task_id") != task.get("task_id"):
        errors.append("metadata task_id mismatch")
    if metadata.get("capture_kind") != task.get("status"):
        errors.append("capture kind does not match final task status")
    if metadata.get("domain_ok") is not True:
        errors.append("captured URL is outside the task platform")
    if metadata.get("webdriver_navigation") is not False:
        errors.append("webdriver_navigation must be false")
    if metadata.get("cookie_exported") is not False:
        errors.append("cookie_exported must be false")

    hash_manifest = read_json(capture_dir / "hashes.json", {}) or {}
    records = hash_manifest.get("files") or {}
    for filename in sorted(REQUIRED_FILES - {"hashes.json"}):
        path = capture_dir / filename
        record = records.get(filename)
        if not path.is_file() or not isinstance(record, dict):
            errors.append(f"hash record missing: {filename}")
            continue
        if int(record.get("size_bytes") or -1) != path.stat().st_size:
            errors.append(f"size mismatch: {filename}")
        if str(record.get("sha256") or "").lower() != sha256(path):
            errors.append(f"sha256 mismatch: {filename}")
    summary = {
        "task_id": task.get("task_id"),
        "platform": task.get("platform"),
        "query": task.get("query"),
        "target_good": task.get("target_good"),
        "capture_kind": task.get("status"),
        "capture_path": relative,
        "captured_at": metadata.get("captured_at"),
        "url": metadata.get("url"),
        "title": metadata.get("title"),
        "pdf_sha256": sha256(capture_dir / "page.pdf") if (capture_dir / "page.pdf").is_file() else None,
    }
    return not errors, errors, summary


def validate_structured_exports(run_dir: Path, task: dict) -> tuple[list[str], list[dict]]:
    """Validate optional CSV/XLSX lead imports referenced by a manual task."""
    errors = []
    summaries = []
    task_id = str(task.get("task_id") or "")
    allowed_root = (run_dir / "discovery" / "manual-exports" / task_id).resolve()
    required_artifacts = {"raw_export", "normalized_json", "normalized_csv", "rejected_rows_json"}
    for index, reference in enumerate(task.get("data_exports") or [], start=1):
        label = f"structured export {index}"
        relative = str(reference.get("manifest_path") or "")
        manifest_path = (run_dir / relative).resolve()
        if not manifest_path.is_relative_to(allowed_root):
            errors.append(f"{label} manifest is outside discovery/manual-exports/{task_id}")
            continue
        if not manifest_path.is_file():
            errors.append(f"{label} manifest is missing")
            continue
        manifest = read_json(manifest_path, {}) or {}
        if not isinstance(manifest, dict):
            errors.append(f"{label} manifest is invalid")
            continue
        if manifest.get("task_id") != task_id:
            errors.append(f"{label} task_id mismatch")
        if manifest.get("platform") != task.get("platform"):
            errors.append(f"{label} platform mismatch")
        if manifest.get("query") != task.get("query"):
            errors.append(f"{label} query mismatch")
        if manifest.get("marks_task_complete") is not False:
            errors.append(f"{label} must not mark a task complete")
        if manifest.get("evidence_level") != "structured_lead_not_formal_use_evidence":
            errors.append(f"{label} evidence level is missing or invalid")
        artifacts = manifest.get("artifacts") or {}
        missing_records = sorted(required_artifacts - set(artifacts))
        if missing_records:
            errors.append(f"{label} artifact records missing: {', '.join(missing_records)}")
        for name in sorted(required_artifacts & set(artifacts)):
            record = artifacts.get(name)
            if not isinstance(record, dict):
                errors.append(f"{label} artifact record invalid: {name}")
                continue
            artifact = (manifest_path.parent / str(record.get("path") or "")).resolve()
            if not artifact.is_relative_to(manifest_path.parent.resolve()):
                errors.append(f"{label} artifact is outside import directory: {name}")
                continue
            if not artifact.is_file():
                errors.append(f"{label} artifact is missing: {name}")
                continue
            if int(record.get("size_bytes") or -1) != artifact.stat().st_size:
                errors.append(f"{label} size mismatch: {name}")
            if str(record.get("sha256") or "").lower() != sha256(artifact):
                errors.append(f"{label} sha256 mismatch: {name}")
        item_count = int(manifest.get("normalized_item_count") or 0)
        if int(reference.get("item_count") or 0) != item_count:
            errors.append(f"{label} item count mismatch")
        summaries.append({
            "import_id": manifest.get("import_id"),
            "manifest_path": relative,
            "source_filename": manifest.get("source_filename"),
            "item_count": item_count,
            "rejected_row_count": int(manifest.get("rejected_row_count") or 0),
            "raw_sha256": manifest.get("raw_sha256"),
            "imported_at": manifest.get("imported_at"),
        })
    return errors, summaries


def validate(run_dir: Path, min_platforms: int = 3) -> tuple[Path, dict]:
    run_dir = run_dir.resolve()
    queue = read_json(run_dir / "discovery" / "manual-capture-queue.json")
    if not isinstance(queue, dict):
        raise FileNotFoundError("discovery/manual-capture-queue.json is required")
    errors = []
    warnings = []
    items = queue.get("items") or []
    platforms = list(dict.fromkeys(str(item.get("platform") or "") for item in items if item.get("platform")))
    if len(platforms) < max(1, min_platforms):
        errors.append(f"platform coverage is {len(platforms)}; required {min_platforms}")
    if queue.get("continuous_pages_required") is not False:
        errors.append("manual queue must explicitly state continuous_pages_required=false")

    accepted_items = []
    structured_exports = []
    task_results = []
    pending_ids = []
    blocked_ids = []
    for task in items:
        task_id = str(task.get("task_id") or "")
        status = task.get("status")
        task_errors = []
        summary = {}
        export_errors, export_summaries = validate_structured_exports(run_dir, task)
        task_errors.extend(export_errors)
        structured_exports.extend(export_summaries)
        if status == "pending":
            pending_ids.append(task_id)
            task_errors.append("task is pending")
        elif status == "blocked":
            blocked_ids.append(task_id)
            task_errors.append("task is blocked and has no accepted result/zero capture")
        elif status not in FINAL_STATUSES:
            task_errors.append(f"unsupported final status: {status!r}")
        else:
            matching = [
                item for item in task.get("captures") or []
                if item.get("accepted") is True and item.get("capture_kind") == status
            ]
            if not matching:
                task_errors.append("accepted final capture is missing")
            else:
                valid, capture_errors, summary = validate_capture(run_dir, task, matching[-1])
                task_errors.extend(capture_errors)
                if valid:
                    summary["structured_export_batch_count"] = len(export_summaries)
                    summary["structured_export_item_count"] = sum(
                        item["item_count"] for item in export_summaries
                    )
                    accepted_items.append(summary)
        task_results.append({
            "task_id": task_id,
            "platform": task.get("platform"),
            "query": task.get("query"),
            "target_good": task.get("target_good"),
            "status": status,
            "ok": not task_errors,
            "errors": task_errors,
            "accepted_capture": summary or None,
            "structured_exports": export_summaries,
        })
        errors.extend(f"{task_id}: {message}" for message in task_errors)

    diagnostic_root = run_dir / "capture-diagnostics" / "manual-capture"
    diagnostic_capture_count = len(list(diagnostic_root.glob("*/*/metadata.json"))) if diagnostic_root.is_dir() else 0
    output = {
        "schema_version": "1.0",
        "record_type": "manual_sales_capture_validation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": queue.get("run_id"),
        "ok": not errors,
        "continuous_pages_required": False,
        "evidence_level": "related_lead_not_formal_use_evidence",
        "metrics": {
            "task_count": len(items),
            "accepted_task_count": len(accepted_items),
            "pending_task_count": len(pending_ids),
            "blocked_task_count": len(blocked_ids),
            "platform_count": len(platforms),
            "minimum_platforms": min_platforms,
            "diagnostic_capture_count": diagnostic_capture_count,
            "structured_export_batch_count": len(structured_exports),
            "structured_export_item_count": sum(item["item_count"] for item in structured_exports),
            "structured_export_rejected_row_count": sum(item["rejected_row_count"] for item in structured_exports),
            "failed_or_blocked_pages_included": 0,
        },
        "platforms": platforms,
        "pending_task_ids": pending_ids,
        "blocked_task_ids": blocked_ids,
        "accepted_items": accepted_items,
        "structured_exports": structured_exports,
        "task_results": task_results,
        "errors": errors,
        "warnings": warnings,
        "limitations": [
            "搜索结果页属于相关线索，不自动等同商标实际使用证据。",
            "未发现仅表示本次人工操作和页面可见范围内未发现。",
            "本模式不要求连续五页。",
            "CSV/XLSX 结构化导出仅用于筛查线索，不会替代页面固证或完成任务。",
        ],
    }
    output_path = run_dir / "manual-capture-validation.json"
    atomic_write_json(output_path, output)
    lines = [
        "# 人工销售平台固证校验",
        "",
        f"- 结果：{'通过' if output['ok'] else '未通过'}",
        f"- 任务：{len(accepted_items)}/{len(items)}",
        f"- 平台：{len(platforms)}/{min_platforms}",
        f"- 待处理：{len(pending_ids)}",
        f"- 验证/阻断：{len(blocked_ids)}",
        f"- 结构化导出：{len(structured_exports)} 批 / {sum(item['item_count'] for item in structured_exports)} 条",
        "- 连续五页：不要求",
        "- 验证/阻断页纳入交付：0",
        "",
    ]
    if errors:
        lines.extend(["## 待处理问题", "", *[f"- {item}" for item in errors], ""])
    (run_dir / "manual-capture-validation.md").write_text("\n".join(lines), encoding="utf-8")
    return output_path, output


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a human-operated sales capture run")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--min-platforms", type=int, default=3)
    args = parser.parse_args()
    try:
        path, output = validate(Path(args.run_dir), args.min_platforms)
        print(json.dumps({
            "manual_capture_validation": True,
            "ok": output["ok"],
            "accepted_task_count": output["metrics"]["accepted_task_count"],
            "task_count": output["metrics"]["task_count"],
            "output": str(path),
        }, ensure_ascii=False, indent=2))
        raise SystemExit(0 if output["ok"] else 4)
    except Exception as exc:
        print(json.dumps({"manual_capture_validation": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
