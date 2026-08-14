#!/usr/bin/env python3
"""Fail-closed terminal audit for CherryStudio trademark investigations.

This script never writes a narrative conclusion.  It emits a machine-readable
audit and, only after every mode-specific gate passes, a completion receipt.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, unquote_plus, urlsplit

try:
    import fitz
except (ImportError, OSError):  # Preflight must report missing/broken binary runtimes cleanly.
    fitz = None

try:
    from PIL import Image
except (ImportError, OSError):  # See the fail-closed checks in ``audit_run`` below.
    Image = None


sys.path.insert(0, str(Path(__file__).resolve().parent))
from qcc_reference_guard import validate_qcc_reference
from runtime_policy import (
    PDF_TAIL_ANALYSIS_SCALE,
    PDF_TAIL_MAX_LARGEST_RASTER_AREA_RATIO,
    PDF_TAIL_MAX_NONWHITE_RATIO,
    PDF_TAIL_MAX_TEXT_CHARS,
    PDF_TAIL_NONWHITE_GRAY_THRESHOLD,
    PUBLIC_SEARCH_PROVIDERS,
    SALES_PLATFORMS,
)


DELIVERY_SEARCH_STATES = {"normal", "zero_results"}
BAD_PAGE_TITLE = re.compile(r"(?:^|\b)(?:400|401|403|404|405|429|500|502|503)(?:\b|错误)|错误页面|访问受限", re.I)
FORBIDDEN_REPORT_NAMES = {
    "调查报告.md", "investigation-report.md", "trademark-report.md",
}
ALLOWED_ROOT_MARKDOWN = {"visual-match-results.md"}


def normalize_sales_query(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("+", " ").replace("＋", " ")).strip()


def normalized_search_route(value) -> str:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
    return f"{(parsed.hostname or '').casefold()}{path.casefold()}"


def decoded_raw_query_values(value: str, key: str) -> list[tuple[str, str]]:
    """Decode URL query values with the UTF-8/GB18030 behavior used by 1688.

    Older and current 1688 result URLs may percent-encode Chinese query text
    with GBK.  ``parse_qs`` assumes UTF-8 and replaces those bytes, which would
    force a needless re-search of otherwise intact same-RUN captures.
    """
    try:
        raw_query = urlsplit(str(value or "")).query
    except ValueError:
        return []
    values: list[tuple[str, str]] = []
    for pair in raw_query.split("&"):
        raw_key, separator, raw_value = pair.partition("=")
        try:
            decoded_key = unquote_plus(raw_key, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
        if decoded_key != key:
            continue
        for encoding in ("utf-8", "gb18030"):
            try:
                decoded = unquote_plus(raw_value if separator else "", encoding=encoding, errors="strict")
            except UnicodeDecodeError:
                continue
            candidate = (decoded, encoding)
            if candidate not in values and not any(item[0] == decoded for item in values):
                values.append(candidate)
    return values


def url_query_binding_for_plan(planned: dict, actual_url: str) -> dict | None:
    if not isinstance(planned, dict):
        return None
    requested = str(planned.get("query") or "")
    expected_url = str(planned.get("search_url") or "")
    if not requested or normalized_search_route(expected_url) != normalized_search_route(actual_url):
        return None
    expected_params = parse_qs(urlsplit(expected_url).query, keep_blank_values=True)
    for key, values in expected_params.items():
        if not any(normalize_sales_query(value) == normalize_sales_query(requested) for value in values):
            continue
        actual_values = decoded_raw_query_values(actual_url, key)
        matched = next((
            (value, encoding) for value, encoding in actual_values
            if normalize_sales_query(value) == normalize_sales_query(requested)
        ), None)
        if matched is None:
            continue
        matched_value, matched_encoding = matched
        return {
            "schema_version": "1.0",
            "requested_query": requested,
            "normalized_requested_query": normalize_sales_query(requested),
            "expected_search_url": expected_url,
            "expected_route": normalized_search_route(expected_url),
            "actual_url": actual_url,
            "actual_route": normalized_search_route(actual_url),
            "route_matches": True,
            "url_query_key": key,
            "expected_url_query_value": requested,
            "actual_url_query_value": matched_value,
            "actual_url_query_encoding": matched_encoding,
            "url_query_matches": True,
            "visible_input_selector": None,
            "visible_input_value": None,
            "visible_input_matches": False,
            "verified_by": ["url_query"],
            "verified": True,
        }
    return None


def assisted_query_binding_valid(binding: dict, planned: dict, actual_url: str) -> bool:
    if not isinstance(binding, dict) or not isinstance(planned, dict):
        return False
    requested = str(planned.get("query") or "")
    expected_url = str(planned.get("search_url") or "")
    if not requested or not expected_url or not actual_url:
        return False
    if normalized_search_route(expected_url) != normalized_search_route(actual_url):
        return False
    expected_params = parse_qs(urlsplit(expected_url).query, keep_blank_values=True)
    actual_params = parse_qs(urlsplit(actual_url).query, keep_blank_values=True)
    query_keys = [
        key for key, values in expected_params.items()
        if any(normalize_sales_query(value) == normalize_sales_query(requested) for value in values)
    ]
    url_match = any(
        any(normalize_sales_query(value) == normalize_sales_query(requested) for value in actual_params.get(key, []))
        for key in query_keys
    )
    visible_match = bool(
        binding.get("visible_input_matches") is True
        and normalize_sales_query(binding.get("visible_input_value")) == normalize_sales_query(requested)
        and str(binding.get("visible_input_selector") or "").strip()
    )
    verified_by = set(binding.get("verified_by") or [])
    return bool(
        binding.get("verified") is True
        and binding.get("route_matches") is True
        and binding.get("requested_query") == requested
        and binding.get("expected_search_url") == expected_url
        and binding.get("actual_url") == actual_url
        and ((url_match and "url_query" in verified_by) or (visible_match and "visible_input" in verified_by))
    )


def read_json(path: Path, default=None):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def nonempty_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def add(errors: list[str], condition: bool, code: str) -> None:
    if not condition:
        errors.append(code)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def confined_path(run_dir: Path, raw_path, expected_root: Path) -> Path | None:
    """Resolve an artifact path and reject traversal, symlinks, and foreign roots."""
    if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
        return None
    candidate = Path(str(raw_path).strip())
    if not candidate.is_absolute():
        candidate = run_dir / candidate
    try:
        resolved = candidate.resolve(strict=False)
        root = expected_root.resolve(strict=False)
        return resolved if resolved.is_relative_to(root) else None
    except (OSError, RuntimeError, ValueError):
        return None


def validate_pdf(path: Path) -> tuple[bool, str, int]:
    """Open and inspect a PDF; a header-sized placeholder or blank PDF is not evidence."""
    if not path.is_file():
        return False, "missing", 0
    try:
        if path.stat().st_size < 256:
            return False, "too_small", 0
    except OSError:
        return False, "unreadable", 0
    if fitz is None:
        return False, "pymupdf_unavailable", 0
    document = None
    try:
        document = fitz.open(path)
        if document.needs_pass:
            return False, "encrypted", document.page_count
        if document.page_count < 1:
            return False, "no_pages", 0
        meaningful = False
        for page_number in range(document.page_count):
            page = document.load_page(page_number)
            rect = page.rect
            if not all(map(lambda value: value > 10, (rect.width, rect.height))):
                return False, f"invalid_page_geometry_{page_number + 1}", document.page_count
            if meaningful:
                continue
            if page.get_text("text").strip() or page.get_images(full=True) or page.get_drawings():
                meaningful = True
        if not meaningful:
            # Some valid raster PDFs expose neither images nor drawings through an
            # unusual content stream. Render one page and reject an all-white shell.
            page = document.load_page(0)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(0.35, 0.35), colorspace=fitz.csRGB, alpha=False)
            samples = memoryview(pixmap.samples)
            stride = max(3, (len(samples) // 12000 // 3) * 3)
            meaningful = any(samples[index] < 245 for index in range(0, len(samples), stride))
        if not meaningful:
            return False, "blank", document.page_count
        return True, "ok", document.page_count
    except Exception as error:
        return False, f"unreadable_{type(error).__name__}", 0
    finally:
        if document is not None:
            document.close()


def pdf_tail_page_metric(page) -> dict:
    """Recompute the shared conservative low-information signals physically."""
    text_char_count = len(re.sub(r"\s+", "", page.get_text("text") or ""))
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(PDF_TAIL_ANALYSIS_SCALE, PDF_TAIL_ANALYSIS_SCALE),
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    samples = pixmap.samples
    nonwhite_ratio = (
        sum(value < PDF_TAIL_NONWHITE_GRAY_THRESHOLD for value in samples) / len(samples)
        if samples else 0.0
    )
    page_area = max(1.0, page.rect.width * page.rect.height)
    largest_raster_area_ratio = 0.0
    for image in page.get_image_info(xrefs=True):
        bbox = fitz.Rect(image.get("bbox") or fitz.Rect()) & page.rect
        if bbox.is_empty or bbox.is_infinite:
            continue
        largest_raster_area_ratio = max(
            largest_raster_area_ratio,
            bbox.width * bbox.height / page_area,
        )
    return {
        "text_char_count": text_char_count,
        "rendered_nonwhite_ratio": nonwhite_ratio,
        "largest_raster_area_ratio": largest_raster_area_ratio,
        "low_information": bool(
            text_char_count <= PDF_TAIL_MAX_TEXT_CHARS
            and nonwhite_ratio <= PDF_TAIL_MAX_NONWHITE_RATIO
            and largest_raster_area_ratio <= PDF_TAIL_MAX_LARGEST_RASTER_AREA_RATIO
        ),
    }


def validate_all_detected_delivery_bundle(run_dir: Path, config: dict | None = None) -> tuple[list[str], dict]:
    """Physically re-verify the only publishable CherryStudio PDF bundle."""
    errors: list[str] = []
    manifest_path = run_dir / "all-detected-html-pages.manifest.json"
    pdf_path = run_dir / "all-detected-html-pages.pdf"
    manifest = read_json(manifest_path, {}) or {}
    validation = manifest.get("validation") if isinstance(manifest.get("validation"), dict) else {}
    required_validation = (
        "ok", "embedded_html_count_matches", "embedded_html_hashes_match",
        "all_source_entries_jump_to_pdf_pages", "native_html_pdf_links_preserved",
        "all_html_sources_have_pdf_body_page", "all_source_urls_clickable",
        "all_pages_have_timestamp_footer", "all_pages_have_source_url_footer",
        "platform_page_budgets_respected", "no_residual_low_information_pdf_tails",
    )
    for field in required_validation:
        if validation.get(field) is not True:
            errors.append(f"all_detected_manifest_validation_false:{field}")
    if validation.get("failed_or_blocked_pages_included") != 0:
        errors.append("all_detected_manifest_contains_blocked_pages")
    if manifest.get("record_type") != "all_detected_html_pdf_manifest":
        errors.append("all_detected_manifest_record_type_invalid")
    if config and str(manifest.get("run_id") or "") != str(config.get("run_id") or ""):
        errors.append("all_detected_manifest_run_id_mismatch")
    pdf_ok, pdf_reason, pdf_pages = validate_pdf(pdf_path)
    if not pdf_ok:
        errors.append(f"all_detected_html_pdf_invalid:{pdf_reason}")
    if not manifest_path.is_file():
        errors.append("all_detected_html_manifest_missing")
    elif not pdf_path.is_file() or manifest.get("output_sha256") != sha256(pdf_path):
        errors.append("all_detected_html_pdf_hash_mismatch")
    if pdf_path.is_file() and int(manifest.get("output_size_bytes") or -1) != pdf_path.stat().st_size:
        errors.append("all_detected_html_pdf_size_mismatch")
    if int(manifest.get("page_count") or -1) != pdf_pages:
        errors.append("all_detected_html_pdf_page_count_mismatch")

    attachments = [value for value in manifest.get("attachments") or [] if isinstance(value, dict)]
    expected_attachment_count = int(manifest.get("embedded_html_count") or 0)
    if expected_attachment_count < 1 or len(attachments) != expected_attachment_count:
        errors.append("all_detected_html_attachment_manifest_empty_or_mismatched")
    sources = [value for value in manifest.get("sources") or [] if isinstance(value, dict)]
    if not sources:
        errors.append("all_detected_html_sources_empty")

    tail_qa = manifest.get("trailing_low_information_qa")
    if not isinstance(tail_qa, dict) or tail_qa.get("ok") is not True:
        errors.append("all_detected_tail_quality_qa_missing_or_false")
    elif int(tail_qa.get("residual_source_count") or 0) != 0 or tail_qa.get("residual_sources"):
        errors.append("all_detected_tail_quality_qa_has_residual_sources")

    source_tail_checks = 0
    physically_trimmed_source_pages = 0
    declared_trimmed_total = 0
    for source in sources:
        source_id = str(source.get("id") or "unknown")
        try:
            original = int(source.get("original_rendered_page_count"))
            established = int(source.get("rendered_page_count"))
            effective = int(source.get("effective_rendered_page_count"))
            included = int(source.get("visual_pages_included"))
            trimmed_count = int(source.get("visual_pages_trimmed_low_information"))
            omitted = int(source.get("visual_pages_omitted"))
            omitted_by_budget = int(source.get("visual_pages_omitted_by_budget"))
        except (TypeError, ValueError):
            errors.append(f"all_detected_tail_quality_counts_missing:{source_id}")
            continue
        if not (original >= effective >= included >= 1) or established != original:
            errors.append(f"all_detected_tail_quality_counts_invalid:{source_id}")
            continue
        expected_trimmed_indexes = list(range(effective + 1, original + 1))
        declared_indexes = source.get("trimmed_source_page_indexes")
        if declared_indexes != expected_trimmed_indexes:
            errors.append(f"all_detected_tail_quality_indexes_invalid:{source_id}")
        if int(source.get("trimmed_source_page_index_base") or 0) != 1:
            errors.append(f"all_detected_tail_quality_index_base_invalid:{source_id}")
        if (
            trimmed_count != original - effective
            or omitted != original - included
            or omitted_by_budget != effective - included
        ):
            errors.append(f"all_detected_tail_quality_omission_counts_invalid:{source_id}")
        trimmed_records = [
            value for value in source.get("trimmed_pages") or [] if isinstance(value, dict)
        ]
        if [value.get("source_page_index") for value in trimmed_records] != expected_trimmed_indexes:
            errors.append(f"all_detected_tail_quality_records_invalid:{source_id}")
        if any(value.get("reason") != "continuous_low_information_pdf_tail" for value in trimmed_records):
            errors.append(f"all_detected_tail_quality_reason_invalid:{source_id}")
        declared_trimmed_total += trimmed_count

        render_source = confined_path(run_dir, source.get("render_source"), run_dir)
        if render_source is None or not render_source.is_file():
            errors.append(f"all_detected_render_source_missing_or_unconfined:{source_id}")
            continue
        if source.get("render_source_sha256") != sha256(render_source):
            errors.append(f"all_detected_render_source_hash_mismatch:{source_id}")
        if render_source.suffix.casefold() != ".pdf" or fitz is None:
            continue
        source_document = None
        try:
            source_document = fitz.open(render_source)
            if source_document.needs_pass or source_document.page_count != original:
                errors.append(f"all_detected_tail_quality_source_pdf_invalid:{source_id}")
                continue
            metrics = [pdf_tail_page_metric(page) for page in source_document]
            if any(not metrics[index - 1]["low_information"] for index in expected_trimmed_indexes):
                errors.append(f"all_detected_declared_trimmed_page_is_meaningful:{source_id}")
            if effective > 1 and metrics[effective - 1]["low_information"]:
                errors.append(f"all_detected_residual_low_information_tail:{source_id}")
            source_tail_checks += 1
            physically_trimmed_source_pages += len(expected_trimmed_indexes)
        except Exception as error:
            errors.append(f"all_detected_tail_quality_physical_check_failed:{source_id}:{type(error).__name__}")
        finally:
            if source_document is not None:
                source_document.close()

    if int(manifest.get("trimmed_low_information_pdf_page_count") or 0) != declared_trimmed_total:
        errors.append("all_detected_trimmed_page_total_mismatch")

    physical_attachment_errors: list[str] = []
    timestamp_pages = source_footer_pages = beijing_time_pages = 0
    uri_links: set[str] = set()
    goto_links = 0
    document = None
    if fitz is not None and pdf_ok:
        try:
            document = fitz.open(pdf_path)
            embedded_names = set(document.embfile_names())
            if len(embedded_names) != expected_attachment_count:
                physical_attachment_errors.append("embedded_attachment_count_mismatch")
            for record in attachments:
                name = str(record.get("attachment_name") or "")
                rel = str(record.get("path") or "").replace("\\", "/")
                if not rel.casefold().endswith((".html", ".htm")):
                    physical_attachment_errors.append(f"non_html_attachment:{name}")
                if rel.startswith("capture-diagnostics/"):
                    physical_attachment_errors.append(f"diagnostic_attachment:{name}")
                if name not in embedded_names:
                    physical_attachment_errors.append(f"missing_attachment:{name}")
                    continue
                payload = document.embfile_get(name)
                if hashlib.sha256(payload).hexdigest() != str(record.get("sha256") or "").casefold():
                    physical_attachment_errors.append(f"attachment_hash_mismatch:{name}")
            for page in document:
                text = page.get_text("text")
                timestamp_pages += int("存储时间：" in text)
                source_footer_pages += int("来源网址：" in text)
                beijing_time_pages += int("+08:00" in text or "Asia/Shanghai" in text)
                for link in page.get_links():
                    if link.get("kind") == fitz.LINK_URI and link.get("uri"):
                        uri_links.add(str(link["uri"]))
                    elif link.get("kind") == fitz.LINK_GOTO:
                        goto_links += 1
            if timestamp_pages != document.page_count:
                errors.append("all_detected_timestamp_footer_missing")
            if source_footer_pages != document.page_count:
                errors.append("all_detected_source_url_footer_missing")
            if beijing_time_pages != document.page_count:
                errors.append("all_detected_visible_time_not_asia_shanghai")
            expected_urls = {str(value.get("url")) for value in sources if value.get("url")}
            missing_urls = expected_urls - uri_links
            if missing_urls:
                errors.append("all_detected_source_urls_not_clickable")
            if not uri_links:
                errors.append("all_detected_pdf_has_no_uri_links")
            if goto_links < len(sources):
                errors.append("all_detected_source_directory_links_incomplete")
        except Exception as error:
            errors.append(f"all_detected_physical_validation_failed:{type(error).__name__}")
        finally:
            if document is not None:
                document.close()
    errors.extend(physical_attachment_errors)
    stats = {
        "ok": not errors,
        "pdf_sha256": sha256(pdf_path) if pdf_path.is_file() else None,
        "manifest_sha256": sha256(manifest_path) if manifest_path.is_file() else None,
        "page_count": pdf_pages,
        "embedded_html_count": expected_attachment_count,
        "timestamp_footer_pages": timestamp_pages,
        "source_url_footer_pages": source_footer_pages,
        "asia_shanghai_footer_pages": beijing_time_pages,
        "uri_link_count": len(uri_links),
        "goto_link_count": goto_links,
        "tail_quality_source_checks": source_tail_checks,
        "declared_trimmed_source_pages": declared_trimmed_total,
        "physically_verified_trimmed_source_pages": physically_trimmed_source_pages,
    }
    return list(dict.fromkeys(errors)), stats


def valid_image(path: Path) -> bool:
    if Image is None or not nonempty_file(path):
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def artifact_record_matches(run_dir: Path, record, expected_path: Path) -> bool:
    if not isinstance(record, dict) or not expected_path.is_file():
        return False
    recorded = confined_path(run_dir, record.get("path"), expected_path.parent)
    if recorded != expected_path.resolve(strict=False):
        return False
    try:
        if int(record.get("size_bytes") or -1) != expected_path.stat().st_size:
            return False
    except (OSError, TypeError, ValueError):
        return False
    recorded_hash = str(record.get("sha256") or "").casefold()
    return bool(re.fullmatch(r"[0-9a-f]{64}", recorded_hash)) and recorded_hash == sha256(expected_path)


def normalized_path_value(value) -> str:
    if not str(value or "").strip():
        return ""
    try:
        return str(Path(str(value)).resolve(strict=False)).replace("\\", "/").casefold()
    except (OSError, RuntimeError, ValueError):
        return ""


def validate_public_browser_attachment(
    config: dict, state: dict, public: dict,
) -> tuple[list[str], dict]:
    errors: list[str] = []
    orchestration = config.get("cherrystudio_orchestration") or {}
    expected = {
        "attached_to_existing_browser": True,
        "browser_product": str(orchestration.get("selected_browser") or "").casefold(),
        "browser_executable": normalized_path_value(orchestration.get("browser_executable")),
        "browser_user_data": normalized_path_value(orchestration.get("browser_user_data")),
        "profile_directory": str(orchestration.get("profile_directory") or ""),
        "cdp_endpoint": str(orchestration.get("cdp_endpoint") or "").rstrip("/"),
        "cdp_browser": str(orchestration.get("cdp_browser") or ""),
    }
    locked_state = {
        "browser_product": str(state.get("default_browser") or "").casefold(),
        "browser_executable": normalized_path_value(state.get("browser_executable")),
        "browser_user_data": normalized_path_value(state.get("browser_user_data")),
        "profile_directory": str(state.get("profile_directory") or ""),
        "cdp_endpoint": str(state.get("cdp_endpoint") or "").rstrip("/"),
        "cdp_browser": str(state.get("cdp_browser") or ""),
    }
    if any(not expected[key] for key in (
        "browser_product", "browser_executable", "browser_user_data", "profile_directory",
        "cdp_endpoint", "cdp_browser",
    )) or any(locked_state.get(key) != expected.get(key) for key in locked_state):
        errors.append("public_browser_attachment_locked_identity_missing_or_mismatched")

    def normalized_attachment(value) -> dict:
        value = value if isinstance(value, dict) else {}
        return {
            "attached_to_existing_browser": value.get("attached_to_existing_browser") is True,
            "browser_product": str(value.get("browser_product") or "").casefold(),
            "browser_executable": normalized_path_value(value.get("browser_executable")),
            "browser_user_data": normalized_path_value(value.get("browser_user_data")),
            "profile_directory": str(value.get("profile_directory") or ""),
            "cdp_endpoint": str(value.get("cdp_endpoint") or "").rstrip("/"),
            "cdp_browser": str(value.get("cdp_browser") or ""),
        }

    summary_attachment = normalized_attachment(public.get("browser_attachment"))
    if summary_attachment != expected:
        errors.append("public_browser_attachment_summary_mismatch")
    delivered = [
        item for item in public.get("provider_runs") or []
        if isinstance(item, dict) and item.get("state") in DELIVERY_SEARCH_STATES
    ]
    mismatched_runs = []
    for item in delivered:
        if normalized_attachment(item.get("browser_attachment")) != expected:
            mismatched_runs.append(str(item.get("query_id") or "unknown"))
        if (item.get("interactive_search") or {}).get("attached_to_existing_browser") is not True:
            mismatched_runs.append(str(item.get("query_id") or "unknown"))
    if mismatched_runs:
        errors.append("public_provider_browser_attachment_mismatch:" + ",".join(sorted(set(mismatched_runs))))
    return errors, {"expected": expected, "summary": summary_attachment, "delivered_run_count": len(delivered)}


def validate_period(config: dict) -> tuple[bool, dict]:
    trademark = config.get("trademark") or {}
    orchestration = config.get("cherrystudio_orchestration") or {}
    raw = str(trademark.get("period") or "").strip()
    source = str(orchestration.get("period_source") or "").strip()
    result = {"period": raw, "period_source": source, "start": None, "end": None}
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})", raw)
    derived_sources = {
        "derived_three_year_lookback_asia_shanghai",
        "derived_three_year_lookback_utc",  # Accepted only for audit compatibility with older RUNs.
    }
    if not match or source not in {"provided", *derived_sources}:
        return False, result
    try:
        start, end = (date.fromisoformat(value) for value in match.groups())
    except ValueError:
        return False, result
    result.update({"start": start.isoformat(), "end": end.isoformat()})
    if start >= end:
        return False, result
    if source in derived_sources:
        try:
            expected_start = end.replace(year=end.year - 3)
        except ValueError:
            expected_start = end.replace(year=end.year - 3, day=28)
        if start != expected_start:
            return False, result
    return True, result


def validate_assisted_artifacts(
    run_dir: Path, manual: dict, assisted: dict,
) -> tuple[list[str], dict, dict]:
    errors: list[str] = []
    stats = {"validated_runs": 0, "validated_pages": 0, "validated_pdfs": 0}
    accepted_sources: dict = {}
    if assisted.get("schema_version") != "2.0" or assisted.get("record_type") != "browser_sales_platform_discovery":
        errors.append("assisted_matrix_schema_invalid")
    planned = {
        str(item.get("task_id")): item for item in manual.get("items") or []
        if isinstance(item, dict) and item.get("task_id")
    }
    runs = [item for item in assisted.get("platform_runs") or [] if isinstance(item, dict)]
    if len(runs) != len(assisted.get("platform_runs") or []) or len(runs) != int(assisted.get("query_count") or -1):
        errors.append("assisted_matrix_run_count_mismatch")
    seen: set[str] = set()
    assisted_root = run_dir / "discovery" / "assisted-platforms"
    for platform_run in runs:
        query_id = str(platform_run.get("query_id") or "")
        plan = planned.get(query_id)
        if not query_id or query_id in seen:
            errors.append(f"assisted_query_id_duplicate_or_missing:{query_id or 'unknown'}")
        seen.add(query_id)
        identity_ok = bool(plan) and all((
            platform_run.get("platform") == plan.get("platform"),
            platform_run.get("search_query") == plan.get("query"),
            platform_run.get("target_good") == plan.get("target_good"),
            platform_run.get("expected_search_url") == plan.get("search_url"),
        ))
        if not identity_ok:
            errors.append(f"assisted_platform_run_identity_mismatch:{query_id or 'unknown'}")
        if platform_run.get("delivery_eligible") is not True:
            continue
        pages = [item for item in platform_run.get("page_runs") or [] if isinstance(item, dict)]
        eligible_pages = [item for item in pages if item.get("delivery_eligible") is True]
        if not eligible_pages:
            errors.append(f"assisted_delivery_page_missing:{query_id or 'unknown'}")
            continue
        stats["validated_runs"] += 1
        first_dir = str(eligible_pages[0].get("artifact_dir") or "")
        if str(platform_run.get("artifact_dir") or "") != first_dir:
            errors.append(f"assisted_run_artifact_identity_mismatch:{query_id or 'unknown'}")
        for page_run in eligible_pages:
            page_index = int(page_run.get("page_index") or 0)
            label = f"{query_id or 'unknown'}:p{page_index or 0}"
            artifact_dir = confined_path(run_dir, page_run.get("artifact_dir"), assisted_root)
            if artifact_dir is None or artifact_dir == assisted_root.resolve(strict=False):
                errors.append(f"assisted_artifact_path_outside_delivery_tree:{label}")
                continue
            metadata = read_json(artifact_dir / "metadata.json")
            if not isinstance(metadata, dict):
                errors.append(f"assisted_metadata_missing_or_invalid:{label}")
                continue
            for field in (
                "page_index", "state", "url", "captured_at", "result_count", "delivery_eligible",
                "artifact_dir", "platform", "query_id", "search_query", "target_good",
                "expected_search_url", "submitted_query_verified",
            ):
                if metadata.get(field) != page_run.get(field):
                    errors.append(f"assisted_metadata_identity_mismatch:{label}:{field}")
            state = str(page_run.get("state") or "")
            if state not in DELIVERY_SEARCH_STATES:
                errors.append(f"assisted_delivery_state_invalid:{label}")
            if state == "normal" and int(page_run.get("result_count") or 0) <= 0:
                errors.append(f"assisted_normal_result_has_no_items:{label}")
            if state == "zero_results" and page_run.get("explicit_zero_results") is not True:
                errors.append(f"assisted_zero_result_not_explicit:{label}")
            binding = metadata.get("query_binding") or {}
            if not (
                assisted_query_binding_valid(binding, plan or {}, str(metadata.get("url") or ""))
                and metadata.get("submitted_query_verified") is True
            ):
                errors.append(f"assisted_query_binding_invalid:{label}")
            visual = metadata.get("visual_capture") or {}
            if not (
                visual.get("strategy") == "viewport_tile_stitch_v1"
                and visual.get("acceptable") is True
                and visual.get("output_created") is True
                and metadata.get("capture_strategy") == "viewport_tile_stitch_v1"
                and metadata.get("image_load_complete") is True
            ):
                errors.append(f"assisted_visual_capture_invalid:{label}")
            required = {
                "rendered_dom": "rendered-dom.html", "search_html": "search.html",
                "fullpage": "search.png", "mhtml": "page.mhtml", "pdf": "page.pdf",
                "items": "items.json",
            }
            artifacts = metadata.get("artifacts") or {}
            for key, filename in required.items():
                target = artifact_dir / filename
                valid_file = nonempty_file(target)
                if key == "fullpage":
                    valid_file = valid_image(target)
                if not valid_file:
                    errors.append(f"assisted_artifact_missing_or_invalid:{label}:{filename}")
                elif not artifact_record_matches(run_dir, artifacts.get(key), target):
                    errors.append(f"assisted_artifact_record_mismatch:{label}:{filename}")
            pdf_path = artifact_dir / "page.pdf"
            pdf_ok, pdf_reason, _ = validate_pdf(pdf_path)
            if not pdf_ok:
                errors.append(f"assisted_artifact_pdf_invalid:{label}:{pdf_reason}")
            else:
                stats["validated_pdfs"] += 1
            items = read_json(artifact_dir / "items.json")
            if not isinstance(items, list) or len(items) != int(page_run.get("result_count") or 0):
                errors.append(f"assisted_items_identity_mismatch:{label}")
            stats["validated_pages"] += 1
            accepted_sources[(query_id, str(platform_run.get("platform") or ""), page_index)] = {
                "url": str(page_run.get("url") or ""), "artifact_dir": artifact_dir,
                "result_count": int(page_run.get("result_count") or 0),
            }
    if set(planned) != seen:
        errors.append("assisted_query_plan_identity_mismatch")
    return errors, stats, accepted_sources


def validate_public_artifacts(
    run_dir: Path, public_plan: dict, public: dict,
) -> tuple[list[str], dict, dict]:
    errors: list[str] = []
    stats = {"validated_runs": 0, "validated_pdfs": 0}
    accepted_sources: dict = {}
    if public.get("schema_version") != "1.0" or public.get("record_type") != "public_search_goods_matrix":
        errors.append("public_matrix_schema_invalid")
    planned = {
        str(item.get("query_id")): item for item in public_plan.get("items") or []
        if isinstance(item, dict) and item.get("query_id")
    }
    runs = [item for item in public.get("provider_runs") or [] if isinstance(item, dict)]
    if len(runs) != len(public.get("provider_runs") or []) or len(runs) != int(public.get("query_count") or -1):
        errors.append("public_matrix_run_count_mismatch")
    seen: set[str] = set()
    providers_root = run_dir / "discovery" / "providers"
    for provider_run in runs:
        query_id = str(provider_run.get("query_id") or "")
        plan = planned.get(query_id)
        if not query_id or query_id in seen:
            errors.append(f"public_query_id_duplicate_or_missing:{query_id or 'unknown'}")
        seen.add(query_id)
        identity_ok = bool(plan) and all((
            provider_run.get("provider") == plan.get("provider"),
            provider_run.get("query") == plan.get("query"),
            provider_run.get("query_kind") == plan.get("query_kind"),
            provider_run.get("target_good") == plan.get("target_good"),
        ))
        if not identity_ok:
            errors.append(f"public_provider_run_identity_mismatch:{query_id or 'unknown'}")
        if provider_run.get("state") not in DELIVERY_SEARCH_STATES:
            continue
        artifact_dir = confined_path(run_dir, provider_run.get("artifact_dir"), providers_root)
        label = query_id or "unknown"
        if artifact_dir is None or artifact_dir == providers_root.resolve(strict=False):
            errors.append(f"public_artifact_path_outside_delivery_tree:{label}")
            continue
        persisted = read_json(artifact_dir / "results.json")
        if not isinstance(persisted, dict) or any(
            persisted.get(field) != provider_run.get(field)
            for field in ("record_type", "query_id", "query", "provider", "state", "result_count", "final_url")
        ):
            errors.append(f"public_results_identity_mismatch:{label}")
        if provider_run.get("record_type") != "discovery_provider_run" or provider_run.get("schema_version") != "2.0":
            errors.append(f"public_provider_schema_invalid:{label}")
        artifacts = provider_run.get("artifacts") or {}
        required = {"html": "serp.html", "screenshot": "serp.png", "pdf": "serp.pdf"}
        for key, filename in required.items():
            target = artifact_dir / filename
            valid_file = nonempty_file(target) if key != "screenshot" else valid_image(target)
            if not valid_file:
                errors.append(f"public_artifact_missing_or_invalid:{label}:{filename}")
            if artifacts.get(key) != filename:
                errors.append(f"public_artifact_record_mismatch:{label}:{filename}")
        pdf_ok, pdf_reason, _ = validate_pdf(artifact_dir / "serp.pdf")
        if not pdf_ok:
            errors.append(f"public_artifact_pdf_invalid:{label}:{pdf_reason}")
        else:
            stats["validated_pdfs"] += 1
        stats["validated_runs"] += 1
        accepted_sources[(query_id, str(provider_run.get("provider") or ""), 1)] = {
            "url": str(provider_run.get("final_url") or provider_run.get("search_url") or ""),
            "artifact_dir": artifact_dir, "result_count": int(provider_run.get("result_count") or 0),
        }
    if set(planned) != seen:
        errors.append("public_query_plan_identity_mismatch")
    return errors, stats, accepted_sources


def validate_capture_artifacts(
    run_dir: Path, config: dict, assisted: dict, capture: dict,
) -> tuple[list[str], dict, set[str]]:
    errors: list[str] = []
    stats = {"validated_attempts": 0, "validated_pdfs": 0, "diagnostic_detail_skips": 0}
    accepted_urls: set[str] = set()
    if (
        capture.get("schema_version") != "1.0"
        or capture.get("record_type") != "sales_platform_post_login_capture"
        or capture.get("run_id") != config.get("run_id")
    ):
        errors.append("sales_capture_summary_schema_or_run_identity_invalid")
    qcc_reference_exists = (run_dir / "reference" / "qcc-reference.json").is_file()
    if capture.get("visual_match_required") is not qcc_reference_exists:
        errors.append("sales_capture_visual_match_requirement_mismatch")
    if capture.get("visual_match_required") is True:
        visual_match = capture.get("visual_match") or {}
        if visual_match.get("status") != "complete" or capture.get("visual_match_complete") is not True:
            errors.append("sales_capture_visual_match_incomplete")
        output = confined_path(run_dir, visual_match.get("output"), run_dir)
        visual_result = read_json(output) if output is not None else None
        if (
            output != (run_dir / "visual-match-results.json").resolve(strict=False)
            or not isinstance(visual_result, dict)
            or visual_result.get("schema_version") != "1.0"
            or visual_result.get("record_type") != "qcc_reference_visual_match_results"
        ):
            errors.append("sales_capture_visual_match_output_invalid")
    attempts = [item for item in capture.get("attempts") or [] if isinstance(item, dict)]
    if len(attempts) != len(capture.get("attempts") or []):
        errors.append("sales_capture_attempt_schema_invalid")
    attempted_count = int(capture.get("attempted_count") or 0)
    accepted_count = int(capture.get("accepted_count") or 0)
    detail_capture_required = capture.get("detail_capture_required") is not False
    diagnostic_skip_count = int(capture.get("diagnostic_detail_skip_count") or 0)
    if attempted_count != len(attempts):
        errors.append("sales_capture_attempt_count_mismatch")
    accepted_attempts = [
        item for item in attempts
        if item.get("content_valid") is True and item.get("pdf_created") is True
    ]
    if accepted_count != len(accepted_attempts):
        errors.append("sales_capture_accepted_count_mismatch")
    diagnostic_attempts = [item for item in attempts if item.get("diagnostic_only") is True]
    if diagnostic_skip_count != len(diagnostic_attempts):
        errors.append("sales_capture_diagnostic_skip_count_mismatch")
    if not attempts:
        platform_runs = [item for item in assisted.get("platform_runs") or [] if isinstance(item, dict)]
        if (
            assisted.get("items")
            or not platform_runs
            or any(item.get("delivery_eligible") is not True for item in platform_runs)
            or capture.get("status") != "complete"
        ):
            errors.append("sales_capture_empty_attempts_not_justified")
        return errors, stats, accepted_urls

    capture_root = run_dir / "capture" / "sales-after-login"
    seen_ids: set[str] = set()
    for attempt in attempts:
        candidate_id = str(attempt.get("candidate_id") or "")
        label = candidate_id or "unknown"
        if not candidate_id or candidate_id in seen_ids:
            errors.append(f"sales_capture_candidate_id_duplicate_or_missing:{label}")
        seen_ids.add(candidate_id)
        if attempt.get("content_valid") is not True or attempt.get("pdf_created") is not True:
            diagnostic_dir = confined_path(
                run_dir, attempt.get("output_dir"), run_dir / "capture-diagnostics" / "sales-after-login",
            )
            if (
                detail_capture_required
                or attempt.get("diagnostic_only") is not True
                or diagnostic_dir is None
            ):
                errors.append(f"sales_capture_attempt_not_accepted:{label}")
            else:
                stats["diagnostic_detail_skips"] += 1
            continue
        output_dir = confined_path(run_dir, attempt.get("output_dir"), capture_root)
        if output_dir is None or output_dir == capture_root.resolve(strict=False):
            errors.append(f"sales_capture_path_outside_capture_tree:{label}")
            continue
        metadata = read_json(output_dir / "metadata.json")
        if not isinstance(metadata, dict):
            errors.append(f"sales_capture_metadata_missing_or_invalid:{label}")
            continue
        if not (
            metadata.get("schema_version") == "2.0"
            and metadata.get("record_type") == "target_page_capture"
            and metadata.get("source_id") == candidate_id
            and metadata.get("requested_url") == attempt.get("url")
            and metadata.get("content_valid") is True
            and metadata.get("page_state") == attempt.get("page_state")
        ):
            errors.append(f"sales_capture_metadata_identity_mismatch:{label}")
        visual = metadata.get("visual_capture") or {}
        if not (
            visual.get("strategy") == "viewport_tile_stitch_v1"
            and visual.get("acceptable") is True
            and visual.get("output_created") is True
            and metadata.get("image_load_complete") is True
        ):
            errors.append(f"sales_capture_visual_capture_invalid:{label}")
        required = {
            "raw_html": "response.html", "body_text": "body-text.txt",
            "links_index": "page-links.json", "rendered_dom": "rendered-dom.html",
            "fullpage": "fullpage.png", "mhtml": "page.mhtml", "pdf": "page.pdf",
        }
        artifacts = metadata.get("artifacts") or {}
        for key, filename in required.items():
            target = output_dir / filename
            valid_file = nonempty_file(target) if key != "fullpage" else valid_image(target)
            if not valid_file:
                errors.append(f"sales_capture_artifact_missing_or_invalid:{label}:{filename}")
            elif not artifact_record_matches(output_dir, artifacts.get(key), target):
                # Capture metadata paths are relative to output_dir, unlike the
                # assisted-search records which are relative to RUN_DIR.
                errors.append(f"sales_capture_artifact_record_mismatch:{label}:{filename}")
        pdf_ok, pdf_reason, _ = validate_pdf(output_dir / "page.pdf")
        if not pdf_ok:
            errors.append(f"sales_capture_pdf_invalid:{label}:{pdf_reason}")
        else:
            stats["validated_pdfs"] += 1
        stats["validated_attempts"] += 1
        accepted_urls.add(str(attempt.get("url") or ""))
    return errors, stats, accepted_urls


def validate_related_artifacts(
    run_dir: Path, config: dict, manifest: dict, public_sources: dict,
    assisted_sources: dict, accepted_capture_urls: set[str],
) -> tuple[list[str], dict]:
    errors: list[str] = []
    stats = {"validated_items": 0, "validated_item_pdfs": 0, "compiled_pdf_pages": 0}
    trademark = config.get("trademark") or {}
    cover = manifest.get("cover") or {}
    if manifest.get("schema_version") != "1.0" or manifest.get("record_type") != "related_web_result_manifest":
        errors.append("related_manifest_schema_invalid")
    if any((
        cover.get("trademark_name") != trademark.get("name"),
        cover.get("registration_number") != trademark.get("registration_number"),
        cover.get("owner") != trademark.get("owner"),
    )):
        errors.append("related_manifest_trademark_identity_mismatch")
    items = [item for item in manifest.get("items") or [] if isinstance(item, dict)]
    if len(items) != len(manifest.get("items") or []):
        errors.append("related_manifest_item_schema_invalid")
    coverage = manifest.get("coverage") or {}
    if int(coverage.get("included_result_page_count") or -1) != len(items):
        errors.append("related_manifest_item_count_mismatch")
    if not items and (public_sources or assisted_sources or accepted_capture_urls):
        errors.append("related_manifest_missing_delivery_items")
    item_root = run_dir / "related-result-pages"
    seen_ids: set[str] = set()
    covered_sources: set[tuple[str, str, int]] = set()
    for item in items:
        item_id = str(item.get("id") or "")
        label = item_id or "unknown"
        if not item_id or item_id in seen_ids:
            errors.append(f"related_manifest_item_id_duplicate_or_missing:{label}")
        seen_ids.add(item_id)
        if item.get("content_valid") is not True or item.get("page_state") != "related_lead":
            errors.append(f"related_manifest_item_state_invalid:{label}")
        pdf_path = confined_path(run_dir, item.get("pdf"), item_root)
        if pdf_path is None or pdf_path == item_root.resolve(strict=False):
            errors.append(f"related_manifest_pdf_outside_result_tree:{label}")
            continue
        pdf_ok, pdf_reason, _ = validate_pdf(pdf_path)
        if not pdf_ok:
            errors.append(f"related_manifest_item_pdf_invalid:{label}:{pdf_reason}")
        else:
            stats["validated_item_pdfs"] += 1
        query_id = str(item.get("query_id") or "")
        platform = str(item.get("platform") or "")
        page_index = int(item.get("result_page_index") or 1)
        source_url = str(item.get("source_url") or item.get("display_url") or "")
        source = public_sources.get((query_id, platform, page_index)) or assisted_sources.get((query_id, platform, page_index))
        if source is not None:
            if source_url != source.get("url"):
                errors.append(f"related_manifest_source_url_mismatch:{label}")
            covered_sources.add((query_id, platform, page_index))
        elif source_url not in accepted_capture_urls:
            errors.append(f"related_manifest_source_not_accepted:{label}")
        stats["validated_items"] += 1

    required_sources = {
        key for key, source in {**public_sources, **assisted_sources}.items()
        if int(source.get("result_count") or 0) > 0
    }
    if not required_sources.issubset(covered_sources):
        errors.append("related_manifest_delivery_source_coverage_incomplete")
    expected_query_count = len({(key[0], key[1]) for key in covered_sources if key[0]})
    if int(coverage.get("included_query_count") or -1) != expected_query_count:
        errors.append("related_manifest_query_count_mismatch")

    compiled = run_dir / "related-web-results.pdf"
    compiled_ok, compiled_reason, page_count = validate_pdf(compiled)
    stats["compiled_pdf_pages"] = page_count
    if not compiled_ok:
        errors.append(f"related_results_pdf_invalid:{compiled_reason}")
    elif page_count < len(items):
        errors.append("related_results_pdf_page_count_too_small")
    return errors, stats


def workflow_mode(run_dir: Path, config: dict) -> str:
    orchestration = config.get("cherrystudio_orchestration") or {}
    declared = str(orchestration.get("workflow_mode") or "").strip()
    if declared:
        return declared
    if (run_dir / "discovery" / "sales-workflow-state.json").is_file():
        return "free_assisted_browser"
    return str(config.get("execution_profile") or "quick")


def scan_bad_delivery_pages(run_dir: Path) -> list[str]:
    findings: list[str] = []
    roots = ("candidate-pages", "source-pages", "visual-match-pages", "related-result-pages")
    for root_name in roots:
        root = run_dir / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("metadata.json"):
            metadata = read_json(path, {}) or {}
            status = metadata.get("http_status")
            final_url = str(metadata.get("final_url") or metadata.get("display_url") or "")
            title = str(metadata.get("title") or "")
            invalid_status = isinstance(status, int) and status >= 400
            invalid_url = bool(re.search(r"/(?:400|401|403|404|405|429|500|502|503)(?:\.html)?(?:[/?#]|$)", final_url, re.I))
            invalid_title = bool(BAD_PAGE_TITLE.search(title))
            if invalid_status or invalid_url or invalid_title:
                findings.append(path.relative_to(run_dir).as_posix())
    return sorted(set(findings))


def audit_quick_or_forensic(run_dir: Path, mode: str, errors: list[str], checks: dict) -> None:
    validation = read_json(run_dir / "validation.json", {}) or {}
    checks["validation_ok"] = validation.get("ok") is True
    add(errors, checks["validation_ok"], "final_validation_not_true")
    binder_ok, binder_reason, binder_pages = validate_pdf(run_dir / "evidence-binder.pdf")
    checks["evidence_binder_present"] = binder_ok
    checks["evidence_binder_validation"] = {"ok": binder_ok, "reason": binder_reason, "page_count": binder_pages}
    add(errors, binder_ok, f"evidence_binder_invalid:{binder_reason}")

    if mode == "quick":
        summary = read_json(run_dir / "capture" / "quick-capture-summary.json", {}) or {}
        coverage = summary.get("summary") or {}
        checks["quick_capture_summary_present"] = bool(summary)
        checks["quick_minimum_coverage_met"] = coverage.get("minimum_coverage_met") is True
        checks["quick_target_met"] = coverage.get("target_met") is True
        add(errors, checks["quick_capture_summary_present"], "quick_capture_summary_missing")
        add(errors, checks["quick_minimum_coverage_met"], "quick_minimum_coverage_not_met")
        add(errors, checks["quick_target_met"], "quick_target_not_met")


def audit_assisted(run_dir: Path, config: dict, errors: list[str], checks: dict) -> None:
    trademark = config.get("trademark") or {}
    goods = [str(value).strip() for value in trademark.get("goods_services") or [] if str(value).strip()]
    goods_count = len(dict.fromkeys(goods))
    expected_sales_tasks = len(SALES_PLATFORMS) * (goods_count + 1)
    expected_sales_plan = len(SALES_PLATFORMS) * goods_count
    expected_public_tasks = len(PUBLIC_SEARCH_PROVIDERS) * (goods_count + 1)

    orchestration = config.get("cherrystudio_orchestration") or {}
    checks["cache_policy_fresh"] = orchestration.get("cache_policy") == "new_run_no_reuse"
    add(errors, checks["cache_policy_fresh"], "fresh_run_policy_missing")
    period_ok, period_check = validate_period(config)
    checks["investigation_period"] = {"ok": period_ok, **period_check}
    add(errors, period_ok, "investigation_period_invalid_or_unattributed")

    manual = read_json(run_dir / "discovery" / "manual-capture-queue.json", {}) or {}
    sales_plan = read_json(run_dir / "discovery" / "query-plan.json", {}) or {}
    public_plan = read_json(run_dir / "discovery" / "public-search-plan.json", {}) or {}
    state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
    workflow = read_json(run_dir / "visual-sales-workflow-summary.json", {}) or {}
    public = read_json(run_dir / "discovery" / "public-search-matrix.json", {}) or {}
    assisted = read_json(run_dir / "discovery" / "assisted-sales-results.json", {}) or {}
    capture = read_json(run_dir / "capture" / "sales-after-login" / "capture-summary.json", {}) or {}

    attachment_errors, attachment_check = validate_public_browser_attachment(config, state, public)
    errors.extend(attachment_errors)
    checks["public_browser_attachment"] = {"ok": not attachment_errors, **attachment_check}

    selected_browser = str(orchestration.get("selected_browser") or "").strip().casefold()
    state_browser = str(state.get("default_browser") or "").strip().casefold()
    workflow_browser = str(workflow.get("browser") or "").strip().casefold()
    assisted_browser = str(assisted.get("browser_product") or "").strip().casefold()
    capture_browser = str(capture.get("browser") or "").strip().casefold()
    browser_values = [selected_browser, state_browser, workflow_browser, assisted_browser, capture_browser]
    checks["browser_selection_policy"] = orchestration.get("browser_selection_policy")
    checks["browser_products"] = browser_values
    checks["browser_product_consistent"] = (
        selected_browser in {"edge", "chrome"}
        and all(value == selected_browser for value in browser_values)
    )
    add(errors, orchestration.get("browser_selection_policy") == "edge_then_chrome", "browser_selection_policy_invalid")
    add(errors, checks["browser_product_consistent"], "browser_product_state_mismatch")

    executable_values = [
        orchestration.get("browser_executable"), state.get("browser_executable"),
        workflow.get("browser_executable"), assisted.get("browser_executable"),
        capture.get("browser_executable"),
    ]
    normalized_executables = [str(value or "").strip().casefold() for value in executable_values]
    checks["browser_executable_consistent"] = bool(normalized_executables[0]) and len(set(normalized_executables)) == 1
    add(errors, checks["browser_executable_consistent"], "browser_executable_state_mismatch")

    user_data_values = [state.get("browser_user_data"), workflow.get("browser_user_data"), capture.get("browser_user_data")]
    normalized_user_data = [str(value or "").strip().casefold() for value in user_data_values]
    checks["browser_user_data_consistent"] = bool(normalized_user_data[0]) and len(set(normalized_user_data)) == 1
    add(errors, checks["browser_user_data_consistent"], "browser_user_data_state_mismatch")

    cdp_product = str(state.get("cdp_browser") or "").casefold()
    checks["cdp_browser_matches_selection"] = (
        (selected_browser == "edge" and ("edg/" in cdp_product or "microsoft edge" in cdp_product))
        or (selected_browser == "chrome" and "chrome/" in cdp_product and "edg/" not in cdp_product)
    )
    add(errors, checks["cdp_browser_matches_selection"], "cdp_browser_product_mismatch")

    checks.update({
        "manual_task_count": int(manual.get("task_count") or 0),
        "sales_plan_query_count": int(sales_plan.get("query_count") or 0),
        "public_plan_task_count": int(public_plan.get("task_count") or 0),
        "workflow_capture_status": workflow.get("capture_status"),
        "workflow_state_phase": state.get("phase"),
        "public_actual_query_count": int(public.get("query_count") or 0),
        "sales_actual_query_count": int(assisted.get("query_count") or 0),
        "capture_summary_status": capture.get("status"),
    })
    add(errors, manual.get("task_count") == expected_sales_tasks, "assisted_sales_task_plan_incomplete")
    add(errors, sales_plan.get("query_count") == expected_sales_plan, "assisted_sales_query_plan_incomplete")
    add(errors, public_plan.get("task_count") == expected_public_tasks, "public_search_task_plan_incomplete")
    add(errors, state.get("phase") == "capture_complete", "assisted_workflow_not_capture_complete")
    add(errors, workflow.get("record_type") == "visual_first_sales_workflow", "visual_sales_workflow_summary_missing")
    add(errors, workflow.get("capture_status") == "complete", "visual_sales_capture_not_complete")
    add(errors, capture.get("status") == "complete", "sales_detail_capture_summary_not_complete")

    provider_runs = public.get("provider_runs") or []
    delivered_public = sum(1 for item in provider_runs if item.get("state") in DELIVERY_SEARCH_STATES)
    checks["public_delivery_task_count"] = delivered_public
    add(errors, public.get("query_count") == expected_public_tasks, "public_search_matrix_wrong_size")
    add(errors, delivered_public == expected_public_tasks, "public_search_delivery_coverage_incomplete")

    sales_runs = assisted.get("platform_runs") or []
    delivered_sales = sum(1 for item in sales_runs if item.get("delivery_eligible") is True)
    checks["sales_delivery_task_count"] = delivered_sales
    add(errors, assisted.get("query_count") == expected_sales_tasks, "assisted_sales_matrix_wrong_size")
    add(errors, delivered_sales == expected_sales_tasks, "assisted_sales_delivery_coverage_incomplete")

    public_errors, public_stats, public_sources = validate_public_artifacts(run_dir, public_plan, public)
    assisted_errors, assisted_stats, assisted_sources = validate_assisted_artifacts(run_dir, manual, assisted)
    capture_errors, capture_stats, accepted_capture_urls = validate_capture_artifacts(
        run_dir, config, assisted, capture,
    )
    errors.extend(public_errors)
    errors.extend(assisted_errors)
    errors.extend(capture_errors)
    checks["public_artifact_validation"] = {"ok": not public_errors, **public_stats}
    checks["assisted_artifact_validation"] = {"ok": not assisted_errors, **assisted_stats}
    checks["sales_capture_artifact_validation"] = {"ok": not capture_errors, **capture_stats}

    related_manifest = read_json(run_dir / "related-web-results.manifest.json", {}) or {}
    related_coverage = related_manifest.get("coverage") or {}
    related_errors, related_stats = validate_related_artifacts(
        run_dir, config, related_manifest, public_sources, assisted_sources, accepted_capture_urls,
    )
    errors.extend(related_errors)
    checks["related_artifact_validation"] = {"ok": not related_errors, **related_stats}
    checks["related_pdf_present"] = not any(value.startswith("related_results_pdf_invalid:") for value in related_errors)
    checks["related_failed_pages_included"] = related_coverage.get("failed_or_blocked_pages_included")
    add(errors, related_coverage.get("failed_or_blocked_pages_included") == 0, "related_results_contains_blocked_pages")

    bundle_errors, bundle_stats = validate_all_detected_delivery_bundle(run_dir, config)
    checks["all_detected_delivery_bundle"] = bundle_stats
    errors.extend(bundle_errors)


def audit_run(run_dir: Path, *, write_files: bool = True) -> dict:
    run_dir = Path(run_dir).resolve()
    config = read_json(run_dir / "run-config.json", {}) or {}
    errors: list[str] = []
    checks: dict = {}
    checks["runtime_dependencies"] = {
        "pymupdf_available": fitz is not None,
        "pillow_available": Image is not None,
    }
    checks["runtime_dependencies_ok"] = fitz is not None and Image is not None
    add(errors, fitz is not None, "runtime_dependency_missing:pymupdf")
    add(errors, Image is not None, "runtime_dependency_missing:pillow")
    add(errors, bool(config), "run_config_missing")
    mode = workflow_mode(run_dir, config)

    state = read_json(run_dir / "discovery" / "sales-workflow-state.json", {}) or {}
    terminal_artifacts_exist = any((run_dir / name).is_file() for name in (
        "visual-sales-workflow-summary.json", "validation.json", "evidence-binder.pdf",
    ))
    awaiting_candidate = (
        mode == "free_assisted_browser"
        and state.get("phase") == "awaiting_manual_login"
        and not terminal_artifacts_exist
    )

    qcc = validate_qcc_reference(run_dir)
    checks["qcc_reference"] = qcc
    if not qcc.get("ok"):
        errors.extend(f"qcc:{item}" for item in qcc.get("errors") or ["invalid"])

    bad_pages = scan_bad_delivery_pages(run_dir)
    checks["invalid_delivery_pages"] = bad_pages
    if bad_pages:
        errors.append("invalid_http_or_error_page_in_delivery_tree")

    allowed_root_markdown = set(ALLOWED_ROOT_MARKDOWN)
    if mode in {"quick", "forensic"}:
        # finalize-run.py deterministically creates report.md together with
        # validation.json/evidence-binder.pdf; audit_quick_or_forensic checks both.
        allowed_root_markdown.add("report.md")
    forbidden_reports = sorted(
        path.name for path in run_dir.iterdir()
        if path.is_file() and path.suffix.casefold() == ".md"
        and (
            path.name.casefold() in FORBIDDEN_REPORT_NAMES
            or "调查报告" in path.name
            or path.name.casefold() not in allowed_root_markdown
        )
    ) if run_dir.is_dir() else []
    checks["unauthorized_narrative_reports"] = forbidden_reports
    if forbidden_reports:
        errors.append("unauthorized_freeform_narrative_report_present")

    if mode == "free_assisted_browser":
        audit_assisted(run_dir, config, errors, checks)
    elif mode in {"quick", "forensic"}:
        audit_quick_or_forensic(run_dir, mode, errors, checks)
    else:
        errors.append("unsupported_workflow_mode")

    errors = list(dict.fromkeys(errors))
    qcc_record_exists = (run_dir / "reference" / "qcc-reference.json").is_file()
    try:
        cdp_url = urlsplit(str(state.get("cdp_endpoint") or ""))
        cdp_loopback = cdp_url.scheme in {"http", "https"} and cdp_url.hostname in {"127.0.0.1", "localhost", "::1"}
    except ValueError:
        cdp_loopback = False
    checks["awaiting_login_browser_started"] = state.get("login_browser_started") is True
    checks["awaiting_login_cdp_loopback"] = cdp_loopback and state.get("cdp_loopback_only") is True
    awaiting = bool(
        awaiting_candidate
        and checks.get("cache_policy_fresh") is True
        and checks.get("manual_task_count") == len(SALES_PLATFORMS) * (
            len(dict.fromkeys((config.get("trademark") or {}).get("goods_services") or [])) + 1
        )
        and checks.get("public_plan_task_count") == len(PUBLIC_SEARCH_PROVIDERS) * (
            len(dict.fromkeys((config.get("trademark") or {}).get("goods_services") or [])) + 1
        )
        and not bad_pages
        and not forbidden_reports
        and (not qcc_record_exists or qcc.get("ok") is True)
        and checks["awaiting_login_browser_started"]
        and checks["awaiting_login_cdp_loopback"]
        and checks.get("cdp_browser_matches_selection") is True
        and checks["runtime_dependencies_ok"]
    )
    status = "awaiting_manual_login" if awaiting else ("completed" if not errors else "incomplete")
    now = datetime.now(timezone.utc).isoformat()
    audit = {
        "schema_version": "1.0",
        "record_type": "cherrystudio_terminal_audit",
        "audited_at": now,
        "run_id": config.get("run_id"),
        "run_dir": str(run_dir),
        "workflow_mode": mode,
        "status": status,
        "validation": {"ok": status == "completed", "errors": errors},
        "checks": checks,
        "claim_policy": {
            "narrative_report_authorized": False,
            "legal_use_conclusion_authorized": False,
            "allowed_status_claim": "流程校验通过" if status == "completed" else status,
            "forbidden_claims": ["调查完成", "未发现实际使用", "不存在使用"],
        },
    }
    if write_files:
        write_json(run_dir / "cherrystudio-terminal-audit.json", audit)
        receipt_path = run_dir / "cherrystudio-completion-receipt.json"
        # audit_assisted already performed the expensive physical PDF/attachment
        # verification in this same audit invocation.  Re-running it here used to
        # render and hash the complete evidence bundle a second time before a
        # receipt could be issued.  Reuse that verified result atomically instead.
        receipt_bundle = checks.get("all_detected_delivery_bundle") or {}
        receipt_bundle_errors = (
            [] if status == "completed" and receipt_bundle.get("ok") is True
            else ["terminal_audit_incomplete_or_bundle_invalid"]
        )
        bundle_ready = status == "completed" and not receipt_bundle_errors
        if bundle_ready:
            receipt = {
                "schema_version": "1.0",
                "record_type": "cherrystudio_completion_receipt",
                "issued_at": now,
                "run_id": config.get("run_id"),
                "workflow_mode": mode,
                "status": "completed",
                "validation": {"ok": True},
                "terminal_audit": "cherrystudio-terminal-audit.json",
                "narrative_report_authorized": False,
                "legal_use_conclusion_authorized": False,
                "deliverable": {
                    "pdf": "all-detected-html-pages.pdf",
                    "pdf_sha256": receipt_bundle.get("pdf_sha256"),
                    "manifest": "all-detected-html-pages.manifest.json",
                    "manifest_sha256": receipt_bundle.get("manifest_sha256"),
                    "page_count": int(receipt_bundle.get("page_count") or 0),
                    "embedded_html_count": int(receipt_bundle.get("embedded_html_count") or 0),
                    "uri_link_count": int(receipt_bundle.get("uri_link_count") or 0),
                    "goto_link_count": int(receipt_bundle.get("goto_link_count") or 0),
                    "all_pages_have_timestamp_footer": True,
                    "all_pages_have_source_url_footer": True,
                    "all_source_urls_clickable": True,
                },
            }
            write_json(receipt_path, receipt)
        elif receipt_path.exists():
            receipt_path.unlink()
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit a CherryStudio trademark run without writing a narrative report")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    audit = audit_run(Path(args.run_dir))
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    raise SystemExit(0 if audit["status"] == "completed" else 3)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
