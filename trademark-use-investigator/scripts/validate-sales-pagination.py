#!/usr/bin/env python3
"""Validate continuous sales-result pagination and its archived artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


REQUIRED_ARTIFACTS = ("rendered_dom", "mhtml", "fullpage", "pdf", "items")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_artifact(run_dir: Path, name: str, record: dict) -> list[str]:
    errors: list[str] = []
    raw_path = str(record.get("path") or "")
    if not raw_path:
        return [f"artifact_missing_path:{name}"]
    artifact = (run_dir / raw_path).resolve()
    if not artifact.is_relative_to(run_dir):
        return [f"artifact_outside_run:{name}"]
    if not artifact.is_file():
        return [f"artifact_file_missing:{name}:{raw_path}"]
    size = artifact.stat().st_size
    if size <= 0 or int(record.get("size_bytes") or -1) != size:
        errors.append(f"artifact_size_mismatch:{name}:{raw_path}")
    expected_hash = str(record.get("sha256") or "").lower()
    if len(expected_hash) != 64 or sha256_file(artifact) != expected_hash:
        errors.append(f"artifact_hash_mismatch:{name}:{raw_path}")
    return errors


def validate_page(run_dir: Path, page: dict, expected_index: int, zero_result: bool = False) -> list[str]:
    errors: list[str] = []
    if int(page.get("page_index") or 0) != expected_index:
        errors.append(f"page_index_mismatch:{page.get('page_index')}:{expected_index}")
    expected_state = "zero_results" if zero_result else "normal"
    if page.get("state") != expected_state:
        errors.append(f"page_state_invalid:{expected_index}:{page.get('state')}")
    if page.get("delivery_eligible") is not True:
        errors.append(f"page_not_delivery_eligible:{expected_index}")
    if zero_result and page.get("explicit_zero_results") is not True:
        errors.append("zero_result_not_explicit")
    if expected_index > 1 and (page.get("transition") or {}).get("verified") is not True:
        errors.append(f"pagination_transition_unverified:{expected_index}")
    artifacts = page.get("artifacts") or {}
    for name in REQUIRED_ARTIFACTS:
        record = artifacts.get(name)
        if not isinstance(record, dict):
            errors.append(f"artifact_record_missing:{expected_index}:{name}")
            continue
        errors.extend(validate_artifact(run_dir, f"page-{expected_index}:{name}", record))
    return errors


def validate_query(run_dir: Path, run: dict, required_pages: int, allow_zero_results: bool) -> list[str]:
    errors: list[str] = []
    pages = run.get("page_runs") or []
    status = str(run.get("pagination_status") or "")
    if status == "zero_results":
        if not allow_zero_results:
            return ["zero_result_shortfall_not_allowed"]
        if len(pages) != 1:
            errors.append(f"zero_result_page_count_invalid:{len(pages)}")
        elif isinstance(pages[0], dict):
            errors.extend(validate_page(run_dir, pages[0], 1, zero_result=True))
        return errors
    if status != "complete":
        return [f"pagination_status_invalid:{status or 'missing'}"]
    if len(pages) != required_pages:
        errors.append(f"page_count_invalid:{len(pages)}:{required_pages}")
    signatures: list[str] = []
    declared: list[int] = []
    for index, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            errors.append(f"page_record_invalid:{index}")
            continue
        errors.extend(validate_page(run_dir, page, index))
        signature = str(page.get("item_signature") or "")
        if not signature:
            errors.append(f"item_signature_missing:{index}")
        else:
            signatures.append(signature)
        value = page.get("declared_page_number")
        if isinstance(value, int):
            declared.append(value)
    if len(signatures) != len(set(signatures)):
        errors.append("duplicate_result_page_signature")
    if len(declared) > 1 and any(current <= previous for previous, current in zip(declared, declared[1:])):
        errors.append("declared_page_numbers_not_increasing")
    return errors


def validate(
    run_dir: Path,
    minimum_platforms: int,
    required_pages: int,
    allow_zero_results: bool,
    require_all_planned_queries: bool,
) -> dict:
    assisted = read_json(run_dir / "discovery" / "assisted-sales-results.json")
    plan = read_json(run_dir / "discovery" / "query-plan.json", {}) or {}
    if not isinstance(assisted, dict):
        raise ValueError("assisted-sales-results.json is missing or invalid")
    runs = [item for item in assisted.get("platform_runs") or [] if isinstance(item, dict)]
    requested_platforms = [str(item) for item in assisted.get("platforms_requested") or [] if item]
    errors: list[str] = []
    query_results: list[dict] = []
    by_key: dict[tuple[str, str], dict] = {}
    for item in runs:
        platform = str(item.get("platform") or "")
        query_id = str(item.get("query_id") or "")
        query_errors = validate_query(run_dir, item, required_pages, allow_zero_results)
        result = {
            "platform": platform,
            "query_id": query_id,
            "target_good": item.get("target_good"),
            "pagination_status": item.get("pagination_status"),
            "ok": not query_errors,
            "errors": query_errors,
        }
        query_results.append(result)
        by_key[(platform, query_id)] = result

    planned_by_platform: dict[str, list[str]] = {platform: [] for platform in requested_platforms}
    for item in plan.get("items") or []:
        platform = str(item.get("target_platform") or "")
        query_id = str(item.get("query_id") or "")
        if platform in planned_by_platform and query_id:
            planned_by_platform[platform].append(query_id)

    platform_results: list[dict] = []
    for platform in requested_platforms:
        planned_ids = list(dict.fromkeys(planned_by_platform.get(platform) or []))
        platform_runs = [item for item in query_results if item["platform"] == platform]
        if require_all_planned_queries:
            missing = [query_id for query_id in planned_ids if (platform, query_id) not in by_key]
            invalid = [
                query_id for query_id in planned_ids
                if (platform, query_id) in by_key and not by_key[(platform, query_id)]["ok"]
            ]
            ok = bool(planned_ids) and not missing and not invalid
        else:
            missing = []
            invalid = [item["query_id"] for item in platform_runs if not item["ok"]]
            ok = any(item["ok"] for item in platform_runs)
        platform_results.append({
            "platform": platform,
            "ok": ok,
            "planned_query_count": len(planned_ids),
            "captured_query_count": len(platform_runs),
            "missing_query_ids": missing,
            "invalid_query_ids": invalid,
        })
        if missing:
            errors.append(f"missing_queries:{platform}:{','.join(missing)}")

    qualified_platforms = [item["platform"] for item in platform_results if item["ok"]]
    if len(qualified_platforms) < minimum_platforms:
        errors.append(f"insufficient_platforms:{len(qualified_platforms)}:{minimum_platforms}")
    return {
        "schema_version": "1.0",
        "record_type": "sales_pagination_validation",
        "ok": not errors,
        "required_pages_per_query": required_pages,
        "minimum_platforms": minimum_platforms,
        "allow_zero_results": allow_zero_results,
        "require_all_planned_queries": require_all_planned_queries,
        "qualified_platform_count": len(qualified_platforms),
        "qualified_platforms": qualified_platforms,
        "query_results": query_results,
        "platform_results": platform_results,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate continuous sales-result pagination")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--min-platforms", type=int, default=3)
    parser.add_argument("--required-pages-per-query", type=int, default=5)
    parser.add_argument("--allow-zero-results", action="store_true")
    parser.add_argument("--require-all-planned-queries", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()
    result = validate(
        run_dir,
        max(1, args.min_platforms),
        max(1, args.required_pages_per_query),
        args.allow_zero_results,
        args.require_all_planned_queries,
    )
    output = Path(args.output).resolve() if args.output else run_dir / "sales-pagination-validation.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**result, "output": str(output)}, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
