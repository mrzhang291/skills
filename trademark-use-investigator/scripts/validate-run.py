#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import fitz

from fs_safety import TRANSACTION_DIR_NAME, assert_safe_tree, audit_run_tree, is_reparse_point
from provenance_utils import file_sha256, provenance_trace, result_traces, validate_provider
from url_utils import (
    hostname, is_safe_file_id, is_search_result_url, normalize_url, require_safe_file_id,
    site_key, target_id,
)
from visual_consensus import (
    build_consensus,
    normalized_actor,
    validate_review_set,
)


REQUIRED_SOURCE_ARTIFACTS = {
    "singlefile_html", "singlefile_raw_html", "rendered_dom", "mhtml", "fullpage", "pdf",
    "body_text", "images_index", "links_index", "offline_validation",
    "mhtml_validation", "mhtml_offline_screenshot", "archive_visual_comparison",
}
BLOCKED_STATES = {"captcha", "login_required", "access_denied", "empty_shell", "empty_results", "search_result_page"}
SKIPPED_CATEGORY_ORDER = {
    "captcha": 0,
    "login": 1,
    "access_denied": 2,
    "timed_out": 3,
    "unexpected": 4,
    "failed": 5,
    "content_valid_not_selected": 6,
    "not_probed_budget": 7,
}


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pdf_page_count(path: Path) -> int:
    document = fitz.open(path)
    try:
        if document.needs_pass or document.page_count < 1:
            raise ValueError(f"PDF is encrypted or empty: {path}")
        return document.page_count
    finally:
        document.close()


def canonical_url(record: dict, *, frontier_trace: bool = False) -> str | None:
    if frontier_trace:
        raw = record.get("normalized_url") or record.get("requested_url") or ""
    else:
        raw = record.get("final_url") or record.get("normalized_url") or record.get("requested_url") or ""
    return normalize_url(raw)


def _skipped_failure_category(raw_status: object) -> str:
    value = str(raw_status or "failed").strip().casefold().replace("-", "_").replace(" ", "_")
    if "captcha" in value or "verification" in value:
        return "captcha"
    if "login" in value or "sign_in" in value or "signin" in value:
        return "login"
    if value in {"access_denied", "forbidden", "unauthorized", "blocked"} or "access_denied" in value:
        return "access_denied"
    if "timeout" in value or "timed_out" in value:
        return "timed_out"
    if value in {
        "unexpected", "unexpected_content", "search_result_page", "empty_shell",
        "empty_results", "manual_visual_review",
    }:
        return "unexpected"
    return "failed"


def _skipped_reason(stage: str, raw_status: object, record: dict) -> str:
    for key in ("error", "reason", "message"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    errors = record.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict) and str(item.get("message") or "").strip():
                return str(item["message"]).strip()[:500]
            if isinstance(item, str) and item.strip():
                return item.strip()[:500]
    label = str(raw_status or "failed").strip() or "failed"
    if stage == "full_capture":
        return f"Full capture ended with status: {label}"
    if stage == "probe":
        return f"Quick probe ended with status: {label}"
    return "URL was not probed within the configured investigation budget"


def build_skipped_links(
    frontier: dict,
    probe_attempts: list[dict],
    capture_attempts: list[dict],
    formal_sources: list[dict],
) -> dict:
    """Independently rebuild the deterministic omitted-URL deliverable."""
    formal_urls: set[str] = set()
    for source in formal_sources:
        for key in ("requested_url", "normalized_url", "final_url", "final_normalized_url"):
            normalized = normalize_url(source.get(key) or "")
            if normalized:
                formal_urls.add(normalized)

    records: dict[str, dict] = {}

    def ensure(url_value: object, *, frontier_id: object = None) -> tuple[str | None, dict | None]:
        normalized = normalize_url(str(url_value or ""))
        if not normalized or is_search_result_url(normalized):
            return None, None
        record = records.setdefault(normalized, {
            "url": normalized,
            "site_key": site_key(normalized),
            "frontier_id": str(frontier_id or "") or None,
            "probe": None,
            "capture": None,
        })
        if not record.get("frontier_id") and frontier_id:
            record["frontier_id"] = str(frontier_id)
        return normalized, record

    for item in (frontier.get("items") or []):
        if isinstance(item, dict):
            ensure(item.get("normalized_url") or item.get("url"), frontier_id=item.get("target_id"))
    for item in probe_attempts:
        if not isinstance(item, dict):
            continue
        _, record = ensure(
            item.get("url") or item.get("requested_normalized_url"),
            frontier_id=item.get("frontier_id"),
        )
        if record is not None:
            record["probe"] = item
    for item in capture_attempts:
        if not isinstance(item, dict):
            continue
        _, record = ensure(
            item.get("url") or item.get("requested_normalized_url"),
            frontier_id=item.get("frontier_id"),
        )
        if record is not None:
            record["capture"] = item

    items: list[dict] = []
    for url, record in records.items():
        if url in formal_urls:
            continue
        capture = record.get("capture")
        probe = record.get("probe")
        if isinstance(capture, dict):
            raw_status = capture.get("status") or capture.get("page_state") or "failed"
            if capture.get("status") == "accepted_candidate" and capture.get("content_valid") is True:
                category = "content_valid_not_selected"
                status = "content_valid_not_selected"
                reason = "A valid full-page archive was retained but not selected for the formal PDF"
            else:
                category = _skipped_failure_category(raw_status)
                status = "full_capture_failed"
                reason = _skipped_reason("full_capture", raw_status, capture)
            stage = "full_capture"
        elif isinstance(probe, dict):
            raw_status = probe.get("status") or probe.get("page_state") or "failed"
            if probe.get("content_valid") is True and probe.get("status") == "content_valid":
                category = "content_valid_not_selected"
                status = "content_valid_not_selected"
                reason = "The lightweight probe was valid but this URL was not selected for full capture"
            else:
                category = _skipped_failure_category(raw_status)
                status = category
                reason = _skipped_reason("probe", raw_status, probe)
            stage = "probe"
        else:
            raw_status = "not_probed_budget"
            category = "not_probed_budget"
            status = "not_probed_budget"
            reason = _skipped_reason("discovery", raw_status, {})
            stage = "discovery"
        items.append({
            "url": url,
            "site_key": record["site_key"],
            "frontier_id": record.get("frontier_id"),
            "stage": stage,
            "category": category,
            "status": status,
            "source_status": str(raw_status),
            "reason": reason,
        })

    items.sort(key=lambda item: (
        SKIPPED_CATEGORY_ORDER.get(item["category"], 999),
        item["site_key"],
        item["url"],
    ))
    category_counts = {
        category: sum(item["category"] == category for item in items)
        for category in SKIPPED_CATEGORY_ORDER
        if any(item["category"] == category for item in items)
    }
    status_counts = {
        status: sum(item["status"] == status for item in items)
        for status in sorted({item["status"] for item in items})
    }
    return {
        "schema_version": "1.0",
        "count": len(items),
        "category_counts": category_counts,
        "status_counts": status_counts,
        "items": items,
    }


