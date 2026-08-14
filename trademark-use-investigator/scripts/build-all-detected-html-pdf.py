#!/usr/bin/env python3
"""Build a self-contained PDF of every accepted HTML capture in a run.

The PDF body prefers browser-printed PDFs so links inside each rendered web
page remain clickable. Screenshots are only a fallback. Every original HTML
variant is embedded as a document attachment and recorded with SHA-256.
Blocked, login, captcha, failed-submission and diagnostic pages are excluded.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Iterable
from urllib.parse import quote, urlsplit

import fitz
from PIL import Image

from audit_cherrystudio_run import validate_assisted_artifacts
from runtime_policy import (
    DEFAULT_SALES_PAGES_PER_PLATFORM,
    DEFAULT_SEARCH_PAGES_PER_PROVIDER,
    MAX_PAGES_PER_SECTION,
    MIN_PAGES_PER_SECTION,
    PDF_TAIL_ANALYSIS_SCALE,
    PDF_TAIL_MAX_LARGEST_RASTER_AREA_RATIO,
    PDF_TAIL_MAX_NONWHITE_RATIO,
    PDF_TAIL_MAX_TEXT_CHARS,
    PDF_TAIL_NONWHITE_GRAY_THRESHOLD,
)
from partial_materials_contract import (
    PARTIAL_MANIFEST_NAME,
    PARTIAL_MANIFEST_RECORD_TYPE,
    PARTIAL_PDF_PROHIBITED_DISCLOSURES,
    PARTIAL_PDF_NAME,
    PARTIAL_STATUS,
)


ALLOWED_STATES = {"normal", "manual_visual_review", "result", "detail", "zero", "zero_results"}
HTML_NAMES = (
    "serp.html",
    "rendered-dom.html",
    "search.html",
    "response.html",
    "page.singlefile.html",
    "singlefile.html",
    "page.singlefile.raw.html",
)
IMAGE_NAMES = ("serp.png", "fullpage.png", "search.png", "browser-window.png", "visible.png")
PDF_NAMES = ("serp.pdf", "page.pdf")
PUBLIC_SEARCH_PLATFORMS = {"so360", "sogou", "bing", "baidu", "yahoo"}
SALES_PLATFORMS = {"taobao", "tmall", "jd", "1688", "pinduoduo"}
PLATFORM_LABELS = {
    "taobao": "淘宝／天猫",
    "jd": "京东",
    "1688": "1688",
    "baidu": "百度",
    "so360": "360搜索",
    "bing": "Bing搜索",
    "sogou": "搜狗搜索",
    "reference": "商标基准",
    "other": "其他网页",
}
CHINA_STANDARD_TIME = timezone(timedelta(hours=8), name="Asia/Shanghai")

# Browser print output can contain a long run of almost-empty trailing sheets
# caused by fixed-position widgets or an oversized print layout.  Keep these
# thresholds deliberately conservative: a page is low-information only when
# all three independent signals agree.  A large raster object protects
# screenshot-only / image-only evidence even when its pixels are mostly white.
LOW_INFORMATION_TEXT_CHAR_LIMIT = PDF_TAIL_MAX_TEXT_CHARS
LOW_INFORMATION_NONWHITE_RATIO_LIMIT = PDF_TAIL_MAX_NONWHITE_RATIO
MEANINGFUL_RASTER_AREA_RATIO = PDF_TAIL_MAX_LARGEST_RASTER_AREA_RATIO
NONWHITE_GRAY_THRESHOLD = PDF_TAIL_NONWHITE_GRAY_THRESHOLD
PAGE_ANALYSIS_SCALE = PDF_TAIL_ANALYSIS_SCALE
TRAILING_PAGE_TRIM_REASON = "continuous_low_information_pdf_tail"


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def platform_from_url(url: str | None, fallback: str | None = None) -> str:
    raw = str(fallback or "").strip().lower()
    aliases = {"tmall": "taobao", "taobao-tmall": "taobao", "jingdong": "jd"}
    if raw:
        return aliases.get(raw, raw)
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        host = ""
    if host == "1688.com" or host.endswith(".1688.com"):
        return "1688"
    if host == "jd.com" or host.endswith(".jd.com"):
        return "jd"
    if any(host == domain or host.endswith(f".{domain}") for domain in ("taobao.com", "tmall.com")):
        return "taobao"
    if host == "baidu.com" or host.endswith(".baidu.com"):
        return "baidu"
    if host == "so.com" or host.endswith(".so.com") or host == "360.cn" or host.endswith(".360.cn"):
        return "so360"
    if host == "bing.com" or host.endswith(".bing.com"):
        return "bing"
    if host == "sogou.com" or host.endswith(".sogou.com"):
        return "sogou"
    return "other"


def display_platform(key: str, label: str | None = None) -> str:
    return str(label or PLATFORM_LABELS.get(key) or key)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def production_matrix_coverage(run_dir: Path, config: dict) -> dict:
    orchestration = config.get("cherrystudio_orchestration") or {}
    if orchestration.get("workflow_mode") != "free_assisted_browser":
        return {"required": False, "ok": True}
    public_plan = read_json(run_dir / "discovery" / "public-search-plan.json", {}) or {}
    public_matrix = read_json(run_dir / "discovery" / "public-search-matrix.json", {}) or {}
    public_expected = int(public_plan.get("task_count") or len(public_plan.get("items") or []) or 0)
    public_runs = [value for value in public_matrix.get("provider_runs") or [] if isinstance(value, dict)]
    public_by_id = {
        str(value.get("query_id") or ""): value for value in public_runs if value.get("query_id")
    }
    public_incomplete_tasks = []
    for item in public_plan.get("items") or []:
        query_id = str(item.get("query_id") or "")
        observed = public_by_id.get(query_id)
        state = str((observed or {}).get("state") or "missing")
        if state not in {"normal", "zero_results"}:
            public_incomplete_tasks.append({
                "query_id": query_id,
                "provider": item.get("provider"),
                "query": item.get("query"),
                "target_good": item.get("target_good"),
                "state": state,
                "kind": "missing" if observed is None else "blocked_or_incomplete",
            })
    public_complete = sum(1 for value in public_runs if value.get("state") in {"normal", "zero_results"})
    sales_plan = read_json(run_dir / "discovery" / "manual-capture-queue.json", {}) or {}
    sales_report = read_json(run_dir / "discovery" / "assisted-sales-results.json", {}) or {}
    sales_expected = int(sales_plan.get("task_count") or len(sales_plan.get("items") or []) or 0)
    sales_runs = [value for value in sales_report.get("platform_runs") or [] if isinstance(value, dict)]
    sales_by_id = {
        str(value.get("query_id") or ""): value for value in sales_runs if value.get("query_id")
    }
    sales_incomplete_tasks = []
    for item in sales_plan.get("items") or []:
        query_id = str(item.get("task_id") or item.get("query_id") or "")
        observed = sales_by_id.get(query_id)
        if (observed or {}).get("delivery_eligible") is True:
            continue
        state = str(
            (observed or {}).get("final_state")
            or (observed or {}).get("initial_state")
            or "missing"
        )
        sales_incomplete_tasks.append({
            "query_id": query_id,
            "platform": item.get("platform") or item.get("target_platform"),
            "query": item.get("query") or item.get("platform_search_query"),
            "target_good": item.get("target_good"),
            "state": state,
            "kind": "missing" if observed is None else "blocked_or_incomplete",
        })
    sales_complete = sum(1 for value in sales_runs if value.get("delivery_eligible") is True)
    sales_validation_errors, sales_validation_stats, _accepted = validate_assisted_artifacts(
        run_dir, sales_plan, sales_report,
    )
    sales_artifacts_complete = bool(
        not sales_validation_errors
        and int(sales_validation_stats.get("validated_runs") or 0) == sales_expected
    )
    capture_summary = read_json(
        run_dir / "capture" / "sales-after-login" / "capture-summary.json", {}
    ) or {}
    capture_complete = bool(
        capture_summary.get("status") == "complete"
        and capture_summary.get("visual_match_complete") is True
    )
    return {
        "required": True,
        "ok": bool(
            public_expected and sales_expected
            and public_complete == public_expected and sales_complete == sales_expected
            and sales_artifacts_complete
            and capture_complete
        ),
        "public_expected": public_expected,
        "public_complete": public_complete,
        "sales_expected": sales_expected,
        "sales_complete": sales_complete,
        "sales_artifacts_complete": sales_artifacts_complete,
        "sales_artifact_validation_errors": sales_validation_errors,
        "capture_complete": capture_complete,
        "capture_status": capture_summary.get("status") or "missing",
        "public_incomplete_tasks": public_incomplete_tasks,
        "sales_incomplete_tasks": sales_incomplete_tasks,
        "incomplete_task_count": len(public_incomplete_tasks) + len(sales_incomplete_tasks),
    }


def quarantine_provisional_output(run_dir: Path, path: Path) -> str | None:
    if not path.is_file() or not is_within(path, run_dir):
        return None
    destination = (
        run_dir / "capture-diagnostics" / "provisional-deliverables"
        / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{path.name}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(destination))
    return destination.relative_to(run_dir).as_posix()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_run_path(run_dir: Path, value: str | Path | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = run_dir / path
    path = path.resolve()
    return path if is_within(path, run_dir) else None


def relative(path: Path, run_dir: Path) -> str:
    return path.resolve().relative_to(run_dir.resolve()).as_posix()


def html_files(source_dir: Path) -> list[Path]:
    ordered: list[Path] = []
    seen: set[Path] = set()
    for name in HTML_NAMES:
        path = source_dir / name
        if path.is_file():
            ordered.append(path.resolve())
            seen.add(path.resolve())
    for path in sorted([*source_dir.glob("*.html"), *source_dir.glob("*.htm")]):
        resolved = path.resolve()
        if resolved not in seen:
            ordered.append(resolved)
            seen.add(resolved)
    return ordered


def artifact_path(run_dir: Path, source_dir: Path, metadata: dict, keys: Iterable[str]) -> Path | None:
    artifacts = metadata.get("artifacts") if isinstance(metadata, dict) else {}
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    for key in keys:
        value = artifacts.get(key)
        if isinstance(value, dict):
            value = value.get("path")
        if not value:
            continue
        candidate = Path(str(value))
        if not candidate.is_absolute():
            direct = source_dir / candidate
            candidate = direct if direct.is_file() else run_dir / candidate
        candidate = candidate.resolve()
        if candidate.is_file() and is_within(candidate, run_dir):
            return candidate
    return None


def visual_file(run_dir: Path, source_dir: Path, metadata: dict) -> Path | None:
    path = artifact_path(run_dir, source_dir, metadata, ("pdf",))
    if path:
        return path
    for name in PDF_NAMES:
        path = source_dir / name
        if path.is_file():
            return path.resolve()
    path = artifact_path(run_dir, source_dir, metadata, ("fullpage", "search", "screenshot", "browser_window"))
    if path:
        return path
    for name in IMAGE_NAMES:
        path = source_dir / name
        if path.is_file():
            return path.resolve()
    return None


def page_state(metadata: dict) -> str:
    return str(metadata.get("page_state") or metadata.get("state") or "unknown")


def content_is_accepted(metadata: dict, delivery_eligible=None) -> bool:
    state = page_state(metadata)
    if state not in ALLOWED_STATES:
        return False
    if metadata.get("content_valid") is False:
        return False
    if delivery_eligible is False:
        return False
    return True


def add_source(
    sources: list[dict], seen_dirs: set[Path], excluded: list[dict], run_dir: Path,
    *, source_id: str, section: str, label: str, source_dir: Path,
    metadata: dict, state: str, url: str | None = None, delivery_eligible=None,
    platform: str | None = None, platform_label: str | None = None,
) -> None:
    source_dir = source_dir.resolve()
    if source_dir in seen_dirs:
        return
    if not is_within(source_dir, run_dir):
        excluded.append({"path": str(source_dir), "reason": "source_outside_run_dir"})
        return
    metadata = dict(metadata or {})
    metadata.setdefault("state", state)
    if not content_is_accepted(metadata, delivery_eligible):
        excluded.append({
            "path": relative(source_dir, run_dir),
            "source_id": source_id,
            "reason": f"ineligible_state:{page_state(metadata)}",
        })
        return
    html = html_files(source_dir)
    visual = visual_file(run_dir, source_dir, metadata)
    if not html:
        excluded.append({"path": relative(source_dir, run_dir), "source_id": source_id, "reason": "html_missing"})
        return
    if not visual:
        excluded.append({"path": relative(source_dir, run_dir), "source_id": source_id, "reason": "visual_missing"})
        return
    final_url = str(
        url or metadata.get("display_url") or metadata.get("final_url")
        or metadata.get("url") or metadata.get("requested_url") or ""
    )
    platform_key = platform_from_url(final_url, platform or metadata.get("platform"))
    seen_dirs.add(source_dir)
    sources.append({
        "id": source_id,
        "section": section,
        "label": label,
        "status": page_state(metadata),
        "source_dir": source_dir,
        "html": html,
        "visual": visual,
        "url": final_url,
        "captured_at": str(metadata.get("captured_at") or iso_mtime(visual)),
        "platform": platform_key,
        "platform_label": display_platform(platform_key, platform_label or metadata.get("platform_label")),
    })


def collect_reference(run_dir: Path, sources: list[dict], seen_dirs: set[Path], excluded: list[dict]) -> None:
    reference = read_json(run_dir / "reference" / "qcc-reference.json", {}) or {}
    html = resolve_run_path(run_dir, reference.get("html_file"))
    image = resolve_run_path(run_dir, reference.get("image_file"))
    if not html:
        fallback = run_dir / "reference" / "qcc-brand-detail.html"
        html = fallback.resolve() if fallback.is_file() else None
    if not image:
        matches = sorted((run_dir / "reference").glob("qcc-trademark-*")) if (run_dir / "reference").is_dir() else []
        image = matches[0].resolve() if matches else None
    if not html or not image:
        return
    source_dir = html.parent.resolve()
    if source_dir in seen_dirs:
        return
    config = read_json(run_dir / "run-config.json", {}) or {}
    trademark = config.get("trademark") or {}
    registration = reference.get("registration_number") or trademark.get("registration_number") or ""
    name = reference.get("name") or trademark.get("name") or ""
    seen_dirs.add(source_dir)
    sources.append({
        "id": "QCC",
        "section": "企查商标基准",
        "label": f"企查：第{registration}号“{name}”商标详情" if registration else f"企查：“{name}”商标详情",
        "status": "archived_reference",
        "source_dir": source_dir,
        "html": [html],
        "visual": image,
        "url": str(reference.get("source_url") or ""),
        "captured_at": str(reference.get("captured_at") or iso_mtime(image)),
        "platform": "reference",
        "platform_label": PLATFORM_LABELS["reference"],
    })


def collect_assisted_searches(run_dir: Path, sources: list[dict], seen_dirs: set[Path], excluded: list[dict]) -> None:
    report = read_json(run_dir / "discovery" / "assisted-sales-results.json", {}) or {}
    found_manifest_entries = False
    for platform_run in report.get("platform_runs") or []:
        if not isinstance(platform_run, dict):
            continue
        platform_key = str(platform_run.get("platform") or "other").lower()
        platform = str(platform_run.get("platform_label") or display_platform(platform_key))
        query_id = str(platform_run.get("query_id") or "QUERY")
        query = str(platform_run.get("search_query") or platform_run.get("query") or "")
        page_runs = [item for item in platform_run.get("page_runs") or [] if isinstance(item, dict)]
        if page_runs:
            for page_run in page_runs:
                found_manifest_entries = True
                page_index = int(page_run.get("page_index") or 1)
                artifact_dir = resolve_run_path(run_dir, page_run.get("artifact_dir"))
                if not artifact_dir:
                    artifacts = page_run.get("artifacts") or {}
                    candidate = ((artifacts.get("rendered_dom") or {}).get("path") if isinstance(artifacts, dict) else None)
                    artifact = resolve_run_path(run_dir, candidate)
                    artifact_dir = artifact.parent if artifact else None
                if not artifact_dir:
                    excluded.append({"source_id": query_id, "reason": "artifact_dir_missing"})
                    continue
                metadata = read_json(artifact_dir / "metadata.json", {}) or dict(page_run)
                metadata.update({key: value for key, value in page_run.items() if key not in {"artifacts"}})
                if page_run.get("artifacts"):
                    metadata["artifacts"] = page_run["artifacts"]
                add_source(
                    sources, seen_dirs, excluded, run_dir,
                    source_id=f"{query_id}-P{page_index:02d}", section="平台搜索页",
                    label=f"{platform}：{query}（第{page_index}页）", source_dir=artifact_dir,
                    metadata=metadata, state=str(page_run.get("state") or "unknown"),
                    url=page_run.get("url"), delivery_eligible=page_run.get("delivery_eligible"),
                    platform=platform_key, platform_label=platform,
                )
        elif platform_run.get("artifact_dir"):
            found_manifest_entries = True
            artifact_dir = resolve_run_path(run_dir, platform_run.get("artifact_dir"))
            if artifact_dir:
                metadata = read_json(artifact_dir / "metadata.json", {}) or dict(platform_run)
                add_source(
                    sources, seen_dirs, excluded, run_dir,
                    source_id=query_id, section="平台搜索页", label=f"{platform}：{query}",
                    source_dir=artifact_dir, metadata=metadata,
                    state=str(platform_run.get("final_state") or "unknown"),
                    url=platform_run.get("final_url"), delivery_eligible=platform_run.get("delivery_eligible"),
                    platform=platform_key, platform_label=platform,
                )
    if found_manifest_entries:
        return
    root = run_dir / "discovery" / "assisted-platforms"
    if not root.is_dir():
        return
    for metadata_path in sorted(root.rglob("metadata.json")):
        metadata = read_json(metadata_path, {}) or {}
        rel_parts = metadata_path.relative_to(root).parts
        platform = rel_parts[0] if rel_parts else "platform"
        query_id = rel_parts[1] if len(rel_parts) > 1 else metadata_path.parent.name
        page_index = int(metadata.get("page_index") or 1)
        add_source(
            sources, seen_dirs, excluded, run_dir,
            source_id=f"{query_id}-P{page_index:02d}", section="平台搜索页",
            label=f"{platform}：{metadata.get('title') or query_id}", source_dir=metadata_path.parent,
            metadata=metadata, state=page_state(metadata), url=metadata.get("url"),
            delivery_eligible=metadata.get("delivery_eligible"),
            platform=platform, platform_label=display_platform(platform),
        )


def collect_public_searches(run_dir: Path, sources: list[dict], seen_dirs: set[Path], excluded: list[dict]) -> None:
    root = run_dir / "discovery" / "providers"
    if not root.is_dir():
        return
    for results_path in sorted(root.glob("*/results.json")):
        report = read_json(results_path, {}) or {}
        if report.get("record_type") != "discovery_provider_run":
            continue
        provider = str(report.get("provider") or "other").lower()
        query_id = str(report.get("query_id") or results_path.parent.name)
        query = str(report.get("query") or "")
        count = int(report.get("result_count") or 0)
        add_source(
            sources, seen_dirs, excluded, run_dir,
            source_id=query_id, section="公开搜索页",
            label=f"{display_platform(provider)}：{query}（{count}条相关结果）",
            source_dir=results_path.parent, metadata={
                **report,
                "state": report.get("state"),
                "captured_at": report.get("captured_at"),
            },
            state=str(report.get("state") or "unknown"),
            url=report.get("final_url") or report.get("search_url"),
            delivery_eligible=str(report.get("state") or "") in {"normal", "zero_results"},
            platform=provider, platform_label=display_platform(provider),
        )


def collect_detail_captures(run_dir: Path, sources: list[dict], seen_dirs: set[Path], excluded: list[dict]) -> None:
    summary_path = run_dir / "capture" / "sales-after-login" / "capture-summary.json"
    summary = read_json(summary_path, {}) or {}
    attempts = [item for item in summary.get("attempts") or [] if isinstance(item, dict)]
    if attempts:
        for attempt in attempts:
            candidate_id = str(attempt.get("candidate_id") or "DETAIL")
            source_dir = resolve_run_path(run_dir, attempt.get("output_dir"))
            if not source_dir:
                source_dir = (summary_path.parent / candidate_id).resolve()
            metadata = read_json(source_dir / "metadata.json", {}) or dict(attempt)
            accepted = (
                int(attempt.get("return_code") or 0) == 0
                and attempt.get("content_valid") is True
                and attempt.get("page_state") in ALLOWED_STATES
            )
            add_source(
                sources, seen_dirs, excluded, run_dir,
                source_id=candidate_id, section="代表性商品详情页",
                label=str(metadata.get("title") or f"商品详情：{candidate_id}"),
                source_dir=source_dir, metadata=metadata,
                state=str(attempt.get("page_state") or page_state(metadata)),
                url=attempt.get("url"), delivery_eligible=accepted,
                platform=attempt.get("platform") or metadata.get("platform"),
                platform_label=attempt.get("platform_label") or metadata.get("platform_label"),
            )
        return
    root = summary_path.parent
    if not root.is_dir():
        return
    for metadata_path in sorted(root.glob("*/metadata.json")):
        metadata = read_json(metadata_path, {}) or {}
        add_source(
            sources, seen_dirs, excluded, run_dir,
            source_id=str(metadata.get("source_id") or metadata_path.parent.name),
            section="代表性商品详情页",
            label=str(metadata.get("title") or f"商品详情：{metadata_path.parent.name}"),
            source_dir=metadata_path.parent, metadata=metadata, state=page_state(metadata),
            url=metadata.get("display_url") or metadata.get("final_url"),
            delivery_eligible=metadata.get("content_valid"),
            platform=metadata.get("platform"), platform_label=metadata.get("platform_label"),
        )


def collect_other_accepted_captures(run_dir: Path, sources: list[dict], seen_dirs: set[Path], excluded: list[dict]) -> None:
    roots = [
        (run_dir / "manual-capture", "人工固证页面"),
        (run_dir / "capture" / "visual-first", "直达网页"),
        (run_dir / "source-pages", "正式来源网页"),
    ]
    for root, section in roots:
        if not root.is_dir():
            continue
        for metadata_path in sorted(root.rglob("metadata.json")):
            metadata = read_json(metadata_path, {}) or {}
            add_source(
                sources, seen_dirs, excluded, run_dir,
                source_id=str(metadata.get("source_id") or metadata_path.parent.name),
                section=section, label=str(metadata.get("title") or metadata.get("label") or metadata_path.parent.name),
                source_dir=metadata_path.parent, metadata=metadata, state=page_state(metadata),
                url=metadata.get("display_url") or metadata.get("final_url") or metadata.get("url"),
                delivery_eligible=metadata.get("content_valid"),
            )


def collect_sources(run_dir: Path) -> tuple[list[dict], list[dict]]:
    sources: list[dict] = []
    excluded: list[dict] = []
    seen_dirs: set[Path] = set()
    collect_reference(run_dir, sources, seen_dirs, excluded)
    collect_public_searches(run_dir, sources, seen_dirs, excluded)
    collect_assisted_searches(run_dir, sources, seen_dirs, excluded)
    collect_detail_captures(run_dir, sources, seen_dirs, excluded)
    collect_other_accepted_captures(run_dir, sources, seen_dirs, excluded)
    return sources, excluded


def add_textbox(page: fitz.Page, rect: fitz.Rect, text: str, sizes=(20, 18, 16), align=fitz.TEXT_ALIGN_CENTER) -> None:
    for size in sizes:
        if page.insert_textbox(rect, text, fontname="china-s", fontsize=size, lineheight=1.3, align=align) >= 0:
            return
    raise RuntimeError(f"Text did not fit: {text[:80]}")


def cover_title(config: dict, *, partial_materials: bool = False) -> tuple[str, list[str]]:
    trademark = config.get("trademark") or {}
    registration = str(trademark.get("registration_number") or "")
    name = str(trademark.get("name") or "")
    prefix = f"第{registration}号“{name}”商标" if registration else f"“{name}”商标"
    lines = [
        f"注册人：{trademark.get('owner') or '未记录'}",
        f"调查期间：{str(trademark.get('period') or '未记录').replace('/', ' 至 ')}",
    ]
    suffix = "全部有效检测HTML汇编"
    return prefix + f"\n{suffix}", lines


def add_cover(
    doc: fitz.Document, config: dict, sources: list[dict], *,
    coverage: dict | None = None, partial_materials: bool = False,
) -> None:
    a4 = fitz.paper_rect("a4")
    page = doc.new_page(width=a4.width, height=a4.height)
    title, lines = cover_title(config, partial_materials=partial_materials)
    add_textbox(page, fitz.Rect(50, 70, a4.width - 50, 170), title, sizes=(23, 21, 18))
    section_counts: dict[str, int] = {}
    for source in sources:
        section_counts[source["section"]] = section_counts.get(source["section"], 0) + 1
    lines.extend(f"{section}：{count}组" for section, count in section_counts.items())
    lines.extend([
        f"有效可视来源：{len(sources)}组",
        "PDF正文：按平台页数预算汇编渲染截图；PDF附件：全量原始HTML文件",
        "蓝色来源网址可点击打开；每页页脚记录存储时间和来源网址",
    ])
    if partial_materials:
        coverage = coverage or {}
        lines.extend([
            (
                f"任务覆盖记录：公开搜索 {int(coverage.get('public_complete') or 0)} / "
                f"{int(coverage.get('public_expected') or 0)}；销售平台 "
                f"{int(coverage.get('sales_complete') or 0)} / {int(coverage.get('sales_expected') or 0)}"
            ),
            f"未入册任务：{int(coverage.get('incomplete_task_count') or 0)}项；仅收入已验收的正常页或明确零结果页",
        ])
    body_rect = fitz.Rect(70, 205 if partial_materials else 220, a4.width - 70, 500 if partial_materials else 485)
    body_text = "\n".join(lines) if partial_materials else "\n\n".join(lines)
    inserted = page.insert_textbox(
        body_rect, body_text, fontname="china-s",
        fontsize=9.2 if partial_materials else 11.5,
        lineheight=1.28 if partial_materials else 1.35,
    )
    if partial_materials and inserted < 0:
        raise RuntimeError("Partial-material cover summary did not fit")
    note = (
        "验证码、登录、拒绝访问、搜索提交失败、动态错页、空壳和诊断页面不进入正文。"
        "本册是调查线索材料；页面命中不自动证明注册人实际使用。"
    )
    page.insert_textbox(
        fitz.Rect(70, 520, a4.width - 70, 650), note,
        fontname="china-s", fontsize=10, lineheight=1.5, color=(0.35, 0.35, 0.35),
    )


def partial_task_coverage(run_dir: Path, sources: list[dict]) -> dict:
    """Reconcile planned tasks with packet-eligible artifacts actually entering the PDF."""
    public_plan = read_json(run_dir / "discovery" / "public-search-plan.json", {}) or {}
    public_matrix = read_json(run_dir / "discovery" / "public-search-matrix.json", {}) or {}
    sales_plan = read_json(run_dir / "discovery" / "manual-capture-queue.json", {}) or {}
    sales_report = read_json(run_dir / "discovery" / "assisted-sales-results.json", {}) or {}
    public_observed = {
        str(item.get("query_id") or ""): item
        for item in public_matrix.get("provider_runs") or [] if isinstance(item, dict)
    }
    sales_observed = {
        str(item.get("query_id") or ""): item
        for item in sales_report.get("platform_runs") or [] if isinstance(item, dict)
    }
    source_ids = {str(source.get("id") or "") for source in sources if source.get("platform") != "reference"}

    completed: list[dict] = []
    missing: list[dict] = []
    blocked: list[dict] = []

    def accepted_ids(query_id: str) -> list[str]:
        return sorted(value for value in source_ids if value == query_id or value.startswith(query_id + "-P"))

    for item in public_plan.get("items") or []:
        query_id = str(item.get("query_id") or "")
        observed = public_observed.get(query_id)
        state = str((observed or {}).get("state") or "missing")
        record = {
            "kind": "public_search", "query_id": query_id,
            "provider": item.get("provider"), "query": item.get("query"),
            "target_good": item.get("target_good"), "state": state,
        }
        packet_sources = accepted_ids(query_id)
        if packet_sources:
            completed.append({**record, "packet_source_ids": packet_sources})
        elif observed is None:
            missing.append({**record, "reason": "task_not_observed"})
        else:
            reason = (
                "accepted_record_without_packet_eligible_artifacts"
                if state in {"normal", "zero_results"}
                else "blocked_or_incomplete"
            )
            blocked.append({**record, "reason": reason})

    for item in sales_plan.get("items") or []:
        query_id = str(item.get("task_id") or item.get("query_id") or "")
        observed = sales_observed.get(query_id)
        state = str(
            (observed or {}).get("final_state")
            or (observed or {}).get("initial_state")
            or "missing"
        )
        record = {
            "kind": "sales_platform", "query_id": query_id,
            "platform": item.get("platform") or item.get("target_platform"),
            "query": item.get("query") or item.get("platform_search_query"),
            "target_good": item.get("target_good"), "state": state,
        }
        packet_sources = accepted_ids(query_id)
        if packet_sources:
            completed.append({**record, "packet_source_ids": packet_sources})
        elif observed is None:
            missing.append({**record, "reason": "task_not_observed"})
        else:
            reason = (
                "accepted_record_without_packet_eligible_artifacts"
                if (observed or {}).get("delivery_eligible") is True
                else "blocked_or_incomplete"
            )
            blocked.append({**record, "reason": reason})

    return {
        "planned_task_count": len(completed) + len(missing) + len(blocked),
        "completed_task_count": len(completed),
        "missing_task_count": len(missing),
        "blocked_task_count": len(blocked),
        "incomplete_task_count": len(missing) + len(blocked),
        "completed_tasks": completed,
        "missing_tasks": missing,
        "blocked_tasks": blocked,
        "packet_source_ids": sorted(source_ids),
    }


def add_partial_coverage_pages(doc: fitz.Document, task_coverage: dict) -> list[int]:
    """Add explicit, paginated coverage gaps to a customer-requested partial packet."""
    incomplete = [
        *task_coverage.get("missing_tasks", []),
        *task_coverage.get("blocked_tasks", []),
    ]
    lines = []
    for item in incomplete:
        channel = item.get("provider") or item.get("platform") or "未知渠道"
        query = re.sub(r"\s+", " ", str(item.get("query") or "")).strip()
        query = query if len(query) <= 76 else query[:73] + "..."
        lines.append(
            f"[{item.get('query_id') or '—'}] {channel}｜{item.get('state') or 'missing'}｜"
            f"{item.get('reason') or 'incomplete'}｜{query}"
        )
    if not lines:
        lines = ["任务矩阵中存在未入册项目，但未能解析具体项目；请以随附 manifest 的 coverage 字段为准。"]
    per_page = 16
    page_indexes: list[int] = []
    a4 = fitz.paper_rect("a4")
    for offset in range(0, len(lines), per_page):
        page_index = doc.page_count
        page_indexes.append(page_index)
        page = doc.new_page(width=a4.width, height=a4.height)
        page.insert_text(
            (48, 55), "任务覆盖记录与未入册项目",
            fontname="china-s", fontsize=18, color=(0.12, 0.24, 0.42),
        )
        summary = (
            f"计划任务 {task_coverage.get('planned_task_count', 0)} 项；已进入本材料包 "
            f"{task_coverage.get('completed_task_count', 0)} 项；缺失 "
            f"{task_coverage.get('missing_task_count', 0)} 项；阻断或材料不合格 "
            f"{task_coverage.get('blocked_task_count', 0)} 项。\n"
            "下列任务未进入本册正文；验证码、登录、访问受限、空白、失败或缺少HTML/可视材料的页面均被排除。"
        )
        if page.insert_textbox(
            fitz.Rect(48, 78, a4.width - 48, 145), summary,
            fontname="china-s", fontsize=9.3, lineheight=1.35, color=(0.22, 0.22, 0.22),
        ) < 0:
            raise RuntimeError("Partial-material coverage summary did not fit")
        if page.insert_textbox(
            fitz.Rect(48, 155, a4.width - 48, a4.height - 58),
            "\n\n".join(lines[offset:offset + per_page]),
            fontname="china-s", fontsize=7.5, lineheight=1.3,
        ) < 0:
            raise RuntimeError("Partial-material coverage task list did not fit")
    return page_indexes


def add_index_pages(doc: fitz.Document, source_count: int) -> list[int]:
    a4 = fitz.paper_rect("a4")
    count = max(1, math.ceil(source_count / 15))
    indexes = []
    for _ in range(count):
        indexes.append(doc.page_count)
        doc.new_page(width=a4.width, height=a4.height)
    return indexes


def attachment_records(run_dir: Path, source: dict, source_index: int, known_paths: set[Path]) -> list[dict]:
    records = []
    for html_index, html_path in enumerate(source["html"], start=1):
        html_path = html_path.resolve()
        if html_path in known_paths:
            continue
        known_paths.add(html_path)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(source["id"]))[:48] or "SOURCE"
        records.append({
            "path": relative(html_path, run_dir),
            "size_bytes": html_path.stat().st_size,
            "sha256": sha256(html_path),
            "attachment_name": f"H{source_index:03d}-{safe_id}-{html_index}-{html_path.name}",
            "source_path": html_path,
        })
    return records


def add_divider(doc: fitz.Document, source: dict, records: list[dict]) -> int:
    a4 = fitz.paper_rect("a4")
    page_index = doc.page_count
    page = doc.new_page(width=a4.width, height=a4.height)
    page.insert_text((52, 58), source["section"], fontname="china-s", fontsize=12, color=(0.35, 0.42, 0.52))
    add_textbox(page, fitz.Rect(52, 90, a4.width - 52, 180), source["label"])
    details = [
        f"来源编号：{source['id']}",
        f"页面状态：{source['status']}",
        f"来源URL：{source['url'] or '未记录'}",
        "",
        "点击目录来源标题可跳转到本PDF对应网页页。",
        f"原始HTML仅作后台哈希核验附件（{len(records)}个），不作为主要点击入口。",
    ]
    page.insert_textbox(
        fitz.Rect(52, 210, a4.width - 52, a4.height - 80), "\n".join(details),
        fontname="china-s", fontsize=8.4, lineheight=1.42,
    )
    if source["url"]:
        page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(52, 145, a4.width - 52, 198), "uri": source["url"]})
    return page_index


def image_page_count(source: dict, max_width: int) -> int:
    with Image.open(source["visual"]) as image:
        width = min(image.width, max_width)
        height = max(1, round(image.height * width / image.width))
    a4 = fitz.paper_rect("a4")
    content_w = a4.width - 56
    content_h = a4.height - 28 - 52
    slice_h = max(1, int(width * content_h / content_w))
    return max(1, math.ceil(height / slice_h))


def normalized_page_text_char_count(page: fitz.Page) -> int:
    return len(re.sub(r"\s+", "", page.get_text("text") or ""))


def rendered_nonwhite_ratio(page: fitz.Page, clip: fitz.Rect | None = None) -> float:
    """Measure visible ink without trusting the PDF object structure."""
    area = (fitz.Rect(clip) & page.rect) if clip is not None else page.rect
    if area.is_empty or area.is_infinite:
        return 0.0
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(PAGE_ANALYSIS_SCALE, PAGE_ANALYSIS_SCALE),
        colorspace=fitz.csGRAY,
        alpha=False,
        clip=area,
    )
    samples = pixmap.samples
    if not samples:
        return 0.0
    return sum(value < NONWHITE_GRAY_THRESHOLD for value in samples) / len(samples)


def largest_raster_area_ratio(page: fitz.Page) -> float:
    page_area = max(1.0, page.rect.width * page.rect.height)
    largest = 0.0
    for image in page.get_image_info(xrefs=True):
        bbox = fitz.Rect(image.get("bbox") or fitz.Rect()) & page.rect
        if bbox.is_empty or bbox.is_infinite:
            continue
        largest = max(largest, bbox.width * bbox.height / page_area)
    return largest


def pdf_page_information(page: fitz.Page) -> dict:
    text_char_count = normalized_page_text_char_count(page)
    nonwhite_ratio = rendered_nonwhite_ratio(page)
    raster_area_ratio = largest_raster_area_ratio(page)
    low_information = bool(
        text_char_count <= LOW_INFORMATION_TEXT_CHAR_LIMIT
        and nonwhite_ratio <= LOW_INFORMATION_NONWHITE_RATIO_LIMIT
        and raster_area_ratio <= MEANINGFUL_RASTER_AREA_RATIO
    )
    return {
        "text_char_count": text_char_count,
        "rendered_nonwhite_ratio": round(nonwhite_ratio, 6),
        "largest_raster_area_ratio": round(raster_area_ratio, 6),
        "low_information": low_information,
    }


def visual_page_plan(source: dict, max_width: int) -> dict:
    """Return a prefix-only rendering plan; internal pages are never removed."""
    visual = source["visual"]
    if visual.suffix.lower() != ".pdf":
        count = image_page_count(source, max_width)
        return {
            "source_type": "screenshot_fallback",
            "original_page_count": count,
            "effective_page_count": count,
            "trimmed_source_page_indexes": [],
            "trimmed_pages": [],
            "page_metrics": [],
        }

    with fitz.open(visual) as pdf:
        if not pdf.is_pdf or pdf.needs_pass or pdf.page_count < 1:
            raise ValueError(f"Invalid source PDF: {visual}")
        metrics = [pdf_page_information(page) for page in pdf]
        original_count = pdf.page_count

    # Only peel a contiguous low-information suffix.  Stop at the first
    # meaningful page, and always retain source page 1.
    effective_count = original_count
    while effective_count > 1 and metrics[effective_count - 1]["low_information"]:
        effective_count -= 1
    trimmed_pages = []
    for zero_index in range(effective_count, original_count):
        trimmed_pages.append({
            "source_page_index": zero_index + 1,
            "reason": TRAILING_PAGE_TRIM_REASON,
            **{key: value for key, value in metrics[zero_index].items() if key != "low_information"},
        })
    return {
        "source_type": "native_pdf",
        "original_page_count": original_count,
        "effective_page_count": effective_count,
        "trimmed_source_page_indexes": [item["source_page_index"] for item in trimmed_pages],
        "trimmed_pages": trimmed_pages,
        "page_metrics": metrics,
    }


def visual_page_count(source: dict, max_width: int) -> int:
    return int(visual_page_plan(source, max_width)["effective_page_count"])


def page_plan_manifest_fields(plan: dict, included: int) -> dict:
    original = int(plan["original_page_count"])
    effective = int(plan["effective_page_count"])
    trimmed = list(plan.get("trimmed_source_page_indexes") or [])
    return {
        # Keep the established field as the immutable source-PDF page count.
        "rendered_page_count": original,
        "original_rendered_page_count": original,
        "effective_rendered_page_count": effective,
        "visual_pages_included": included,
        "visual_pages_omitted": original - included,
        "visual_pages_trimmed_low_information": original - effective,
        "visual_pages_omitted_by_budget": max(0, effective - included),
        "trimmed_source_page_index_base": 1,
        "trimmed_source_page_indexes": trimmed,
        "trimmed_pages": list(plan.get("trimmed_pages") or []),
        "trimming_reason": TRAILING_PAGE_TRIM_REASON if trimmed else None,
    }


def add_image_pages(
    doc: fitz.Document, source: dict, max_width: int, quality: int, limit: int | None = None,
) -> tuple[int, int, list[int]]:
    image = Image.open(source["visual"]).convert("RGB")
    if image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.Resampling.LANCZOS)
    a4 = fitz.paper_rect("a4")
    margin_x, header_h, footer_h = 28, 28, 52
    content_w = a4.width - margin_x * 2
    content_h = a4.height - header_h - footer_h
    slice_h = max(1, int(image.width * content_h / content_w))
    full_count = max(1, math.ceil(image.height / slice_h))
    allowed = full_count if limit is None else max(0, min(full_count, limit))
    page_indexes: list[int] = []
    for top in range(0, image.height, slice_h):
        if len(page_indexes) >= allowed:
            break
        crop = image.crop((0, top, image.width, min(image.height, top + slice_h)))
        buffer = io.BytesIO()
        crop.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
        page = doc.new_page(width=a4.width, height=a4.height)
        page_indexes.append(page.number)
        page.insert_text(
            (margin_x, 18), f"{source['id']}  {source['label']}",
            fontname="china-s", fontsize=7.5, color=(0.32, 0.36, 0.44),
        )
        rendered_h = crop.height * content_w / crop.width
        page.insert_image(
            fitz.Rect(margin_x, header_h, a4.width - margin_x, header_h + rendered_h),
            stream=buffer.getvalue(),
        )
    image.close()
    return full_count, len(page_indexes), page_indexes


def add_pdf_pages(
    doc: fitz.Document, source: dict, max_width: int, quality: int, limit: int | None = None,
    page_plan: dict | None = None,
) -> tuple[int, int, list[int]]:
    pdf = fitz.open(source["visual"])
    try:
        if not pdf.is_pdf or pdf.needs_pass or pdf.page_count < 1:
            raise ValueError(f"Invalid source PDF: {source['visual']}")
        plan = page_plan or visual_page_plan(source, max_width)
        if int(plan["original_page_count"]) != pdf.page_count:
            raise RuntimeError(f"Source PDF page count changed while rendering: {source['id']}")
        full_count = int(plan["effective_page_count"])
        allowed = full_count if limit is None else max(0, min(full_count, limit))
        page_indexes: list[int] = []
        for source_page in range(allowed):
            original = pdf[source_page]
            rect = original.rect
            scale = min(3.0, max(1.0, max_width / max(1.0, rect.width)))
            pixmap = original.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image_bytes = pixmap.tobytes("jpeg", jpg_quality=min(80, quality))
            target_page = doc.new_page(width=rect.width, height=rect.height)
            # Browser-printed PDFs usually fill the complete media box. Reserve a
            # real footer strip instead of painting provenance over page content.
            footer_reserve = min(52.0, max(24.0, rect.height * 0.09))
            content_scale = min(1.0, max(0.1, (rect.height - footer_reserve) / rect.height))
            content_width = rect.width * content_scale
            x_offset = (rect.width - content_width) / 2
            content_rect = fitz.Rect(x_offset, 0, x_offset + content_width, rect.height * content_scale)
            target_page.insert_image(content_rect, stream=image_bytes)
            target_index = target_page.number
            page_indexes.append(target_index)
            for link_rect, uri in page_uri_link_records(original):
                mapped = fitz.Rect(
                    x_offset + link_rect.x0 * content_scale,
                    link_rect.y0 * content_scale,
                    x_offset + link_rect.x1 * content_scale,
                    link_rect.y1 * content_scale,
                )
                target_page.insert_link({"kind": fitz.LINK_URI, "from": mapped, "uri": uri})
        return full_count, allowed, page_indexes
    finally:
        pdf.close()


def page_uri_link_records(page: fitz.Page) -> list[tuple[fitz.Rect, str]]:
    records: list[tuple[fitz.Rect, str]] = []
    for link in page.get_links():
        uri = str(link.get("uri") or "").strip()
        if link.get("kind") != fitz.LINK_URI or not re.match(r"^(?:https?|mailto):", uri, re.I):
            continue
        rect = fitz.Rect(link.get("from") or fitz.Rect()) & page.rect
        if rect.is_empty or rect.is_infinite or rect.width < 0.5 or rect.height < 0.5:
            continue
        pdf_safe_uri = quote(uri, safe=":/?&=%#@!$'*,;~+-._")
        records.append((rect, pdf_safe_uri))
    return records


def pdf_uri_links(path: Path, limit: int | None = None) -> set[str]:
    links: set[str] = set()
    pdf = fitz.open(path)
    try:
        allowed = pdf.page_count if limit is None else min(pdf.page_count, max(0, limit))
        for page_index in range(allowed):
            links.update(uri for _, uri in page_uri_link_records(pdf[page_index]))
    finally:
        pdf.close()
    return links


def add_visual_pages(
    doc: fitz.Document, source: dict, max_width: int, quality: int, limit: int | None = None,
    page_plan: dict | None = None,
) -> tuple[int, int, list[int]]:
    if source["visual"].suffix.lower() == ".pdf":
        return add_pdf_pages(doc, source, max_width, quality, limit, page_plan)
    return add_image_pages(doc, source, max_width, quality, limit)


def allocate_visual_pages(full_counts: list[int], available: int) -> list[int]:
    """Allocate a stable fair-share page budget without exceeding available."""
    allocated = [0] * len(full_counts)
    remaining = max(0, available)
    for index, count in enumerate(full_counts):
        if remaining <= 0:
            break
        if count > 0:
            allocated[index] = 1
            remaining -= 1
    while remaining > 0:
        advanced = False
        for index, count in enumerate(full_counts):
            if remaining <= 0:
                break
            if allocated[index] < count:
                allocated[index] += 1
                remaining -= 1
                advanced = True
        if not advanced:
            break
    return allocated


def trailing_low_information_qa(doc: fitz.Document, native_output_records: list[dict]) -> dict:
    """Recheck complete native-PDF bodies after final rendering and footers."""
    residual_sources: list[dict] = []
    checked = 0
    skipped_budget_truncated = 0
    for record in native_output_records:
        plan = record["page_plan"]
        included = int(record["included"])
        effective = int(plan["effective_page_count"])
        if included != effective:
            skipped_budget_truncated += 1
            continue
        if included < 1:
            continue
        checked += 1
        output_indexes = list(record["output_page_indexes"])
        metrics = list(plan.get("page_metrics") or [])
        trailing: list[dict] = []
        for source_zero_index in range(included - 1, 0, -1):
            output_index = output_indexes[source_zero_index]
            page = doc.load_page(output_index)
            footer_reserve = min(52.0, max(24.0, page.rect.height * 0.09))
            body_clip = fitz.Rect(0, 0, page.rect.width, page.rect.height - footer_reserve)
            output_nonwhite_ratio = rendered_nonwhite_ratio(page, body_clip)
            source_metric = metrics[source_zero_index]
            low_information = bool(
                int(source_metric["text_char_count"]) <= LOW_INFORMATION_TEXT_CHAR_LIMIT
                and output_nonwhite_ratio <= LOW_INFORMATION_NONWHITE_RATIO_LIMIT
                and float(source_metric["largest_raster_area_ratio"]) <= MEANINGFUL_RASTER_AREA_RATIO
            )
            if not low_information:
                break
            trailing.append({
                "source_page_index": source_zero_index + 1,
                "output_page_index": output_index + 1,
                "rendered_nonwhite_ratio": round(output_nonwhite_ratio, 6),
            })
        if trailing:
            trailing.reverse()
            residual_sources.append({
                "source_id": record["source_id"],
                "pages": trailing,
                "reason": "residual_continuous_low_information_pdf_tail",
            })
    return {
        "ok": not residual_sources,
        "complete_native_pdf_sources_checked": checked,
        "budget_truncated_native_pdf_sources_skipped": skipped_budget_truncated,
        "residual_source_count": len(residual_sources),
        "residual_sources": residual_sources,
    }


def add_platform_index_pages(doc: fitz.Document, platform_label: str, source_count: int) -> list[int]:
    a4 = fitz.paper_rect("a4")
    page_count = max(1, math.ceil(source_count / 18))
    indexes = []
    for part in range(page_count):
        page = doc.new_page(width=a4.width, height=a4.height)
        page.insert_text((45, 48), f"{platform_label}检测来源", fontname="china-s", fontsize=18)
        page.insert_text(
            (45, 70),
            f"第 {part + 1} / {page_count} 张目录页；点击来源标题跳转到本 PDF 对应网页页",
            fontname="china-s", fontsize=8, color=(0.35, 0.4, 0.48),
        )
        indexes.append(page.number)
    return indexes


def fill_platform_indexes(doc: fitz.Document, index_pages: list[int], items: list[dict]) -> int:
    link_count = 0
    for group, page_index in enumerate(index_pages):
        page = doc[page_index]
        y = 100
        for item in items[group * 18:(group + 1) * 18]:
            label = item["label"] if len(item["label"]) <= 44 else item["label"][:43] + "…"
            count = item["visual_pages_included"]
            effective_count = int(item.get("effective_rendered_page_count") or item["rendered_page_count"])
            note = f"网页PDF {count}/{effective_count} 页"
            if count == 0:
                note = "正文因页数预算省略"
            page.insert_text((45, y), str(item["id"])[:15], fontname="helv", fontsize=7.7)
            page.insert_text((118, y), label, fontname="china-s", fontsize=7.7)
            page.insert_text((445, y), note, fontname="china-s", fontsize=7.2)
            target = max(0, int(item["destination_page"]) - 1)
            page.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(42, y - 10, 438, y + 8), "page": target})
            link_count += 1
            if item["url"]:
                page.insert_text((118, y + 15), "打开原网页", fontname="china-s", fontsize=7.2, color=(0.05, 0.3, 0.75))
                page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(115, y + 5, 185, y + 20), "uri": item["url"]})
                link_count += 1
            page.draw_line((45, y + 27), (550, y + 27), color=(0.86, 0.88, 0.91), width=0.5)
            y += 39
    return link_count


def fill_indexes(doc: fitz.Document, index_pages: list[int], ranges: list[dict]) -> int:
    link_count = 0
    for group, page_index in enumerate(index_pages):
        page = doc[page_index]
        page.insert_text((45, 48), "检测HTML目录", fontname="china-s", fontsize=18)
        page.insert_text((45, 73), "编号", fontname="china-s", fontsize=9)
        page.insert_text((110, 73), "来源页面", fontname="china-s", fontsize=9)
        page.insert_text((500, 73), "PDF页码", fontname="china-s", fontsize=9)
        y = 98
        for item in ranges[group * 15:(group + 1) * 15]:
            label = item["label"] if len(item["label"]) <= 42 else item["label"][:41] + "…"
            pages = str(item["start_page"]) if item["start_page"] == item["end_page"] else f"{item['start_page']}-{item['end_page']}"
            page.insert_text((45, y), str(item["id"])[:14], fontname="helv", fontsize=8)
            page.insert_text((110, y), label, fontname="china-s", fontsize=8)
            page.insert_text((500, y), pages, fontname="helv", fontsize=8)
            page.insert_link({
                "kind": fitz.LINK_GOTO,
                "from": fitz.Rect(42, y - 10, 550, y + 10),
                "page": max(0, int(item["destination_page"]) - 1),
            })
            link_count += 1
            page.draw_line((45, y + 9), (550, y + 9), color=(0.86, 0.88, 0.91), width=0.5)
            y += 42
    return link_count


def footer_display_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return "—（本页为目录或汇编说明）"
    return value if len(value) <= 88 else value[:85] + "…"


def footer_display_time(value: str, fallback: str) -> str:
    raw = str(value or fallback or "").strip()
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(CHINA_STANDARD_TIME).isoformat(timespec="seconds")
    except ValueError:
        return raw


def platform_page_budget(args, platform_key: str) -> int:
    if platform_key in PUBLIC_SEARCH_PLATFORMS:
        return int(args.max_search_pages_per_provider)
    if platform_key in SALES_PLATFORMS:
        return int(args.max_sales_pages_per_platform)
    return int(args.max_sales_pages_per_platform)


def add_page_footers(doc: fitz.Document, page_context: dict[int, dict], generated_at: str) -> tuple[int, int]:
    timestamp_pages = 0
    source_url_pages = 0
    for page_index, page in enumerate(doc):
        context = page_context.get(page_index) or {}
        captured_at = footer_display_time(str(context.get("captured_at") or ""), generated_at)
        url = str(context.get("url") or "")
        page.draw_line(
            (28, page.rect.height - 44), (page.rect.width - 28, page.rect.height - 44),
            color=(0.82, 0.84, 0.88), width=0.45, overlay=True,
        )
        # Keep CJK labels in the built-in Chinese font, but render ASCII values
        # with Helvetica so timestamps and URLs remain compact and legible.
        page.insert_text(
            (30, page.rect.height - 31), "存储时间：", fontname="china-s", fontsize=6.4,
            color=(0.35, 0.35, 0.35), overlay=True,
        )
        page.insert_text(
            (68, page.rect.height - 31), captured_at, fontname="helv", fontsize=6.4,
            color=(0.35, 0.35, 0.35), overlay=True,
        )
        page.insert_textbox(
            fitz.Rect(page.rect.width - 120, page.rect.height - 41, page.rect.width - 30, page.rect.height - 27),
            f"第 {page_index + 1} / {doc.page_count} 页", fontname="china-s", fontsize=6.4,
            color=(0.35, 0.35, 0.35), align=fitz.TEXT_ALIGN_RIGHT, overlay=True,
        )
        url_rect = fitz.Rect(30, page.rect.height - 25, page.rect.width - 30, page.rect.height - 9)
        footer_color = (0.05, 0.28, 0.7) if url else (0.4, 0.4, 0.4)
        page.insert_text(
            (30, page.rect.height - 13), "来源网址：", fontname="china-s", fontsize=6.2,
            color=footer_color, overlay=True,
        )
        if url:
            page.insert_text(
                (68, page.rect.height - 13), footer_display_url(url),
                fontname="helv", fontsize=6.2, color=footer_color, overlay=True,
            )
        else:
            page.insert_text(
                (68, page.rect.height - 13), "—（本页为目录或汇编说明）",
                fontname="china-s", fontsize=6.2, color=footer_color, overlay=True,
            )
        timestamp_pages += 1
        if url:
            page.insert_link({"kind": fitz.LINK_URI, "from": url_rect, "uri": url})
            source_url_pages += 1
    return timestamp_pages, source_url_pages


QA_PREVIEW_MARK = "流程验收预览｜矩阵不完整｜不可作为调查结论"


def add_incomplete_marks(doc: fitz.Document, mark_text: str) -> int:
    """Mark every page of an incomplete build so it cannot resemble a final deliverable."""
    marked = 0
    for page in doc:
        banner = fitz.Rect(24, 1, max(25, page.rect.width - 24), 14)
        page.draw_rect(
            banner,
            color=(0.72, 0.05, 0.05),
            fill=(1.0, 0.93, 0.93),
            width=0.8,
            fill_opacity=0.82,
            overlay=True,
        )
        inserted = page.insert_textbox(
            banner,
            mark_text,
            fontname="china-s",
            fontsize=6.4,
            color=(0.68, 0.02, 0.02),
            align=fitz.TEXT_ALIGN_CENTER,
            overlay=True,
        )
        if inserted >= 0:
            marked += 1
    return marked


def add_qa_preview_marks(doc: fitz.Document) -> int:
    return add_incomplete_marks(doc, QA_PREVIEW_MARK)


def build(args) -> dict:
    Image.MAX_IMAGE_PIXELS = None
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / "run-config.json").is_file():
        raise FileNotFoundError(f"run-config.json is missing: {run_dir}")
    config = read_json(run_dir / "run-config.json")
    if not isinstance(config, dict):
        raise ValueError("run-config.json is invalid")
    qa_preview = bool(getattr(args, "qa_preview_incomplete", False))
    partial_materials = bool(getattr(args, "partial_customer_request", False))
    if qa_preview and partial_materials:
        raise ValueError("QA preview and customer-requested partial materials are mutually exclusive")
    if partial_materials:
        canonical_output = (run_dir / PARTIAL_PDF_NAME).resolve()
        canonical_manifest = (run_dir / PARTIAL_MANIFEST_NAME).resolve()
        output = Path(args.output).resolve() if args.output else canonical_output
        manifest_path = Path(args.manifest).resolve() if args.manifest else canonical_manifest
        if output != canonical_output or manifest_path != canonical_manifest:
            raise ValueError("Partial-material output paths are fixed inside RUN_DIR")
    else:
        output = Path(args.output).resolve() if args.output else run_dir / "all-detected-html-pages.pdf"
        manifest_path = Path(args.manifest).resolve() if args.manifest else run_dir / "all-detected-html-pages.manifest.json"
    if not qa_preview and not partial_materials:
        canonical_output = (run_dir / "all-detected-html-pages.pdf").resolve()
        canonical_manifest = (run_dir / "all-detected-html-pages.manifest.json").resolve()
        if output != canonical_output or manifest_path != canonical_manifest:
            raise ValueError(
                "Production output paths are fixed inside RUN_DIR; use the orchestrator publish command for delivery"
            )
    if qa_preview:
        if not args.output or not args.manifest:
            raise ValueError("--qa-preview-incomplete requires explicit --output and --manifest paths")
        if output.name.casefold() == "all-detected-html-pages.pdf".casefold():
            raise ValueError("QA preview must not use the canonical final PDF filename")
        if manifest_path.name.casefold() == "all-detected-html-pages.manifest.json".casefold():
            raise ValueError("QA preview must not use the canonical final manifest filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    coverage = production_matrix_coverage(run_dir, config)
    matrix_incomplete = bool(coverage.get("required") and not coverage.get("ok"))
    if qa_preview and not matrix_incomplete:
        raise ValueError("--qa-preview-incomplete is only valid when the required production matrix is incomplete")
    if partial_materials and not matrix_incomplete:
        raise ValueError("--partial-customer-request is only valid when the required production matrix is incomplete")
    if matrix_incomplete and not qa_preview and not partial_materials:
        quarantined = quarantine_provisional_output(run_dir, output)
        manifest_path.write_text(json.dumps({
            "schema_version": "1.0",
            "record_type": "all_detected_html_pages_manifest",
            "status": "incomplete_not_deliverable",
            "coverage": coverage,
            "quarantined_provisional_pdf": quarantined,
            "validation": {
                "ok": False, "production_matrix_complete": False,
                "capture_complete": coverage.get("capture_complete") is True,
            },
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise RuntimeError(
            "Production matrix is incomplete; refusing to build or publish a final PDF "
            f"(public {coverage['public_complete']}/{coverage['public_expected']}, "
            f"sales {coverage['sales_complete']}/{coverage['sales_expected']})"
        )
    sources, excluded = collect_sources(run_dir)
    if partial_materials:
        partial_sources = []
        for source in sources:
            if source.get("platform") == "reference" or source.get("status") in {"normal", "zero", "zero_results"}:
                partial_sources.append(source)
            else:
                excluded.append({
                    "source_id": source.get("id"),
                    "path": relative(source["source_dir"], run_dir),
                    "reason": f"partial_packet_requires_normal_or_explicit_zero:{source.get('status')}",
                })
        sources = partial_sources
    if not sources:
        raise ValueError("No accepted HTML capture with a visual rendering is available")
    task_coverage = partial_task_coverage(run_dir, sources) if partial_materials else None
    if partial_materials and int((task_coverage or {}).get("completed_task_count") or 0) < 1:
        raise ValueError("Partial materials require at least one packet-eligible non-reference task")
    if partial_materials:
        coverage = {
            **coverage,
            "packet_completed_task_count": task_coverage.get("completed_task_count"),
            "incomplete_task_count": task_coverage.get("incomplete_task_count"),
        }
    assessment_pdf = (
        None
        if qa_preview or partial_materials or args.no_auto_assessment
        else resolve_run_path(run_dir, args.assessment_pdf or "manual-use-assessment.pdf")
    )
    assessment_html = (
        None
        if qa_preview or partial_materials or args.no_auto_assessment
        else resolve_run_path(run_dir, args.assessment_html or "manual-use-assessment.html")
    )
    if assessment_pdf and assessment_pdf.suffix.lower() != ".pdf":
        raise ValueError("Assessment PDF must be a PDF file")

    generated_at = datetime.now(timezone.utc).isoformat()
    doc = fitz.open()
    add_cover(
        doc, config, sources, coverage=coverage,
        partial_materials=partial_materials,
    )
    coverage_pages = add_partial_coverage_pages(doc, task_coverage or {}) if partial_materials else []
    index_pages = add_index_pages(doc, len(sources))
    toc = [[1, "封面与目录", 1]]
    if coverage_pages:
        toc.append([1, "任务覆盖记录与未入册项目", coverage_pages[0] + 1])
    page_context: dict[int, dict] = {
        page_index: {"captured_at": generated_at, "url": ""}
        for page_index in range(doc.page_count)
    }
    assessment = None
    if assessment_pdf and assessment_pdf.is_file():
        assessment_start = doc.page_count
        assessment = fitz.open(assessment_pdf)
        if not assessment.is_pdf or assessment.needs_pass or assessment.page_count < 1:
            raise ValueError(f"Assessment PDF is invalid: {assessment_pdf}")
        doc.insert_pdf(assessment, links=True, annots=True)
        toc.append([1, "人工复核结论", assessment_start + 1])
        assessment_pages = assessment.page_count
        for page_index in range(assessment_start, assessment_start + assessment_pages):
            page_context[page_index] = {"captured_at": generated_at, "url": ""}
        assessment.close()
    else:
        assessment_pages = 0

    ranges: list[dict] = []
    attachments: list[dict] = []
    known_attachment_paths: set[Path] = set()
    if assessment_html and assessment_html.is_file():
        record = {
            "path": relative(assessment_html, run_dir),
            "size_bytes": assessment_html.stat().st_size,
            "sha256": sha256(assessment_html),
            "attachment_name": "H000-ASSESSMENT-manual-use-assessment.html",
            "source_path": assessment_html,
        }
        attachments.append(record)
        known_attachment_paths.add(assessment_html.resolve())
        doc.embfile_add(
            record["attachment_name"], assessment_html.read_bytes(), filename=assessment_html.name,
            ufilename=assessment_html.name, desc="人工复核报告HTML",
        )

    source_entries: list[tuple[dict, list[dict], int]] = []
    for source_index, source in enumerate(sources, start=1):
        records = attachment_records(run_dir, source, source_index, known_attachment_paths)
        if not records:
            excluded.append({"source_id": source["id"], "reason": "all_html_files_were_duplicates"})
            continue
        for record in records:
            doc.embfile_add(
                record["attachment_name"], record["source_path"].read_bytes(),
                filename=record["source_path"].name,
                ufilename=f"{source['id']}-{record['source_path'].name}",
                desc=f"{source['label']} | {record['path']}",
            )
            attachments.append(record)
        source_entries.append((source, records, source_index))

    grouped: OrderedDict[str, list[tuple[dict, list[dict], int]]] = OrderedDict()
    for entry in source_entries:
        grouped.setdefault(entry[0]["platform"], []).append(entry)

    page_plans = {
        str(source["visual"].resolve()): visual_page_plan(source, args.max_image_width)
        for source, _records, _source_index in source_entries
    }

    platform_summaries: dict[str, dict] = {}
    clickable_link_count = 0
    expected_native_page_urls: set[str] = set()
    native_pdf_source_count = 0
    native_output_records: list[dict] = []
    for platform_key, entries in grouped.items():
        platform_label = entries[0][0]["platform_label"]
        if platform_key == "reference":
            group_start = doc.page_count
            for source, records, _ in entries:
                page_plan = page_plans[str(source["visual"].resolve())]
                divider_index = add_divider(doc, source, records)
                page_context[divider_index] = {"captured_at": source["captured_at"], "url": source["url"]}
                if source["url"]:
                    clickable_link_count += 1
                full_count, included, visual_indexes = add_visual_pages(
                    doc, source, args.max_image_width, args.jpeg_quality, page_plan=page_plan,
                )
                if full_count != int(page_plan["effective_page_count"]):
                    raise RuntimeError(f"Visual page count changed while rendering: {source['id']}")
                if page_plan["source_type"] == "native_pdf":
                    native_output_records.append({
                        "source_id": source["id"], "page_plan": page_plan,
                        "included": included, "output_page_indexes": visual_indexes,
                    })
                for page_index in visual_indexes:
                    page_context[page_index] = {"captured_at": source["captured_at"], "url": source["url"]}
                item = {
                    "id": source["id"], "section": source["section"], "label": source["label"],
                    "platform": platform_key, "platform_label": platform_label,
                    "status": source["status"], "url": source["url"], "captured_at": source["captured_at"],
                    "html": [{k: v for k, v in record.items() if k != "source_path"} for record in records],
                    "render_source": relative(source["visual"], run_dir),
                    "render_source_sha256": sha256(source["visual"]),
                    **page_plan_manifest_fields(page_plan, included),
                    "start_page": divider_index + 1, "end_page": doc.page_count,
                    "destination_page": divider_index + 1,
                }
                ranges.append(item)
                toc.append([1, f"{source['id']} {source['label']}", divider_index + 1])
            platform_summaries[platform_key] = {
                "label": platform_label, "source_count": len(entries), "index_pages": 0,
                "visual_pages": doc.page_count - group_start, "total_pages": doc.page_count - group_start,
                "budget": None, "truncated_visual_pages": 0,
                "trimmed_low_information_pages": sum(
                    int(page_plans[str(source["visual"].resolve())]["original_page_count"])
                    - int(page_plans[str(source["visual"].resolve())]["effective_page_count"])
                    for source, _records, _source_index in entries
                ),
            }
            continue

        budget = platform_page_budget(args, platform_key)
        platform_index_pages = add_platform_index_pages(doc, platform_label, len(entries))
        if len(platform_index_pages) > budget:
            raise ValueError(f"{platform_label} source index alone exceeds the platform page budget")
        for page_index in platform_index_pages:
            page_context[page_index] = {"captured_at": generated_at, "url": ""}
        toc.append([1, f"{platform_label}（最多{budget}页）", platform_index_pages[0] + 1])
        entry_plans = [page_plans[str(source["visual"].resolve())] for source, _, _ in entries]
        full_counts = [int(plan["effective_page_count"]) for plan in entry_plans]
        available = budget - len(platform_index_pages)
        allocations = allocate_visual_pages(full_counts, available)
        platform_items: list[dict] = []
        visual_pages = 0
        for (source, records, _), page_plan, full_count, allocation in zip(
            entries, entry_plans, full_counts, allocations,
        ):
            start_zero = doc.page_count
            checked_full, included, visual_indexes = add_visual_pages(
                doc, source, args.max_image_width, args.jpeg_quality, allocation, page_plan,
            )
            if checked_full != full_count:
                raise RuntimeError(f"Visual page count changed while rendering: {source['id']}")
            render_source_type = "native_pdf" if source["visual"].suffix.lower() == ".pdf" else "screenshot_fallback"
            native_page_links: set[str] = set()
            if render_source_type == "native_pdf" and included:
                native_pdf_source_count += 1
                native_page_links = pdf_uri_links(source["visual"], included)
                expected_native_page_urls.update(native_page_links)
                native_output_records.append({
                    "source_id": source["id"], "page_plan": page_plan,
                    "included": included, "output_page_indexes": visual_indexes,
                })
            visual_pages += included
            for page_index in visual_indexes:
                page_context[page_index] = {"captured_at": source["captured_at"], "url": source["url"]}
            destination = (visual_indexes[0] + 1) if visual_indexes else (platform_index_pages[0] + 1)
            end_page = (visual_indexes[-1] + 1) if visual_indexes else destination
            item = {
                "id": source["id"], "section": source["section"], "label": source["label"],
                "platform": platform_key, "platform_label": platform_label,
                "status": source["status"], "url": source["url"], "captured_at": source["captured_at"],
                "html": [{k: v for k, v in record.items() if k != "source_path"} for record in records],
                "render_source": relative(source["visual"], run_dir),
                "render_source_sha256": sha256(source["visual"]),
                "render_source_type": render_source_type,
                "native_page_link_count": len(native_page_links),
                **page_plan_manifest_fields(page_plan, included),
                "start_page": (start_zero + 1) if included else destination,
                "end_page": end_page, "destination_page": destination,
            }
            platform_items.append(item)
            ranges.append(item)
            toc.append([2, f"{source['id']} {source['label']}", destination])
        clickable_link_count += fill_platform_indexes(doc, platform_index_pages, platform_items)
        total_platform_pages = len(platform_index_pages) + visual_pages
        platform_summaries[platform_key] = {
            "label": platform_label, "source_count": len(entries),
            "index_pages": len(platform_index_pages), "visual_pages": visual_pages,
            "total_pages": total_platform_pages, "budget": budget,
            "truncated_visual_pages": sum(full_counts) - visual_pages,
            "trimmed_low_information_pages": sum(
                int(plan["original_page_count"]) - int(plan["effective_page_count"])
                for plan in entry_plans
            ),
        }

    if not ranges:
        doc.close()
        raise ValueError("All accepted sources were duplicates; no PDF body was produced")
    clickable_link_count += fill_indexes(doc, index_pages, ranges)
    timestamp_footer_pages, source_url_footer_pages = add_page_footers(doc, page_context, generated_at)
    qa_preview_mark_pages = add_qa_preview_marks(doc) if qa_preview else 0
    clickable_link_count += source_url_footer_pages
    total_pages = doc.page_count
    title, _ = cover_title(config, partial_materials=partial_materials)
    doc.set_toc(toc)
    doc.set_metadata({
        "title": title.replace("\n", " "),
        "author": "商标调查人工固证工作台",
        "subject": (
            "正文为已验收HTML渲染页；原始HTML作为PDF附件嵌入"
            if partial_materials
            else "正文为HTML渲染页；原始HTML作为PDF附件嵌入"
        ),
    })
    temporary = output.with_name(output.name + ".tmp.pdf")
    if temporary.exists():
        temporary.unlink()
    doc.save(temporary, garbage=4, deflate=True, use_objstms=1)
    doc.close()

    reopened = fitz.open(temporary)
    names = reopened.embfile_names()
    errors = []
    if reopened.page_count != total_pages:
        errors.append("page_count_changed")
    if len(names) != len(attachments):
        errors.append("attachment_count_mismatch")
    for record in attachments:
        if record["attachment_name"] not in names:
            errors.append(f"missing_attachment:{record['attachment_name']}")
            continue
        if hashlib.sha256(reopened.embfile_get(record["attachment_name"])).hexdigest() != record["sha256"]:
            errors.append(f"attachment_hash_mismatch:{record['attachment_name']}")
    uri_links: set[str] = set()
    goto_destinations: set[int] = set()
    pages_with_timestamp_footer = 0
    pages_with_source_url_footer = 0
    pages_with_qa_preview_mark = 0
    pages_with_prohibited_partial_disclosure = 0
    for page_index in range(reopened.page_count):
        page = reopened.load_page(page_index)
        page_text = page.get_text("text")
        if "存储时间：" in page_text:
            pages_with_timestamp_footer += 1
        if "来源网址：" in page_text:
            pages_with_source_url_footer += 1
        if QA_PREVIEW_MARK in page_text:
            pages_with_qa_preview_mark += 1
        if any(text in page_text for text in PARTIAL_PDF_PROHIBITED_DISCLOSURES):
            pages_with_prohibited_partial_disclosure += 1
        for link in page.get_links():
            if link.get("kind") == fitz.LINK_URI and link.get("uri"):
                uri_links.add(str(link["uri"]))
            elif link.get("kind") == fitz.LINK_GOTO and isinstance(link.get("page"), int):
                goto_destinations.add(int(link["page"]))
    expected_urls = {str(item["url"]) for item in ranges if item.get("url")}
    missing_clickable_urls = sorted(expected_urls - uri_links)
    if missing_clickable_urls:
        errors.append("missing_clickable_source_urls:" + ",".join(missing_clickable_urls[:5]))
    missing_native_page_urls = sorted(expected_native_page_urls - uri_links)
    if missing_native_page_urls:
        errors.append("native_pdf_page_links_missing:" + ",".join(missing_native_page_urls[:5]))
    expected_source_destinations = {max(0, int(item["destination_page"]) - 1) for item in ranges}
    missing_source_destinations = sorted(expected_source_destinations - goto_destinations)
    if missing_source_destinations:
        errors.append("source_directory_jump_missing:" + ",".join(map(str, missing_source_destinations[:10])))
    sources_without_pdf_body = [
        str(item.get("id")) for item in ranges
        if item.get("platform") != "reference" and int(item.get("visual_pages_included") or 0) < 1
    ]
    if sources_without_pdf_body:
        errors.append("html_source_without_pdf_body:" + ",".join(sources_without_pdf_body[:10]))
    if pages_with_timestamp_footer != reopened.page_count:
        errors.append("timestamp_footer_missing")
    if pages_with_source_url_footer != reopened.page_count:
        errors.append("source_url_footer_missing")
    if qa_preview and pages_with_qa_preview_mark != reopened.page_count:
        errors.append("qa_preview_mark_missing")
    if partial_materials and pages_with_prohibited_partial_disclosure:
        errors.append("partial_materials_visible_disclosure_present")
    budgets_respected = all(
        item.get("budget") is None or int(item["total_pages"]) <= int(item["budget"])
        for item in platform_summaries.values()
    )
    if not budgets_respected:
        errors.append("platform_page_budget_exceeded")
    trailing_page_qa = trailing_low_information_qa(reopened, native_output_records)
    if not trailing_page_qa["ok"]:
        errors.append(
            "residual_low_information_pdf_tail:"
            + ",".join(item["source_id"] for item in trailing_page_qa["residual_sources"][:10])
        )
    reopened.close()
    if errors:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("PDF validation failed: " + ", ".join(errors))
    os.replace(temporary, output)

    attachment_manifest = [{k: v for k, v in record.items() if k != "source_path"} for record in attachments]
    technical_validation = {
        "ok": True,
        "pdf_reopened": True,
        "all_pages_loaded": True,
        "embedded_html_count_matches": True,
        "embedded_html_hashes_match": True,
        "failed_or_blocked_pages_included": 0,
        "clickable_source_links_present": not missing_clickable_urls,
        "all_source_urls_clickable": not missing_clickable_urls,
        "all_source_entries_jump_to_pdf_pages": not missing_source_destinations,
        "native_html_pdf_links_preserved": not missing_native_page_urls,
        "all_html_sources_have_pdf_body_page": not sources_without_pdf_body,
        "all_pages_have_timestamp_footer": pages_with_timestamp_footer == total_pages,
        "all_pages_have_source_url_footer": pages_with_source_url_footer == total_pages,
        "platform_page_budgets_respected": budgets_respected,
        "no_residual_low_information_pdf_tails": trailing_page_qa["ok"],
    }
    if qa_preview:
        technical_validation["qa_preview_mark_all_pages"] = pages_with_qa_preview_mark == total_pages
    if partial_materials:
        technical_validation["visible_partial_disclosures_absent"] = (
            pages_with_prohibited_partial_disclosure == 0
        )
        technical_validation["coverage_gap_pages_present"] = bool(coverage_pages)
    result = {
        "schema_version": "1.1",
        "record_type": "all_detected_html_pdf_manifest",
        "run_id": config.get("run_id") or run_dir.name,
        "output": os.path.relpath(output, start=manifest_path.parent).replace("\\", "/"),
        "output_size_bytes": output.stat().st_size,
        "output_sha256": sha256(output),
        "page_count": total_pages,
        "visual_source_count": len(ranges),
        "embedded_html_count": len(attachment_manifest),
        "native_html_pdf_source_count": native_pdf_source_count,
        "preserved_native_page_link_count": len(expected_native_page_urls),
        "trimmed_low_information_pdf_page_count": sum(
            int(item.get("visual_pages_trimmed_low_information") or 0) for item in ranges
        ),
        "low_information_pdf_tail_policy": {
            "only_contiguous_trailing_pages": True,
            "minimum_retained_source_pages": 1,
            "text_char_count_max": LOW_INFORMATION_TEXT_CHAR_LIMIT,
            "rendered_nonwhite_ratio_max": LOW_INFORMATION_NONWHITE_RATIO_LIMIT,
            "largest_raster_area_ratio_max": MEANINGFUL_RASTER_AREA_RATIO,
            "signals_combined_with": "and",
        },
        "trailing_low_information_qa": trailing_page_qa,
        "assessment_pdf_source_pages": assessment_pages,
        "generated_at": generated_at,
        "max_pages_per_platform": max(args.max_sales_pages_per_platform, args.max_search_pages_per_provider),
        "max_sales_pages_per_platform": args.max_sales_pages_per_platform,
        "max_search_pages_per_provider": args.max_search_pages_per_provider,
        "platforms": platform_summaries,
        "clickable_source_link_count": clickable_link_count,
        "footer_pages_with_timestamp": timestamp_footer_pages,
        "footer_pages_with_source_url": pages_with_source_url_footer,
        "footer_source_url_pages": source_url_footer_pages,
        "qa_preview_mark_pages": pages_with_qa_preview_mark if qa_preview else qa_preview_mark_pages,
        "partial_materials_visible_disclosure_pages": (
            pages_with_prohibited_partial_disclosure if partial_materials else 0
        ),
        "sources": ranges,
        "attachments": attachment_manifest,
        "excluded": excluded,
        "validation": technical_validation,
    }
    if qa_preview:
        result.update({
            "record_type": "workflow_qa_preview_manifest",
            "status": "incomplete_qa_preview",
            "qa_preview": True,
            "delivery_authorized": False,
            "coverage": coverage,
            "render_validation": dict(technical_validation),
            "validation": {
                **technical_validation,
                "ok": False,
                "production_matrix_complete": False,
                "delivery_authorized": False,
            },
        })
    if partial_materials:
        partial_validation = {
            **technical_validation,
            "ok": True,
            "investigation_complete": False,
            "visible_partial_disclosures_absent": pages_with_prohibited_partial_disclosure == 0,
            "coverage_gap_pages_present": bool(coverage_pages),
        }
        result.update({
            "record_type": PARTIAL_MANIFEST_RECORD_TYPE,
            "status": PARTIAL_STATUS,
            "partial_materials": True,
            "customer_request_required": True,
            "investigation_complete": False,
            "delivery_authorized": False,
            "completion_claim_allowed": False,
            "coverage": coverage,
            "task_coverage": task_coverage,
            "partial_materials_validation": partial_validation,
            "render_validation": dict(technical_validation),
            "validation": {
                **technical_validation,
                "ok": False,
                "production_matrix_complete": False,
                "investigation_complete": False,
                "delivery_authorized": False,
                "completion_claim_allowed": False,
            },
        })
    manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**result, "output_path": str(output), "manifest_path": str(manifest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build one PDF containing every accepted detected HTML page")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output")
    parser.add_argument("--manifest")
    parser.add_argument("--assessment-pdf")
    parser.add_argument("--assessment-html")
    parser.add_argument("--no-auto-assessment", action="store_true")
    parser.add_argument(
        "--qa-preview-incomplete", action="store_true",
        help=(
            "Build a visibly watermarked, non-deliverable layout preview from accepted sources even when "
            "the production matrix is incomplete; explicit noncanonical output and manifest paths are required"
        ),
    )
    parser.add_argument(
        "--partial-customer-request", action="store_true",
        help=(
            "Build the canonical, visibly watermarked partial-investigation materials packet from accepted "
            "sources while the matrix is incomplete; orchestration must separately prove the customer request"
        ),
    )
    parser.add_argument("--max-image-width", type=int, default=1600)
    parser.add_argument("--jpeg-quality", type=int, default=83)
    parser.add_argument(
        "--max-pages-per-platform", type=int,
        help="Legacy global section cap; overrides sales-platform and public-search caps",
    )
    parser.add_argument(
        "--max-sales-pages-per-platform", type=int, default=DEFAULT_SALES_PAGES_PER_PLATFORM,
        help=f"Cap each sales-platform section (default: {DEFAULT_SALES_PAGES_PER_PLATFORM})",
    )
    parser.add_argument(
        "--max-search-pages-per-provider", type=int, default=DEFAULT_SEARCH_PAGES_PER_PROVIDER,
        help=f"Cap each public-search section (default: {DEFAULT_SEARCH_PAGES_PER_PROVIDER})",
    )
    args = parser.parse_args()
    if args.max_pages_per_platform is not None:
        args.max_sales_pages_per_platform = args.max_pages_per_platform
        args.max_search_pages_per_provider = args.max_pages_per_platform
    if not 800 <= args.max_image_width <= 4000:
        raise ValueError("--max-image-width must be between 800 and 4000")
    if not 50 <= args.jpeg_quality <= 95:
        raise ValueError("--jpeg-quality must be between 50 and 95")
    if not MIN_PAGES_PER_SECTION <= args.max_sales_pages_per_platform <= MAX_PAGES_PER_SECTION:
        raise ValueError(
            f"--max-sales-pages-per-platform must be between {MIN_PAGES_PER_SECTION} and {MAX_PAGES_PER_SECTION}"
        )
    if not MIN_PAGES_PER_SECTION <= args.max_search_pages_per_provider <= MAX_PAGES_PER_SECTION:
        raise ValueError(
            f"--max-search-pages-per-provider must be between {MIN_PAGES_PER_SECTION} and {MAX_PAGES_PER_SECTION}"
        )
    result = build(args)
    print(json.dumps({
        "pdf": result["output_path"],
        "manifest": result["manifest_path"],
        "pages": result["page_count"],
        "visual_sources": result["visual_source_count"],
        "embedded_html": result["embedded_html_count"],
        "max_pages_per_platform": result["max_pages_per_platform"],
        "max_sales_pages_per_platform": result["max_sales_pages_per_platform"],
        "max_search_pages_per_provider": result["max_search_pages_per_provider"],
        "platform_pages": {key: value["total_pages"] for key, value in result["platforms"].items()},
        "size_mb": round(result["output_size_bytes"] / 1024 / 1024, 1),
        "sha256": result["output_sha256"],
        "qa_preview": bool(result.get("qa_preview")),
        "partial_materials": bool(result.get("partial_materials")),
        "delivery_authorized": result.get("delivery_authorized"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
