#!/usr/bin/env python3
"""Build a clearly labelled PDF of related search and sales-platform pages.

This is a lead packet, not the formal evidence binder. Normal result pages may be
included so relevant hits are not lost merely because owner attribution is weak.
Captcha, login, access-denied and empty-shell pages are never included.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import fitz
from audit_cherrystudio_run import validate_assisted_artifacts
from process_utils import run_bounded


ALLOWED_STATES = {"normal", "no_extractable_results", "zero_results"}
FORBIDDEN_STATES = {
    "captcha", "login_required", "access_denied", "empty_shell", "error",
    "repeated_page", "search_not_submitted",
}
PROVIDER_LABELS = {"so360": "360搜索", "sogou": "搜狗搜索", "baidu": "百度", "bing": "Bing", "yahoo": "Yahoo"}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def add_source(sources: list[dict], seen: set[Path], source: dict) -> None:
    path = Path(source["path"]).resolve()
    if path in seen or not path.is_file():
        return
    state = str(source.get("state") or "unknown")
    if state in FORBIDDEN_STATES or state not in ALLOWED_STATES:
        return
    seen.add(path)
    source["path"] = path
    sources.append(source)


def collect_sources(
    run_dir: Path,
    source_pdfs: list[str],
    include_empty: bool,
    include_visual_matches: bool = True,
    include_search_providers: bool = True,
    max_detail_pages: int = 2,
) -> list[dict]:
    sources: list[dict] = []
    seen: set[Path] = set()
    discovery = run_dir / "discovery"

    if include_search_providers:
        for summary_path in sorted(discovery.glob("Q*-summary.json")):
            summary = read_json(summary_path, {}) or {}
            query = str(summary.get("query") or summary.get("query_id") or summary_path.stem)
            for provider_run in summary.get("provider_runs") or []:
                state = str(provider_run.get("state") or "unknown")
                count = int(provider_run.get("result_count") or 0)
                if count < 1 and not include_empty:
                    continue
                diagnostic = provider_run.get("diagnostic_dir")
                if not diagnostic:
                    continue
                image = run_dir / diagnostic / "serp.png"
                provider = str(provider_run.get("provider") or "search")
                add_source(sources, seen, {
                    "kind": "image", "path": image, "state": state,
                    "label": f"{provider}：{query}（{count}条相关结果）",
                    "url": provider_run.get("final_url") or provider_run.get("search_url"),
                    "captured_at": provider_run.get("executed_at"),
                })
        providers_root = discovery / "providers"
        if providers_root.is_dir():
            for results_path in sorted(providers_root.glob("*/results.json")):
                provider_run = read_json(results_path, {}) or {}
                if provider_run.get("record_type") != "discovery_provider_run":
                    continue
                state = str(provider_run.get("state") or "unknown")
                count = int(provider_run.get("result_count") or 0)
                if count < 1 and not include_empty:
                    continue
                provider = str(provider_run.get("provider") or "search")
                query = str(provider_run.get("query") or provider_run.get("query_id") or results_path.parent.name)
                add_source(sources, seen, {
                    "kind": "image", "path": results_path.parent / "serp.png", "state": state,
                    "label": f"{PROVIDER_LABELS.get(provider, provider)}：{query}（{count}条相关结果）",
                    "platform": provider,
                    "platform_label": PROVIDER_LABELS.get(provider, provider),
                    "query_id": provider_run.get("query_id"),
                    "target_good": provider_run.get("target_good"),
                    "search_query": query,
                    "result_page_index": 1,
                    "url": provider_run.get("final_url") or provider_run.get("search_url"),
                    "captured_at": provider_run.get("captured_at"),
                })

    assisted = read_json(discovery / "assisted-sales-results.json", {}) or {}
    for platform_run in assisted.get("platform_runs") or []:
        page_runs = [item for item in platform_run.get("page_runs") or [] if isinstance(item, dict)]
        if page_runs:
            platform = str(platform_run.get("platform") or "platform")
            label = str(platform_run.get("platform_label") or platform)
            for page_run in page_runs:
                state = str(page_run.get("state") or "unknown")
                count = int(page_run.get("result_count") or 0)
                delivery_eligible = page_run.get("delivery_eligible") is True
                if not delivery_eligible and not (
                    include_empty and state in {"no_extractable_results", "zero_results"}
                ):
                    continue
                artifact_dir = str(page_run.get("artifact_dir") or "")
                if not artifact_dir:
                    continue
                artifacts = page_run.get("artifacts") or {}
                fullpage = (artifacts.get("fullpage") or {}).get("path")
                image = run_dir / str(fullpage or Path(artifact_dir) / "search.png")
                page_index = int(page_run.get("page_index") or 1)
                pages_requested = int(platform_run.get("pages_requested") or len(page_runs) or 1)
                add_source(sources, seen, {
                    "kind": "image", "path": image, "state": state,
                    "label": (
                        f"{label}站内搜索：{platform_run.get('search_query') or ''}"
                        f"（结果页 {page_index}/{pages_requested}，{count}条候选链接）"
                    ),
                    "platform": platform,
                    "platform_label": label,
                    "query_id": platform_run.get("query_id"),
                    "target_good": platform_run.get("target_good"),
                    "search_query": platform_run.get("search_query"),
                    "result_page_index": page_index,
                    "declared_page_number": page_run.get("declared_page_number"),
                    "pagination_verified": (page_run.get("transition") or {}).get("verified"),
                    "url": page_run.get("url"),
                    "captured_at": page_run.get("captured_at") or assisted.get("finished_at"),
                    "source_artifacts": artifacts,
                })
            continue
        state = str(platform_run.get("final_state") or "unknown")
        count = int(platform_run.get("result_count") or 0)
        delivery_eligible = platform_run.get("delivery_eligible") is True
        if not delivery_eligible and not (include_empty and state == "no_extractable_results"):
            continue
        platform = str(platform_run.get("platform") or "platform")
        label = str(platform_run.get("platform_label") or platform)
        artifact_dir = str(platform_run.get("artifact_dir") or "")
        if not artifact_dir:
            continue
        image = run_dir / artifact_dir / "search.png"
        add_source(sources, seen, {
            "kind": "image", "path": image, "state": state,
            "label": f"{label}站内搜索：{platform_run.get('search_query') or ''}（{count}条候选链接）",
            "platform": platform,
            "platform_label": label,
            "query_id": platform_run.get("query_id"),
            "target_good": platform_run.get("target_good"),
            "search_query": platform_run.get("search_query"),
            "url": platform_run.get("final_url"),
            "captured_at": assisted.get("finished_at"),
        })

    if include_visual_matches:
        visual_results = read_json(run_dir / "visual-match-results.json", {}) or {}
        for item in visual_results.get("items") or []:
            if item.get("retained") is not True:
                continue
            retained_rel = str(item.get("retained_dir") or "")
            retained_dir = (run_dir / retained_rel).resolve()
            if not retained_rel or not retained_dir.is_relative_to(run_dir):
                continue
            page_pdf = retained_dir / "page.pdf"
            fullpage = retained_dir / "fullpage.png"
            source_path = page_pdf if page_pdf.is_file() else fullpage
            if not source_path.is_file():
                continue
            status = str(item.get("status") or "visual_candidate")
            score = float(item.get("visual_score") or 0.0)
            add_source(sources, seen, {
                "kind": "pdf" if source_path.suffix.lower() == ".pdf" else "image",
                "path": source_path, "state": "normal",
                "label": f"图样候选 {item.get('candidate_id')}: {status}（视觉分数 {score:.3f}）",
                "url": item.get("url"), "captured_at": None,
                "max_pages": max(1, max_detail_pages) if source_path.suffix.lower() == ".pdf" else None,
                "is_detail_preview": True,
            })

    for index, raw in enumerate(source_pdfs, start=1):
        path = Path(raw).expanduser().resolve()
        if path in seen or not path.is_file():
            raise FileNotFoundError(f"Source PDF does not exist: {path}")
        seen.add(path)
        sources.append({
            "kind": "pdf", "path": path, "state": "normal",
            "label": f"补充网页截图材料 {index}", "url": None, "captured_at": None,
        })
    return sources


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a PDF packet of related web-result pages")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output")
    parser.add_argument("--source-pdf", action="append", default=[])
    parser.add_argument("--include-empty-results", action="store_true")
    parser.add_argument("--menu-only", action="store_true", help="Include search/list pages only; exclude direct product pages")
    parser.add_argument("--min-platforms", type=int, default=1, help="Require this many distinct normal sales platforms")
    parser.add_argument("--required-pages-per-query", type=int, default=1)
    parser.add_argument(
        "--max-detail-pages", type=int, default=2,
        help="Maximum pages retained from each direct-product PDF in this lead packet (default: 2)",
    )
    parser.add_argument("--pagination-validation", help="Validated sales-pagination JSON; defaults to RUN_DIR/sales-pagination-validation.json")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    config = read_json(run_dir / "run-config.json")
    if not isinstance(config, dict):
        raise ValueError("run-config.json is missing or invalid")
    orchestration = config.get("cherrystudio_orchestration") or {}
    if orchestration.get("workflow_mode") == "free_assisted_browser" and not args.menu_only:
        capture_summary = read_json(run_dir / "capture" / "sales-after-login" / "capture-summary.json") or {}
        if not (
            capture_summary.get("status") == "complete"
            and capture_summary.get("visual_match_complete") is True
        ):
            raise RuntimeError(
                "Direct-page capture is incomplete; refusing to build a final related-web-results PDF"
            )
        manual = read_json(run_dir / "discovery" / "manual-capture-queue.json") or {}
        assisted = read_json(run_dir / "discovery" / "assisted-sales-results.json") or {}
        assisted_errors, assisted_stats, _accepted = validate_assisted_artifacts(
            run_dir, manual, assisted,
        )
        expected = int(manual.get("task_count") or len(manual.get("items") or []) or 0)
        if assisted_errors or int(assisted_stats.get("validated_runs") or 0) != expected:
            raise RuntimeError(
                "Sales search artifacts failed strict query/visual/hash validation; "
                "refusing to build a final related-web-results PDF"
            )
    trademark = config.get("trademark") or {}
    sources = collect_sources(
        run_dir, args.source_pdf, args.include_empty_results,
        include_visual_matches=not args.menu_only,
        include_search_providers=not args.menu_only,
        max_detail_pages=max(1, min(10, args.max_detail_pages)),
    )
    if not sources:
        raise ValueError("No normal related-result screenshots or source PDFs are available")
    pagination_validation = None
    if args.menu_only:
        included_platforms = sorted({str(item.get("platform")) for item in sources if item.get("platform")})
        if args.required_pages_per_query > 1:
            validation_path = (
                Path(args.pagination_validation).resolve()
                if args.pagination_validation
                else run_dir / "sales-pagination-validation.json"
            )
            pagination_validation = read_json(validation_path)
            if not isinstance(pagination_validation, dict) or pagination_validation.get("ok") is not True:
                raise ValueError(f"Continuous pagination validation is missing or failed: {validation_path}")
            if int(pagination_validation.get("required_pages_per_query") or 0) != args.required_pages_per_query:
                raise ValueError("Pagination validation page requirement does not match the requested PDF package")
            validated_platforms = set(pagination_validation.get("qualified_platforms") or [])
            included_platforms = sorted(set(included_platforms) & validated_platforms)
        if len(included_platforms) < max(1, args.min_platforms):
            raise ValueError(
                f"Insufficient normal platform coverage: {len(included_platforms)} < {max(1, args.min_platforms)}"
            )
    else:
        included_platforms = []

    pages_dir = run_dir / ("sales-menu-result-pages" if args.menu_only else "related-result-pages")
    pages_dir.mkdir(parents=True, exist_ok=True)
    script_dir = Path(__file__).resolve().parent
    items = []
    for index, source in enumerate(sources, start=1):
        if source["kind"] == "image":
            output = pages_dir / f"R{index:03d}" / "page.pdf"
            output.parent.mkdir(parents=True, exist_ok=True)
            command = [
                sys.executable, str(script_dir / "raster-to-pdf.py"),
                "--image", str(source["path"]), "--output", str(output),
                "--title", source["label"], "--url", str(source.get("url") or ""),
            ]
            completed = run_bounded(command, timeout=180)
            if completed.returncode != 0 or not output.is_file():
                raise RuntimeError(f"Failed to convert result screenshot: {source['path']}\n{completed.stderr}")
            pdf_path = output
        else:
            pdf_path = source["path"]
            max_pages = int(source.get("max_pages") or 0)
            if max_pages > 0:
                output = pages_dir / f"R{index:03d}" / "detail-preview.pdf"
                output.parent.mkdir(parents=True, exist_ok=True)
                original = fitz.open(source["path"])
                try:
                    included_pages = min(original.page_count, max_pages)
                    preview = fitz.open()
                    preview.insert_pdf(original, from_page=0, to_page=included_pages - 1, links=True, annots=True)
                    if output.exists():
                        output.unlink()
                    preview.save(output, garbage=4, deflate=True)
                    preview.close()
                finally:
                    original.close()
                pdf_path = output
            else:
                included_pages = None
        items.append({
            "id": f"R{index:03d}", "label": source["label"], "pdf": str(pdf_path),
            "source_url": source.get("url"), "display_url": source.get("url"),
            "captured_at": source.get("captured_at"), "page_state": "related_lead",
            "platform": source.get("platform"), "platform_label": source.get("platform_label"),
            "query_id": source.get("query_id"), "target_good": source.get("target_good"),
            "search_query": source.get("search_query"),
            "result_page_index": source.get("result_page_index"),
            "declared_page_number": source.get("declared_page_number"),
            "pagination_verified": source.get("pagination_verified"),
            "source_artifacts": source.get("source_artifacts"),
            "detail_preview": bool(source.get("is_detail_preview")),
            "detail_pages_included": included_pages if source.get("is_detail_preview") else None,
            "original_detail_pdf": (
                str(Path(source["path"]).relative_to(run_dir)).replace("\\", "/")
                if source.get("is_detail_preview") and Path(source["path"]).is_relative_to(run_dir)
                else None
            ),
            "content_valid": True,
        })

    period = str(trademark.get("period") or "").replace("/", " 至 ")
    manifest = {
        "schema_version": "1.0",
        "record_type": "sales_menu_result_manifest" if args.menu_only else "related_web_result_manifest",
        "title": (
            f"第{trademark.get('registration_number') or ''}号“{trademark.get('name') or ''}”商标销售平台菜单线索汇编"
            if args.menu_only else
            f"第{trademark.get('registration_number') or ''}号“{trademark.get('name') or ''}”商标相关网页线索汇编"
        ),
        "cover": {
            "trademark_name": trademark.get("name"),
            "registration_number": trademark.get("registration_number"),
            "owner": trademark.get("owner"),
            "period": period,
            "summary": (
                f"本册仅收录 {len(items)} 组正常销售平台搜索菜单或列表页面，不含商品详情页。"
                if args.menu_only else
                f"本册收录 {len(items)} 组正常搜索或销售平台页面。相关命中即使尚未证明销售主体为目标注册人也予以保留，并在后续报告中区分已核实、待核实和排除。"
            ),
            "limitations": [
                "本册是相关网页线索汇编，不等同于商标实际使用的最终法律证据。",
                "搜索结果页可用于说明检索覆盖及发现候选，不自动证明商品来源或商标权属。",
                "验证码、登录、拒绝访问、错误和空白页面不进入本册。",
                "公开网络未发现仅代表本次检索范围。",
            ],
        },
        "coverage": {
            "query_strategy": "mark_plus_each_good_per_platform" if args.menu_only else None,
            "included_query_count": len({
                (item.get("platform"), item.get("query_id"))
                for item in items if item.get("query_id")
            }),
            "included_result_page_count": len(items),
            "included_platform_count": len(included_platforms),
            "included_platforms": included_platforms,
            "required_pages_per_query": args.required_pages_per_query if args.menu_only else None,
            "continuous_pagination_validated": bool(pagination_validation and pagination_validation.get("ok")),
            "pagination_validation": (
                "sales-pagination-validation.json" if pagination_validation else None
            ),
            "failed_or_blocked_pages_included": 0,
        },
        "items": items,
    }
    manifest_path = run_dir / ("sales-menu-results.manifest.json" if args.menu_only else "related-web-results.manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_path = Path(args.output).resolve() if args.output else run_dir / ("sales-menu-results.pdf" if args.menu_only else "related-web-results.pdf")
    canonical_output = (run_dir / ("sales-menu-results.pdf" if args.menu_only else "related-web-results.pdf")).resolve()
    if output_path != canonical_output:
        raise ValueError("Related-results PDF is an internal RUN artifact and cannot be written elsewhere")
    command = [
        sys.executable, str(script_dir / "merge-evidence-pdf.py"),
        "--manifest", str(manifest_path), "--output", str(output_path),
    ]
    completed = run_bounded(command, timeout=600)
    if completed.returncode != 0 or not output_path.is_file():
        raise RuntimeError(f"Failed to build related-results PDF\n{completed.stderr}")
    print(json.dumps({
        "related_results_pdf": str(output_path),
        "manifest": str(manifest_path),
        "included_source_count": len(items),
        "formal_evidence_binder": False,
        "menu_only": bool(args.menu_only),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