def capture_invocation_count(attempts: list[dict]) -> int:
    invocation_ids: set[str] = set()
    unkeyed_attempts = 0
    for item in attempts:
        history = item.get("attempt_history") if isinstance(item.get("attempt_history"), list) else []
        history_ids = {
            str(entry.get("invocation_id") or entry.get("attempt_id"))
            for entry in history if isinstance(entry, dict) and (entry.get("invocation_id") or entry.get("attempt_id"))
        }
        invocation_ids.update(history_ids)
        declared_count = max(1, int(item.get("attempt_count") or 1))
        if history_ids:
            unkeyed_attempts += max(0, declared_count - len(history_ids))
            continue
        invocation_id = item.get("invocation_id") or item.get("attempt_id")
        if invocation_id:
            invocation_ids.add(str(invocation_id))
            unkeyed_attempts += max(0, declared_count - 1)
        else:
            unkeyed_attempts += declared_count
    return len(invocation_ids) + unkeyed_attempts


def negative_visual_finding(record: dict) -> bool:
    trademark_class = record.get("trademark_class") or (record.get("trademark_match") or {}).get("classification")
    return bool(
        record.get("trademark_visible") == "no"
        or trademark_class in {"different", "not_match", "unreadable"}
        or record.get("commercial_use") in {"no", "non_commercial_reference"}
        or record.get("goods_match") == "no"
        or record.get("owner_attribution") in {"third_party", "unrelated"}
    )


def positive_confirmed(consensus: dict, expected_owner: str) -> bool:
    return bool(
        consensus.get("status") == "confirmed"
        and consensus.get("positive_evidence_eligible") is True
        and consensus.get("confidence") == "high"
        and consensus.get("trademark_visible") == "yes"
        and consensus.get("trademark_class") in {"exact", "near"}
        and consensus.get("commercial_use") == "yes"
        and consensus.get("goods_match") == "yes"
        and consensus.get("owner_attribution") == "owner"
        and consensus.get("actor_owner_match") is True
        and bool(expected_owner)
        and normalized_actor(consensus.get("page_actor")) == normalized_actor(expected_owner)
    )


def verify_visual_consensus(
    run_dir: Path,
    source_id: str,
    origin: str,
    stored: dict,
    candidate_metadata: dict,
    config: dict,
    errors: list[str],
) -> tuple[dict, list[dict]]:
    review_dir = run_dir / "visual-reviews" / origin
    consensus_path = review_dir / "consensus.json"
    if not consensus_path.is_file():
        errors.append(f"Positive evidence source has no consensus.json: {source_id}")
        return {}, []
    disk = read_json(consensus_path, {}) or {}
    reviews = []
    for path in sorted(review_dir.glob("*.json")):
        if path.name == "consensus.json":
            continue
        review = read_json(path, {}) or {}
        if review.get("review_id") != path.stem:
            errors.append(f"Visual review filename does not match review_id for {source_id}: {path.name}")
        reviews.append(review)
    if not reviews:
        errors.append(f"Visual consensus has no review records: {source_id}")
    review_errors = validate_review_set(reviews, origin, run_created_at=config.get("created_at"))
    if review_errors:
        errors.append(f"Visual review set is incomplete/invalid for {source_id}: {', '.join(review_errors)}")
    expected_owner = str((config.get("trademark") or {}).get("owner") or "").strip()
    rebuilt = build_consensus(
        reviews,
        origin,
        updated_at=disk.get("updated_at"),
        expected_owner=expected_owner,
        run_created_at=config.get("created_at"),
    )
    if rebuilt.get("status") in {"invalid_review_set", "invalid_role_assignment"}:
        errors.append(f"Visual review set has an invalid status for {source_id}: {rebuilt.get('status')}")
    if disk != rebuilt:
        errors.append(f"Visual consensus is stale, incomplete or inconsistent for {source_id}")
    if stored != disk:
        errors.append(f"Promoted visual consensus no longer matches its review record: {source_id}")

    candidate_dir = (run_dir / "candidate-pages" / origin).resolve()
    reference_root = (run_dir / "reference").resolve()
    expected_fullpage = str(((candidate_metadata.get("artifacts") or {}).get("fullpage") or {}).get("path") or "")
    for review in reviews:
        inputs = review.get("inputs") if isinstance(review.get("inputs"), dict) else {}
        fullpage = inputs.get("fullpage") if isinstance(inputs.get("fullpage"), dict) else {}
        if fullpage.get("path") != expected_fullpage:
            errors.append(f"Visual review fullpage role does not equal candidate metadata for {source_id}: {review.get('review_id')}")
        role_groups = (
            (reference_root, inputs.get("reference") or [], "reference"),
            (candidate_dir, [fullpage] if fullpage else [], "fullpage"),
            (candidate_dir, inputs.get("regions") or [], "region"),
        )
        for root, artifacts, role in role_groups:
            if not artifacts:
                errors.append(f"Visual review has no {role} input for {source_id}: {review.get('review_id')}")
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    errors.append(f"Visual review {role} input is malformed for {source_id}: {review.get('review_id')}")
                    continue
                rel = artifact.get("path") or ""
                path = (root / rel).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    errors.append(f"Visual review {role} input is missing/outside scope for {source_id}: {rel}")
                elif sha256(path) != artifact.get("sha256") or path.stat().st_size != artifact.get("size_bytes"):
                    errors.append(f"Visual review {role} input hash/size mismatch for {source_id}: {rel}")
    return rebuilt, reviews


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


def jaccard(left: str, right: str) -> float:
    a = set(normalized_text(left).split())
    b = set(normalized_text(right).split())
    if not a and not b:
        return 1.0
    return len(a & b) / max(1, len(a | b))


def pdf_quality(path: Path, skip_pages: int = 0) -> dict:
    blank = []
    near_blank = []
    repeated = []
    page_stats = []
    document = fitz.open(path)
    previous_text = None
    for index, page in enumerate(document, start=1):
        text = page.get_text("text") or ""
        pix = page.get_pixmap(matrix=fitz.Matrix(0.45, 0.45), colorspace=fitz.csGRAY, alpha=False)
        samples = pix.samples
        nonwhite = sum(value < 245 for value in samples) / max(1, len(samples))
        stat = {"page": index, "text_chars": len(text.strip()), "nonwhite_ratio": round(nonwhite, 6)}
        page_stats.append(stat)
        if index > skip_pages:
            # Extractable text can be white/invisible. A virtually all-white
            # rendering is independently blank regardless of text objects.
            if nonwhite < 0.0005:
                blank.append(index)
            elif len(text.strip()) < 40 and nonwhite < 0.015:
                blank.append(index)
            elif len(text.strip()) < 100 and nonwhite < 0.03:
                near_blank.append(index)
            if previous_text and 20 <= len(text.strip()) < 500 and jaccard(previous_text, text) >= 0.95:
                repeated.append(index)
        previous_text = text
    count = document.page_count
    links = sum(len(page.get_links()) for page in document)
    document.close()
    return {"page_count": count, "blank_pages": blank, "near_blank_pages": near_blank, "repeated_low_text_pages": repeated, "link_count": links, "pages": page_stats}


def verify_manifest(run_dir: Path, errors: list[str]) -> dict:
    manifest = read_json(run_dir / "manifest.json", {}) or {}
    files = manifest.get("files") or []
    for record in files:
        rel = record.get("path") or ""
        path = (run_dir / rel).resolve()
        if not path.is_relative_to(run_dir) or not path.is_file():
            errors.append(f"Manifest file is missing or outside run directory: {rel}")
            continue
        if path.stat().st_size != record.get("size_bytes") or sha256(path) != record.get("sha256"):
            errors.append(f"Manifest hash/size mismatch: {rel}")
    paths = {item.get("path") for item in files}
    if len(paths) != len(files):
        errors.append("Manifest contains duplicate path rows")
    actual_paths = {
        str(path.relative_to(run_dir)).replace("\\", "/")
        for path in run_dir.rglob("*")
        if path.is_file()
        and path.name not in {"manifest.json", "validation.json"}
        and (not path.relative_to(run_dir).parts or path.relative_to(run_dir).parts[0] != TRANSACTION_DIR_NAME)
    }
    if paths != actual_paths:
        errors.append("Manifest file set does not exactly match RUN_DIR (excluding validation and active transaction)")
    for required in (
        "evidence-binder.pdf", "evidence-binder.build.json", "results.json",
        "coverage-matrix.json", "capture-order.json", "skipped-links.json",
    ):
        if required not in paths:
            errors.append(f"Manifest does not include {required}")
    if any(str(path).startswith(TRANSACTION_DIR_NAME + "/") for path in paths):
        errors.append("Manifest contains finalization transaction residue")
    if any(re.search(r"(^|/)(browser-profile|\.private)(/|$)|browser-state|storage-state|cookies\.json", str(path), re.I) for path in paths):
        errors.append("Manifest contains browser state, cookies or a private profile")
    return manifest


def verify_skipped_links(
    run_dir: Path,
    expected: dict,
    results: dict,
    order: dict,
    errors: list[str],
) -> dict:
    path = run_dir / "skipped-links.json"
    actual = read_json(path, {}) or {}
    if actual != expected:
        errors.append("skipped-links.json does not match the independently rebuilt URL list")
    if results.get("skipped_link_count") != expected.get("count"):
        errors.append("results skipped_link_count does not match skipped-links.json")
    deliverables = results.get("deliverables") or {}
    if deliverables.get("skipped_links") != "skipped-links.json":
        errors.append("results deliverables.skipped_links is missing or invalid")
    if path.is_file() and deliverables.get("skipped_links_sha256") != sha256(path):
        errors.append("results skipped_links_sha256 does not match skipped-links.json")
    report_path = run_dir / "report.md"
    report = report_path.read_text(encoding="utf-8", errors="replace") if report_path.is_file() else ""
    if "## 未归档链接" not in report or f"共 {expected.get('count')} 条" not in report:
        errors.append("report.md lacks the complete skipped-link count/section")
    skipped_urls = {
        normalize_url(item.get("url") or "")
        for item in (actual.get("items") or []) if isinstance(item, dict)
    }
    order_urls = {
        normalize_url(item.get("source_url") or item.get("display_url") or "")
        for item in (order.get("items") or []) if isinstance(item, dict)
    }
    overlap = sorted((skipped_urls & order_urls) - {None})
    if overlap:
        errors.append(f"Skipped/failed URLs entered the evidence PDF capture order: {overlap}")
    return actual


def validate_sensitive_inputs(record: dict, label: str, errors: list[str]) -> None:
    values = record.get("sensitive_inputs") or []
    if not isinstance(values, list):
        errors.append(f"Sensitive-input provenance is malformed: {label}")
        return
    for item in values:
        if not isinstance(item, dict):
            errors.append(f"Sensitive-input provenance is malformed: {label}")
            continue
        if item.get("inside_run_dir") is not False:
            errors.append(f"Sensitive input was recorded inside RUN_DIR or lacks the outside-run assertion: {label}")
        if not item.get("basename") or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("absolute_path_sha256") or "")):
            errors.append(f"Sensitive input lacks basename/path hash provenance: {label}")
        if any(key in item for key in ("path", "absolute_path", "resolved_path")):
            errors.append(f"Sensitive input leaks a filesystem path into the evidence package: {label}")


def successful_provider_bases(queries: list[dict]) -> set[str]:
    """Provider instances are audit detail; they never inflate base-provider coverage."""
    return {
        str(item.get("provider"))
        for item in queries
        if item.get("state") in {"normal", "zero_results"} and item.get("provider")
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate destination-page archives, provenance and derivative PDF")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--strict", action="store_true", help="Treat insufficient investigation coverage as an error")
    parser.add_argument("--validation-nonce")
    args = parser.parse_args()

    raw_run_dir = Path(args.run_dir).expanduser().absolute()
    if is_reparse_point(raw_run_dir):
        raise ValueError("RUN_DIR cannot be a symlink, junction or reparse point")
    run_dir = raw_run_dir.resolve(strict=True)
    transaction_dir = run_dir / TRANSACTION_DIR_NAME
    allow_active_transaction = False
    if transaction_dir.exists() or is_reparse_point(transaction_dir):
        journal = read_json(transaction_dir / "journal.json", {}) or {}
        allow_active_transaction = bool(
            args.validation_nonce
            and journal.get("phase") == "mutating"
            and journal.get("validation_nonce") == args.validation_nonce
        )
        if not allow_active_transaction:
            raise ValueError("RUN_DIR contains an unfinished or nonce-mismatched finalization transaction")
    audit_run_tree(run_dir, allow_active_transaction=allow_active_transaction)
    for name in ("candidate-pages", "source-pages"):
        root = run_dir / name
        if root.exists():
            assert_safe_tree(root, run_dir, name)
    review_root = run_dir / "visual-reviews"
    if review_root.exists():
        assert_safe_tree(review_root, run_dir, "visual-reviews")
    errors: list[str] = []
    warnings: list[str] = []
    config = read_json(run_dir / "run-config.json", {}) or {}
    reference = read_json(run_dir / "reference" / "reference.json", {}) or {}
    results = read_json(run_dir / "results.json", {}) or {}
    matrix = read_json(run_dir / "coverage-matrix.json", {}) or {}
    order = read_json(run_dir / "capture-order.json", {}) or {}
    build = read_json(run_dir / "evidence-binder.build.json", {}) or {}
    queries = read_jsonl(run_dir / "discovery" / "queries.jsonl")
    discovered = read_jsonl(run_dir / "discovery" / "results.jsonl")
    frontier = read_json(run_dir / "discovery" / "url-frontier.json", {}) or {}
    probe_attempts = read_jsonl(run_dir / "probe-attempts.jsonl")
    attempts = read_jsonl(run_dir / "capture-attempts.jsonl")

    required_files = [
        "run-config.json", "reference/reference.json", "discovery/queries.jsonl",
        "discovery/results.jsonl", "discovery/url-frontier.json", "capture-attempts.jsonl",
        "coverage-matrix.json", "capture-order.json", "results.json", "report.md",
        "manifest.json", "skipped-links.json", "evidence-binder.pdf", "evidence-binder.build.json",
    ]
    for rel in required_files:
        if not (run_dir / rel).is_file():
            errors.append(f"Required output is missing: {rel}")
    if reference.get("analysis_status") != "complete":
        errors.append("Reference analysis is not complete")
    if not queries:
        errors.append("No discovery provider runs were recorded")
    if any(is_search_result_url(item.get("normalized_url") or "") for item in discovered):
        errors.append("discovery/results.jsonl contains a search-result URL instead of a destination URL")
    query_runs_by_key: dict[tuple[str, str], dict] = {}
    raw_hash_providers: dict[str, set[str]] = {}
    for query_run in queries:
        query_id = str(query_run.get("query_id") or "")
        provider = str(query_run.get("provider") or "")
        source_kind = str(query_run.get("source_kind") or "")
        if not is_safe_file_id(query_id):
            errors.append(f"Discovery run has an unsafe query ID: {query_id!r}")
        try:
            normalized_provider, expected_provider_key = validate_provider(
                provider,
                source_kind=source_kind,
                test_mode=config.get("test_mode") is True,
                provider_instance_id=query_run.get("provider_instance_id"),
            )
        except ValueError as error:
            errors.append(f"Discovery run provider is not trusted: {error}")
            continue
        if query_run.get("provider_key") != expected_provider_key:
            errors.append(f"Discovery run provider_key is inconsistent: {query_id}/{provider}")
        key = (query_id, expected_provider_key)
        if key in query_runs_by_key:
            errors.append(f"Duplicate discovery provider run: {query_id}/{expected_provider_key}")
        query_runs_by_key[key] = query_run
        raw_rel = query_run.get("raw_file")
        raw_path = (run_dir / str(raw_rel or "__missing__")).resolve()
        raw_root = (run_dir / "discovery" / "raw").resolve()
        if not raw_rel or not raw_path.is_relative_to(raw_root) or not raw_path.is_file():
            if query_run.get("state") in {"normal", "zero_results"}:
                errors.append(f"Successful discovery run lacks an in-package raw response: {query_id}/{provider}")
            continue
        actual_raw_hash = file_sha256(raw_path)
        if query_run.get("raw_sha256") != actual_raw_hash:
            errors.append(f"Discovery raw response hash mismatch: {query_id}/{provider}")
        raw_hash_providers.setdefault(actual_raw_hash, set()).add(normalized_provider)
    for raw_hash, providers_for_hash in raw_hash_providers.items():
        if len(providers_for_hash) > 1:
            errors.append(
                f"The same raw discovery payload is claimed by multiple providers: {raw_hash} {sorted(providers_for_hash)}"
            )

    max_results_per_query = max(1, int((config.get("budgets") or {}).get("max_results_per_query", 20)))
    query_result_urls: dict[str, set[str]] = {}
    seen_query_urls: set[tuple[str, str]] = set()
    expected_frontier_traces: dict[str, set[tuple]] = {}
    for item in discovered:
        query_id = str(item.get("query_id") or "")
        result_url = normalize_url(item.get("normalized_url") or "")
        if result_url:
            pair = (query_id, result_url)
            if pair in seen_query_urls:
                errors.append(f"Discovery results contain a duplicate normalized URL for {query_id}: {result_url}")
            seen_query_urls.add(pair)
            query_result_urls.setdefault(query_id, set()).add(result_url)
            if item.get("target_id") != target_id(result_url):
                errors.append(f"Discovery result target_id does not derive from URL: {item.get('discovery_id')}")
            if item.get("site_key") != site_key(result_url):
                errors.append(f"Discovery result site_key is inconsistent: {item.get('discovery_id')}")
            traces = result_traces(item)
            for trace in traces:
                trace_key = provenance_trace(trace)
                expected_frontier_traces.setdefault(result_url, set()).add(trace_key)
                run = query_runs_by_key.get((str(trace.get("query_id") or ""), str(trace.get("provider_key") or "")))
                if not run:
                    errors.append(f"Discovery result provenance has no provider run: {item.get('discovery_id')}")
                    continue
                for field in ("query", "provider", "raw_sha256"):
                    if trace.get(field) != run.get(field):
                        errors.append(
                            f"Discovery result provenance disagrees with provider run ({field}): {item.get('discovery_id')}"
                        )
                if not trace.get("discovery_id"):
                    errors.append(f"Discovery result provenance lacks discovery_id: {result_url}")
    for query_id, urls in query_result_urls.items():
        if len(urls) > max_results_per_query:
            errors.append(f"Discovery results exceed max_results_per_query={max_results_per_query} for {query_id}: {len(urls)}")

    frontier_items = frontier.get("items") or []
    frontier_by_id: dict[str, str] = {}
    frontier_urls: set[str] = set()
    for frontier_item in frontier_items:
        frontier_id = frontier_item.get("target_id")
        frontier_url = normalize_url(frontier_item.get("normalized_url") or "")
        if not frontier_id or not frontier_url:
            errors.append("URL frontier contains a missing/invalid target ID or normalized URL")
            continue
        if frontier_id in frontier_by_id:
            errors.append(f"URL frontier contains duplicate target ID: {frontier_id}")
        if frontier_url in frontier_urls:
            errors.append(f"URL frontier contains duplicate canonical URL: {frontier_url}")
        if frontier_id != target_id(frontier_url):
            errors.append(f"URL frontier target ID does not derive from its canonical URL: {frontier_id}")
        if frontier_item.get("site_key") != site_key(frontier_url):
            errors.append(f"URL frontier site_key is inconsistent: {frontier_id}")
        actual_traces = {
            provenance_trace(trace)
            for trace in (frontier_item.get("discovered_via") or [])
            if isinstance(trace, dict)
        }
        if actual_traces != expected_frontier_traces.get(frontier_url, set()):
            errors.append(f"URL frontier provenance does not exactly match discovery/results.jsonl: {frontier_id}")
        frontier_by_id[frontier_id] = frontier_url
        frontier_urls.add(frontier_url)
    if frontier_urls != set(expected_frontier_traces):
        errors.append("URL frontier URL set does not exactly match discovery/results.jsonl")
    if frontier.get("target_count") != len(frontier_items):
        errors.append("URL frontier target_count does not match its unique item rows")
    attempt_candidates: set[str] = set()
    accepted_attempt_candidates: set[str] = set()
    accepted_attempt_urls: set[str] = set()
    candidate_urls: dict[str, str] = {}
    url_candidates: dict[str, str] = {}
    site_urls: dict[str, set[str]] = {}
    seen_candidate_rows: set[str] = set()
    seen_url_rows: set[str] = set()
    global_history_ids: set[str] = set()
    started_attempts: list[str] = []
    for attempt in attempts:
        candidate_id = attempt.get("candidate_id") or ""
        if not is_safe_file_id(candidate_id):
            errors.append(f"Capture attempt has an invalid candidate ID: {candidate_id!r}")
        candidate_key = str(candidate_id).casefold()
        attempt_url = normalize_url(attempt.get("url") or "")
        if not attempt_url:
            errors.append(f"Capture attempt has an invalid normalized URL: {candidate_id!r}")
            continue
        if candidate_key in seen_candidate_rows:
            errors.append(f"capture-attempts.jsonl contains duplicate candidate rows: {candidate_id}")
        if attempt_url in seen_url_rows:
            errors.append(f"capture-attempts.jsonl contains duplicate normalized URL rows: {attempt_url}")
        seen_candidate_rows.add(candidate_key)
        seen_url_rows.add(attempt_url)
        if candidate_key in candidate_urls and candidate_urls[candidate_key] != attempt_url:
            errors.append(f"Candidate ID was reused across different normalized URLs: {candidate_id}")
        if attempt_url in url_candidates and url_candidates[attempt_url].casefold() != candidate_key:
            errors.append(f"Normalized URL was captured under multiple candidate IDs: {attempt_url}")
        candidate_urls[candidate_key] = attempt_url
        url_candidates[attempt_url] = candidate_id
        attempt_candidates.add(candidate_key)
        attempt_site = site_key(attempt_url)
        if attempt.get("site_key") != attempt_site:
            errors.append(f"Capture attempt site_key is inconsistent: {candidate_id}")
        site_urls.setdefault(attempt_site, set()).add(attempt_url)
        requested = normalize_url(attempt.get("requested_normalized_url") or "")
        if requested != attempt_url:
            errors.append(f"Capture attempt requested URL provenance is inconsistent: {candidate_id}")
        assertions = attempt.get("expected_assertions") or []
        if not isinstance(assertions, list) or not any(str(item).strip() for item in assertions):
            errors.append(f"Capture attempt has no substantive content assertion: {candidate_id}")
        source_mode = attempt.get("source_mode")
        provenance = attempt.get("source_provenance") or {}
        if provenance.get("source_mode") != source_mode:
            errors.append(f"Capture attempt source provenance mode is inconsistent: {candidate_id}")
        if source_mode == "discovered_frontier":
            frontier_id = attempt.get("frontier_id")
            if not frontier_id or frontier_by_id.get(frontier_id) != requested:
                errors.append(f"Discovered capture attempt does not trace to frontier: {candidate_id}")
            if str(attempt.get("manual_source_reason") or "").strip():
                errors.append(f"Discovered capture attempt improperly carries a manual-source reason: {candidate_id}")
            if provenance.get("frontier_id") != frontier_id:
                errors.append(f"Capture source_provenance frontier ID is inconsistent: {candidate_id}")
        elif source_mode == "manual_user_url":
            if attempt.get("frontier_id"):
                errors.append(f"Manual URL capture improperly claims a frontier ID: {candidate_id}")
            reason = attempt.get("manual_source_reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"Manual URL capture lacks a non-empty reason: {candidate_id}")
            if provenance.get("manual_source_reason") != reason:
                errors.append(f"Manual URL capture provenance is inconsistent: {candidate_id}")
        else:
            errors.append(f"Capture attempt has an unknown source_mode: {candidate_id}")
        validate_sensitive_inputs(attempt, f"capture attempt {candidate_id}", errors)
        final_url = normalize_url(attempt.get("final_normalized_url") or "")
        if final_url and is_search_result_url(final_url):
            errors.append(f"Capture attempt redirected to a search-result page: {candidate_id}")
        redirect_external = bool(final_url and site_key(final_url) != attempt_site)
        if bool(attempt.get("redirect_external_domain")) != redirect_external:
            errors.append(f"Capture attempt external-redirect flag is inconsistent: {candidate_id}")
        if redirect_external and attempt.get("status") == "accepted_candidate":
            candidate_meta = read_json(run_dir / "candidate-pages" / candidate_id / "metadata.json", {}) or {}
            if candidate_meta.get("content_assertion_passed") is not True:
                errors.append(f"External-domain redirect was accepted without a passed text assertion: {candidate_id}")
        history = attempt.get("attempt_history")
        if not isinstance(history, list) or not history:
            errors.append(f"Capture attempt has no durable invocation history: {candidate_id}")
            history = []
        history_ids = []
        for entry in history:
            if not isinstance(entry, dict):
                errors.append(f"Capture attempt history contains a non-object: {candidate_id}")
                continue
            invocation_id = str(entry.get("invocation_id") or entry.get("attempt_id") or "")
            if not invocation_id or invocation_id in history_ids or invocation_id in global_history_ids:
                errors.append(f"Capture invocation ID is missing or globally duplicated: {candidate_id}/{invocation_id}")
            if entry.get("invocation_id") and entry.get("attempt_id") and entry["invocation_id"] != entry["attempt_id"]:
                errors.append(f"Capture invocation/attempt ID mismatch: {candidate_id}/{invocation_id}")
            history_ids.append(invocation_id)
            global_history_ids.add(invocation_id)
        if type(attempt.get("attempt_count")) is not int or attempt.get("attempt_count") != len(history):
            errors.append(f"Capture attempt_count does not match history length: {candidate_id}")
        orphan_ids = [
            str(entry.get("attempt_id") or candidate_id)
            for entry in history if isinstance(entry, dict) and entry.get("status") == "started"
        ]
        if attempt.get("status") == "started" and not orphan_ids:
            orphan_ids.append(str(attempt.get("attempt_id") or candidate_id))
        started_attempts.extend(orphan_ids)
        if attempt.get("status") == "accepted_candidate":
            if not all(attempt.get(flag) is True for flag in (
                "content_valid", "offline_ok", "portable_html_ok", "visual_similarity_ok"
            )):
                errors.append(f"Accepted capture row lacks archive/offline success flags: {candidate_id}")
            accepted_attempt_candidates.add(candidate_key)
            accepted_attempt_urls.add(attempt_url)
    max_pages_per_domain = max(1, int((config.get("budgets") or {}).get("max_pages_per_domain", 5)))
    for domain, urls in site_urls.items():
        if len(urls) > max_pages_per_domain:
            errors.append(f"Capture attempts exceed max_pages_per_domain={max_pages_per_domain} for site_key={domain}: {len(urls)}")
    if started_attempts:
        message = f"Capture attempt log contains started/orphan invocations: {sorted(set(started_attempts))}"
        (errors if args.strict else warnings).append(message)

    source_pairs = []
    for metadata_path in (run_dir / "source-pages").glob("*/metadata.json"):
        source_pairs.append((metadata_path.parent.resolve(), read_json(metadata_path, {}) or {}))
    formal_serp_count = 0
    accepted_blocked_count = 0
    empty_shell_accepted_count = 0
    wrong_subject_accepted_count = 0
    offline_pass = 0
    hash_checks = 0
    hash_pass = 0
    source_quality = {}
    registration = str((config.get("trademark") or {}).get("registration_number") or "")
    owner = str((config.get("trademark") or {}).get("owner") or "")

    if not source_pairs:
        errors.append("No formal destination pages were promoted")
    source_ids = []
    source_orders = []
    source_origins = []
    source_canonical_urls = []
    for source_dir, item in source_pairs:
        source_id = item.get("source_id")
        source_ids.append(source_id)
        source_orders.append(item.get("order"))
        url = item.get("final_url") or item.get("normalized_url") or item.get("requested_url") or ""
        source_canonical = canonical_url(item)
        source_canonical_urls.append(source_canonical)
        if item.get("record_type") != "target_page":
            errors.append(f"Formal source {source_id} has record_type={item.get('record_type')}, expected target_page")
        if not is_safe_file_id(source_id):
            errors.append(f"Formal source has an invalid source ID: {source_id!r}")
        if source_id != source_dir.name:
            errors.append(f"Source ID does not match directory name: {source_dir}")
        if item.get("evidence_accepted") is not True or item.get("content_valid") is not True:
            errors.append(f"Formal source is invalid or unpromoted: {source_id}")
        if item.get("search_result_page") or is_search_result_url(url):
            formal_serp_count += 1
            errors.append(f"Search-result page entered formal evidence: {source_id}")
        if item.get("page_state") in BLOCKED_STATES:
            accepted_blocked_count += 1
            if item.get("page_state") == "empty_shell":
                empty_shell_accepted_count += 1
            errors.append(f"Blocked/empty page entered formal evidence: {source_id} ({item.get('page_state')})")
        origin = item.get("origin_candidate_id")
        source_origins.append(origin)
        if not isinstance(origin, str) or not is_safe_file_id(origin):
            errors.append(f"Formal source has an invalid origin candidate ID: {source_id}")
        candidate_metadata_path = run_dir / "candidate-pages" / str(origin) / "metadata.json"
        candidate_metadata = read_json(candidate_metadata_path, {}) or {}
        if (
            not origin or str(origin).casefold() not in accepted_attempt_candidates or not candidate_metadata_path.is_file()
            or candidate_metadata.get("candidate_accepted") is not True
        ):
            errors.append(f"Formal source has no traceable accepted capture attempt: {source_id}")
        frontier_id = item.get("frontier_id")
        normalized_source_url = canonical_url(item, frontier_trace=True)
        source_mode = item.get("source_mode")
        provenance = item.get("source_provenance") or {}
        if frontier_id and source_mode == "discovered_frontier":
            if frontier_by_id.get(frontier_id) != normalized_source_url:
                errors.append(f"Formal source frontier ID and canonical normalized URL do not match: {source_id}")
            if str(item.get("manual_source_reason") or "").strip():
                errors.append(f"Discovered formal source improperly carries a manual-source reason: {source_id}")
        elif source_mode == "manual_user_url" and not frontier_id:
            reason = item.get("manual_source_reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"Manual formal source lacks a substantive reason: {source_id}")
        else:
            errors.append(f"Formal source has invalid/ambiguous source provenance: {source_id}")
        if provenance.get("source_mode") != source_mode:
            errors.append(f"Formal source source_provenance is inconsistent: {source_id}")
        assertions = item.get("expected_assertions") or []
        if not isinstance(assertions, list) or not any(str(value).strip() for value in assertions):
            errors.append(f"Formal source has no substantive content assertion: {source_id}")
        validate_sensitive_inputs(item, f"formal source {source_id}", errors)
        final_normalized = normalize_url(item.get("final_normalized_url") or item.get("final_url") or "")
        if not final_normalized or is_search_result_url(final_normalized):
            errors.append(f"Formal source final URL is invalid or a search-result page: {source_id}")
        redirect_external = bool(
            final_normalized and normalized_source_url
            and site_key(final_normalized) != site_key(normalized_source_url)
        )
        if bool(item.get("redirect_external_domain")) != redirect_external:
            errors.append(f"Formal source external-redirect flag is inconsistent: {source_id}")
        if redirect_external and item.get("content_assertion_passed") is not True:
            errors.append(f"Formal source external redirect lacks a passed content assertion: {source_id}")

        artifacts = item.get("artifacts") or {}
        missing = REQUIRED_SOURCE_ARTIFACTS - set(artifacts)
        if missing:
            errors.append(f"Formal source {source_id} lacks artifacts: {sorted(missing)}")
        for name, record in artifacts.items():
            if not record.get("path") or not record.get("sha256"):
                continue
            hash_checks += 1
            path = (source_dir / record["path"]).resolve()
            if not path.is_relative_to(source_dir) or not path.is_file():
                errors.append(f"Artifact is missing or escapes its source directory: {source_id}/{name}")
            elif path.stat().st_size != record.get("size_bytes") or sha256(path) != record.get("sha256"):
                errors.append(f"Artifact hash mismatch: {source_id}/{name}")
            else:
                hash_pass += 1

        mhtml_record = item.get("offline_replay") or {}
        singlefile_record = item.get("singlefile_replay") or {}
        mhtml_path = source_dir / ((artifacts.get("mhtml_validation") or {}).get("path") or "mhtml-validation.json")
        singlefile_path = source_dir / ((artifacts.get("offline_validation") or {}).get("path") or "offline-validation.json")
        comparison_path = source_dir / ((artifacts.get("archive_visual_comparison") or {}).get("path") or "archive-visual-comparison.json")
        mhtml_file = read_json(mhtml_path, {}) or {}
        singlefile_file = read_json(singlefile_path, {}) or {}
        comparison = read_json(comparison_path, {}) or {}
        if (
            mhtml_record.get("ok") and mhtml_file.get("ok") and mhtml_file.get("external_request_count") == 0
            and singlefile_record.get("ok") and singlefile_file.get("ok") and singlefile_file.get("external_request_count") == 0
            and comparison.get("ok") and comparison.get("similarity", 0) >= 0.90
        ):
            offline_pass += 1
        else:
            errors.append(f"MHTML/portable HTML replay or visual comparison did not pass: {source_id}")
        if mhtml_file.get("text_retention", 0) < 0.65 or singlefile_file.get("text_retention", 0) < 0.65:
            errors.append(f"Offline text retention is too low: {source_id}")
        if mhtml_file.get("image_preservation_rate", 0) < 0.8:
            errors.append(f"MHTML image preservation is too low: {source_id}")

        body_path = source_dir / ((artifacts.get("body_text") or {}).get("path") or "body-text.txt")
        body_text = body_path.read_text(encoding="utf-8", errors="replace") if body_path.is_file() else ""
        if item.get("page_type") == "registry" and registration and registration not in body_text:
            wrong_subject_accepted_count += 1
            errors.append(f"Registry source does not contain the exact registration number {registration}: {source_id}")
        if item.get("page_type") in {"company", "official_site"} and owner and owner not in body_text:
            wrong_subject_accepted_count += 1
            errors.append(f"Company/official source does not contain the exact owner name {owner}: {source_id}")
        review_dir = run_dir / "visual-reviews" / str(origin)
        has_review_files = review_dir.is_dir() and any(path.name != "consensus.json" for path in review_dir.glob("*.json"))
        has_visual_chain = bool(item.get("visual_consensus")) or has_review_files
        rebuilt: dict = {}
        reviews: list[dict] = []
        if item.get("page_role") == "evidence" or has_visual_chain:
            consensus = item.get("visual_consensus") or {}
            rebuilt, reviews = verify_visual_consensus(
                run_dir, source_id, origin, consensus, candidate_metadata, config, errors
            )
        manual_override = item.get("manual_review_override")
        if item.get("page_role") == "evidence":
            if manual_override is not None:
                errors.append(f"Manual review override is forbidden for positive evidence: {source_id}")
            if not owner:
                errors.append(f"Positive evidence has no configured expected owner: {source_id}")
            if not positive_confirmed(rebuilt, owner):
                errors.append(f"Positive evidence lacks owner-bound recomputed high-confidence consensus: {source_id}")
            if item.get("promotion_confidence") != "high":
                errors.append(f"Positive evidence promotion confidence is not high: {source_id}")
        elif manual_override is not None:
            if not isinstance(manual_override, str) or not manual_override.strip():
                errors.append(f"Manual review override lacks a substantive reason: {source_id}")
            if item.get("promotion_confidence") != "manual_review":
                errors.append(f"Manual override source was not downgraded to manual_review: {source_id}")

        pdf_record = artifacts.get("pdf") or {}
        pdf_path = (source_dir / pdf_record.get("path", "__missing__")).resolve()
        if pdf_path.is_file():
            quality = pdf_quality(pdf_path)
            source_quality[source_id] = quality
            if quality["blank_pages"]:
                errors.append(f"Source PDF contains blank pages: {source_id} {quality['blank_pages']}")
            if quality["repeated_low_text_pages"]:
                errors.append(f"Source PDF contains repeated low-information pages: {source_id} {quality['repeated_low_text_pages']}")

    if len(source_ids) != len({str(item).casefold() for item in source_ids}) or any(not item for item in source_ids):
        errors.append("Formal source IDs are missing or duplicated")
    if len(source_origins) != len({str(item).casefold() for item in source_origins}) or any(not item for item in source_origins):
        errors.append("A candidate is missing or was promoted more than once")
    if (
        len(source_orders) != len(set(source_orders))
        or any(type(item) is not int or item < 1 for item in source_orders)
    ):
        errors.append("Formal source orders are missing, invalid or duplicated")
    if len(source_canonical_urls) != len(set(source_canonical_urls)) or any(not item for item in source_canonical_urls):
        errors.append("Formal source canonical URLs are missing or duplicated")

    expected_skipped_links = build_skipped_links(
        frontier,
        probe_attempts,
        attempts,
        [item for _, item in source_pairs],
    )
    skipped_links = verify_skipped_links(
        run_dir, expected_skipped_links, results, order, errors
    )

    coverage_summary = (matrix.get("summary") or results.get("coverage") or {})
    if matrix.get("summary") != results.get("coverage"):
        errors.append("coverage-matrix summary does not match results coverage")
    execution_profile = config.get("execution_profile") or "forensic"
    if execution_profile not in {"quick", "forensic"}:
        errors.append(f"Unsupported execution_profile: {execution_profile!r}")
        execution_profile = "forensic"
    visual_review_files = [
        path for path in (run_dir / "visual-reviews").glob("*/*.json")
        if path.name != "consensus.json"
    ]
    visual_candidate_ids = {path.parent.name.casefold() for path in visual_review_files}
    budgets_config = config.get("budgets") or {}
    max_visual_candidates = int(budgets_config.get(
        "max_visual_candidates", 2 if execution_profile == "quick" else 10
    ))
    max_visual_invocations = int(budgets_config.get(
        "max_visual_invocations", 2 if execution_profile == "quick" else 30
    ))
    if len(visual_candidate_ids) > max_visual_candidates:
        errors.append(
            f"Visual candidate budget exceeded: {len(visual_candidate_ids)}/{max_visual_candidates}"
        )
    if len(visual_review_files) > max_visual_invocations:
        errors.append(
            f"Visual invocation budget exceeded: {len(visual_review_files)}/{max_visual_invocations}"
        )
    if execution_profile == "quick":
        if results.get("investigation_scope") != "quick_non_exhaustive":
            errors.append("Quick results do not declare investigation_scope=quick_non_exhaustive")
        if not str(results.get("conclusion") or "").strip():
            errors.append("Quick results lack a scoped conclusion")
    requirements_config = config.get("coverage_requirements") or {}
    require_each_query_success = bool(requirements_config.get("require_each_query_success", True))
    if require_each_query_success != (execution_profile == "forensic"):
        errors.append("coverage query-success policy is inconsistent with execution_profile")
    actual_query_ids = sorted({str(item.get("query_id")) for item in queries if item.get("query_id")})
    successful_query_ids = {
        str(item.get("query_id")) for item in queries
        if item.get("query_id") and item.get("state") in {"normal", "zero_results"}
    }
    actual_failed_query_ids = sorted(set(actual_query_ids) - successful_query_ids)
    if coverage_summary.get("execution_profile") != execution_profile:
        errors.append("Coverage execution_profile does not match run-config")
    if coverage_summary.get("query_count") != len(actual_query_ids):
        errors.append("Coverage query_count does not match discovery runs")
    if coverage_summary.get("successful_query_count") != len(successful_query_ids):
        errors.append("Coverage successful_query_count does not match discovery runs")
    if coverage_summary.get("failed_query_ids") != actual_failed_query_ids:
        errors.append("Coverage failed_query_ids does not match discovery runs")
    if coverage_summary.get("require_each_query_success") is not require_each_query_success:
        errors.append("Coverage query-success policy does not match run-config")
    if execution_profile == "quick" and actual_failed_query_ids:
        warnings.append(
            "Quick profile retained failed discovery queries: " + ", ".join(actual_failed_query_ids)
        )
    if coverage_summary.get("formal_serp_count") not in {0, None}:
        errors.append("Coverage summary reports formal search-result pages")
    if args.strict and coverage_summary.get("status") != "complete":
        errors.append("Investigation coverage status is not complete")
    if len(matrix.get("formal_sources") or []) != len(source_pairs):
        errors.append("coverage-matrix formal source count does not match source-pages")
    if len(order.get("items") or []) != len(source_pairs):
        errors.append("capture-order item count does not match source-pages")
    matrix_source_ids = [item.get("source_id") for item in matrix.get("formal_sources") or []]
    order_source_ids = [item.get("id") for item in order.get("items") or []]
    expected_source_ids = [
        item.get("source_id") for _, item in sorted(
            source_pairs, key=lambda pair: (pair[1].get("order") or 999999, pair[1].get("source_id") or "")
        )
    ]
    if matrix_source_ids != expected_source_ids:
        errors.append("coverage-matrix source order/IDs do not match promoted sources")
    if order_source_ids != expected_source_ids:
        errors.append("capture-order source order/IDs do not match promoted sources")

    normal_candidate_urls: set[str] = set()
    for metadata_path in (run_dir / "candidate-pages").glob("*/metadata.json"):
        candidate = read_json(metadata_path, {}) or {}
        candidate_id = candidate.get("candidate_id") or ""
        candidate_key = str(candidate_id).casefold()
        if not is_safe_file_id(candidate_id) or metadata_path.parent.name != candidate_id:
            errors.append(f"Candidate metadata has an unsafe/mismatched ID: {metadata_path}")
        is_normal = (
            candidate.get("candidate_accepted") is True
            and candidate.get("content_valid") is True
            and candidate.get("page_state") == "normal"
        )
        if not is_normal:
            continue
        candidate_url = canonical_url(candidate, frontier_trace=True)
        if candidate_key not in accepted_attempt_candidates:
            errors.append(f"Normal candidate has no accepted capture row: {candidate_id}")
        if not candidate_url or candidate_urls.get(candidate_key) != candidate_url or candidate_url in normal_candidate_urls:
            errors.append(f"Normal candidate URL is missing, mismatched or duplicated: {candidate_id}")
        if not all((candidate.get(field) or {}).get("ok") is True for field in (
            "offline_replay", "singlefile_replay", "archive_visual_comparison"
        )):
            errors.append(f"Normal candidate lacks archive/offline success flags: {candidate_id}")
        candidate_artifacts = candidate.get("artifacts") or {}
        missing_candidate_artifacts = REQUIRED_SOURCE_ARTIFACTS - set(candidate_artifacts)
        if missing_candidate_artifacts:
            errors.append(f"Normal candidate lacks required artifacts: {candidate_id}/{sorted(missing_candidate_artifacts)}")
        for name in REQUIRED_SOURCE_ARTIFACTS:
            record = candidate_artifacts.get(name) or {}
            artifact_path = (metadata_path.parent / str(record.get("path") or "__missing__")).resolve()
            if (
                not artifact_path.is_relative_to(metadata_path.parent.resolve())
                or not artifact_path.is_file()
                or artifact_path.stat().st_size != record.get("size_bytes")
                or sha256(artifact_path) != record.get("sha256")
            ):
                errors.append(f"Normal candidate artifact is missing/escaping/corrupt: {candidate_id}/{name}")
        if candidate_url:
            normal_candidate_urls.add(candidate_url)
    normal_candidate_count = len(normal_candidate_urls)
    actual_coverage = coverage_summary.get("actual") or {}
    if actual_coverage.get("discovery_providers") != len(successful_provider_bases(queries)):
        errors.append("Coverage discovery_providers does not match trusted base providers")
    if actual_coverage.get("unique_target_urls") != len(frontier_urls):
        errors.append("Coverage unique_target_urls does not match the URL frontier")
    frontier_sites = {site_key(url) for url in frontier_urls}
    if actual_coverage.get("target_domains") != len(frontier_sites):
        errors.append("Coverage target_domains does not match unique registrable site_key values")
    if actual_coverage.get("direct_capture_attempts") != len(accepted_attempt_urls):
        errors.append("Coverage direct_capture_attempts includes failed or blocked capture rows")
    if actual_coverage.get("capture_invocations_audit") != len(global_history_ids):
        errors.append("Coverage capture_invocations_audit does not match attempt history")
    if actual_coverage.get("normal_target_pages") != normal_candidate_count:
        errors.append("Coverage normal_target_pages includes non-normal or omits accepted normal candidates")
    if actual_coverage.get("formal_sources") != len(source_pairs):
        errors.append("Coverage formal_sources does not match promoted source pages")
    expected_requirements = {
        "discovery_providers": int(requirements_config.get("min_discovery_providers", 2)),
        "unique_target_urls": int(requirements_config.get("min_target_urls", 10)),
        "target_domains": int(requirements_config.get("min_target_domains", 3)),
        "direct_capture_attempts": int(requirements_config.get("min_capture_attempts", 8)),
        "normal_target_pages": int(requirements_config.get("min_normal_target_pages", 5)),
        "formal_sources": int(requirements_config.get("min_formal_sources", 1)),
    }
    if coverage_summary.get("requirements") != expected_requirements:
        errors.append("Coverage requirements do not match run-config")
    recomputed_actual = {
        "discovery_providers": len(successful_provider_bases(queries)),
        "unique_target_urls": len(frontier_urls),
        "target_domains": len(frontier_sites),
        "direct_capture_attempts": len(accepted_attempt_urls),
        "normal_target_pages": normal_candidate_count,
        "formal_sources": len(source_pairs),
    }
    expected_deficits = {
        key: {"actual": recomputed_actual[key], "required": required}
        for key, required in expected_requirements.items()
        if recomputed_actual[key] < required
    }
    if actual_failed_query_ids and require_each_query_success:
        expected_deficits["queries_without_successful_provider"] = {
            "actual": len(actual_failed_query_ids),
            "required": 0,
            "query_ids": actual_failed_query_ids,
        }
    if coverage_summary.get("deficits") != expected_deficits:
        errors.append("Coverage deficits do not match recomputed requirements")
    expected_coverage_status = "complete" if not expected_deficits else "insufficient_coverage"
    if coverage_summary.get("status") != expected_coverage_status:
        errors.append("Coverage status does not match recomputed deficits")

    binder_path = run_dir / "evidence-binder.pdf"
    binder_quality = None
    if binder_path.is_file():
        if build.get("capture_order_sha256") != sha256(run_dir / "capture-order.json"):
            errors.append("Binder build is not bound to the current capture-order.json hash")
        bindings = build.get("source_bindings") or []
        if build.get("source_bindings_sha256") != canonical_json_sha256(bindings):
            errors.append("Binder source_bindings hash is missing or inconsistent")
        binder_quality = pdf_quality(binder_path, skip_pages=int(build.get("cover_pages") or 0))
        if binder_quality["page_count"] != build.get("page_count"):
            errors.append("Binder build page count does not match the PDF")
        if sha256(binder_path) != build.get("sha256"):
            errors.append("Binder SHA-256 does not match its build record")
        if binder_quality["blank_pages"]:
            errors.append(f"Evidence binder contains blank pages: {binder_quality['blank_pages']}")
        if binder_quality["repeated_low_text_pages"]:
            errors.append(f"Evidence binder contains repeated low-information pages: {binder_quality['repeated_low_text_pages']}")
        ranges = build.get("page_ranges") or []
        if (
            len(ranges) != len(source_pairs)
            or len(bindings) != len(source_pairs)
            or any(item.get("placeholder") for item in ranges)
            or any(item.get("placeholder") for item in bindings)
        ):
            errors.append("Binder ranges are incomplete or contain placeholders")
        expected_page = int(build.get("cover_pages") or 0) + 1
        sorted_sources = sorted(source_pairs, key=lambda pair: (
            pair[1].get("order") if type(pair[1].get("order")) is int else 2**31,
            str(pair[1].get("source_id")).casefold(),
        ))
        order_items = order.get("items") or []
        for index, item in enumerate(ranges):
            if item.get("start_page") != expected_page or type(item.get("end_page")) is not int or item.get("end_page", 0) < expected_page:
                errors.append("Binder page ranges are not contiguous")
                break
            if index >= len(sorted_sources) or index >= len(bindings) or index >= len(order_items):
                break
            source_dir, source_metadata = sorted_sources[index]
            binding = bindings[index]
            order_item = order_items[index]
            source_id = source_metadata.get("source_id")
            relative_pdf = str(order_item.get("pdf") or "").replace("\\", "/")
            source_pdf = (run_dir / relative_pdf).resolve()
            if (
                item.get("id") != source_id
                or binding.get("id") != source_id
                or order_item.get("id") != source_id
                or item.get("source_pdf_relative") != relative_pdf
                or binding.get("source_pdf_relative") != relative_pdf
                or not source_pdf.is_relative_to(source_dir)
                or not source_pdf.is_file()
            ):
                errors.append(f"Binder source ID/path binding is inconsistent: {source_id}")
            else:
                source_hash = sha256(source_pdf)
                source_pages = pdf_page_count(source_pdf)
                expected_end = item["start_page"] + source_pages - 1
                for record in (item, binding):
                    if (
                        record.get("source_pdf_sha256") != source_hash
                        or record.get("source_page_count") != source_pages
                        or record.get("start_page") != item.get("start_page")
                        or record.get("end_page") != expected_end
                    ):
                        errors.append(f"Binder source hash/page/range binding is inconsistent: {source_id}")
                        break
            expected_page = item["end_page"] + 1
        if expected_page - 1 != build.get("page_count"):
            errors.append("Binder page ranges do not end at the actual page count")

    manifest = verify_manifest(run_dir, errors) if (run_dir / "manifest.json").is_file() else {}
    metrics = {
        "formal_sources": len(source_pairs), "formal_serp_count": formal_serp_count,
        "accepted_blocked_count": accepted_blocked_count, "empty_shell_accepted_count": empty_shell_accepted_count,
        "wrong_subject_accepted_count": wrong_subject_accepted_count,
        "offline_replay_pass_rate": offline_pass / len(source_pairs) if source_pairs else 0,
        "artifact_hash_pass_rate": hash_pass / hash_checks if hash_checks else 0,
        "discovery_providers": len(successful_provider_bases(queries)),
        "unique_target_urls": len(frontier_urls),
        "target_domains": len({site_key(url) for url in frontier_urls}),
        "direct_capture_attempts": len(accepted_attempt_urls),
        "capture_invocations_audit": len(global_history_ids),
        "started_orphan_attempts": len(set(started_attempts)),
        "normal_direct_pages": normal_candidate_count,
        "blank_page_count": len((binder_quality or {}).get("blank_pages", [])) + sum(len(item.get("blank_pages", [])) for item in source_quality.values()),
        "nav_only_repeated_page_count": len((binder_quality or {}).get("repeated_low_text_pages", [])) + sum(len(item.get("repeated_low_text_pages", [])) for item in source_quality.values()),
        "execution_profile": execution_profile,
        "discovery_query_count": len(actual_query_ids),
        "failed_discovery_queries": len(actual_failed_query_ids),
        "skipped_link_count": skipped_links.get("count", 0),
        "visual_candidates": len(visual_candidate_ids),
        "visual_invocations": len(visual_review_files),
    }
    payload = {
        "schema_version": "2.0", "ok": not errors, "strict": bool(args.strict),
        "validation_nonce": args.validation_nonce,
        "errors": errors, "warnings": warnings, "metrics": metrics,
        "source_pdf_quality": source_quality, "binder_quality": binder_quality,
        "manifest_file_count": len(manifest.get("files") or []),
    }
    (run_dir / "validation.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(0 if payload["ok"] else 2)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
