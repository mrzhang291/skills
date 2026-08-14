#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fs_safety import assert_safe_tree, audit_run_tree, is_reparse_point
from url_utils import is_search_result_url, normalize_url, require_safe_file_id
from visual_consensus import (
    build_consensus,
    normalized_actor,
    validate_review_set,
)


REQUIRED_ARTIFACTS = {
    "singlefile_html", "singlefile_raw_html", "rendered_dom", "mhtml", "fullpage", "pdf",
    "body_text", "images_index", "links_index", "offline_validation",
    "mhtml_validation", "mhtml_offline_screenshot", "archive_visual_comparison",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_artifacts(candidate_dir: Path, metadata: dict) -> None:
    artifacts = metadata.get("artifacts") or {}
    missing = sorted(REQUIRED_ARTIFACTS - set(artifacts))
    if missing:
        raise ValueError("Candidate is missing required archive artifacts: " + ", ".join(missing))
    for name in REQUIRED_ARTIFACTS:
        record = artifacts[name]
        path = (candidate_dir / record.get("path", "__missing__")).resolve()
        if not path.is_relative_to(candidate_dir) or not path.is_file():
            raise ValueError(f"Artifact path is missing or escapes candidate directory: {name}")
        if sha256(path) != record.get("sha256"):
            raise ValueError(f"Artifact hash mismatch before promotion: {name}")


def canonical_page_url(metadata: dict) -> str:
    raw = metadata.get("final_url") or metadata.get("normalized_url") or metadata.get("requested_url") or ""
    normalized = normalize_url(raw)
    if not normalized:
        raise ValueError(f"Formal source lacks a valid canonical HTTP(S) URL: {raw}")
    return normalized


def load_existing_sources(run_dir: Path) -> list[dict]:
    records = []
    source_root = run_dir / "source-pages"
    if not source_root.exists():
        return records
    assert_safe_tree(source_root, run_dir, "source-pages")
    for path in source_root.glob("*/metadata.json"):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def validate_review_inputs(run_dir: Path, candidate_dir: Path, metadata: dict, review: dict) -> None:
    inputs = review.get("inputs") or {}
    reference_root = (run_dir / "reference").resolve()
    fullpage_metadata = (metadata.get("artifacts") or {}).get("fullpage") or {}
    fullpage = inputs.get("fullpage") or {}
    if fullpage.get("path") != fullpage_metadata.get("path"):
        raise ValueError("Visual review fullpage input does not match candidate metadata")
    groups = (
        (reference_root, inputs.get("reference") or [], "reference"),
        (candidate_dir, [fullpage], "fullpage"),
        (candidate_dir, inputs.get("regions") or [], "region"),
    )
    for root, records, role in groups:
        if not records:
            raise ValueError(f"Visual review has no {role} input")
        for record in records:
            path = (root / str(record.get("path") or "__missing__")).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(f"Visual review {role} input is missing or escapes its root")
            if path.stat().st_size != record.get("size_bytes") or sha256(path) != record.get("sha256"):
                raise ValueError(f"Visual review {role} input hash/size mismatch")


def load_verified_consensus(
    run_dir: Path, candidate_id: str, candidate_dir: Path, metadata: dict, config: dict
) -> tuple[dict | None, list[dict]]:
    review_dir = run_dir / "visual-reviews" / candidate_id
    consensus_path = review_dir / "consensus.json"
    if not consensus_path.is_file():
        if review_dir.is_dir() and any(path.name != "consensus.json" for path in review_dir.glob("*.json")):
            raise ValueError("Visual review files exist but consensus.json is missing")
        return None, []
    stored = json.loads(consensus_path.read_text(encoding="utf-8"))
    reviews = []
    for path in sorted(review_dir.glob("*.json")):
        if path.name == "consensus.json":
            continue
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("review_id") != path.stem:
            raise ValueError(f"Visual review filename/stem mismatch: {path.name}")
        reviews.append(item)
    if not reviews:
        raise ValueError("consensus.json exists but no visual review records were found")
    review_errors = validate_review_set(reviews, candidate_id, run_created_at=config.get("created_at"))
    if review_errors:
        raise ValueError("Visual review set is incomplete or invalid: " + ", ".join(review_errors))
    for review in reviews:
        validate_review_inputs(run_dir, candidate_dir, metadata, review)
    expected_owner = str((config.get("trademark") or {}).get("owner") or "").strip()
    rebuilt = build_consensus(
        reviews,
        candidate_id,
        updated_at=stored.get("updated_at"),
        expected_owner=expected_owner,
        run_created_at=config.get("created_at"),
    )
    if rebuilt.get("status") in {"invalid_review_set", "invalid_role_assignment"}:
        raise ValueError(f"Visual review set cannot be promoted: {rebuilt.get('status')}")
    if stored != rebuilt:
        raise ValueError("Visual consensus is stale, incomplete or inconsistent with its reviews")
    return stored, reviews


def negative_visual_finding(record: dict) -> bool:
    trademark_class = record.get("trademark_class") or (record.get("trademark_match") or {}).get("classification")
    commercial_use = record.get("commercial_use")
    goods_match = record.get("goods_match")
    owner_attribution = record.get("owner_attribution")
    trademark_visible = record.get("trademark_visible")
    return (
        trademark_class in {"different", "not_match", "unreadable"}
        or commercial_use in {"no", "non_commercial_reference"}
        or goods_match == "no"
        or owner_attribution in {"third_party", "unrelated"}
        or trademark_visible == "no"
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote an offline-verified target page into the formal evidence set")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--order", type=int, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--page-role", choices=["evidence", "supporting", "exclusion_reference"], required=True)
    parser.add_argument("--allow-manual-review", action="store_true")
    parser.add_argument("--manual-review-reason")
    args = parser.parse_args()

    args.candidate_id = require_safe_file_id(args.candidate_id, "candidate-id")
    args.source_id = require_safe_file_id(args.source_id, "source-id")
    if type(args.order) is not int or args.order < 1:
        raise ValueError("--order must be an integer greater than or equal to 1")
    if args.allow_manual_review and not args.manual_review_reason:
        raise ValueError("--allow-manual-review requires --manual-review-reason")
    if args.allow_manual_review and args.page_role == "evidence":
        raise ValueError("--allow-manual-review can never be combined with --page-role evidence")

    raw_run_dir = Path(args.run_dir).expanduser().absolute()
    if is_reparse_point(raw_run_dir):
        raise ValueError("RUN_DIR cannot be a symlink, junction or reparse point")
    run_dir = raw_run_dir.resolve(strict=True)
    audit_run_tree(run_dir)
    config_path = run_dir / "run-config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"Run configuration not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    expected_owner = str((config.get("trademark") or {}).get("owner") or "").strip()
    candidate_root = run_dir / "candidate-pages"
    assert_safe_tree(candidate_root, run_dir, "candidate-pages")
    candidate_dir = (candidate_root / args.candidate_id).resolve(strict=True)
    if not candidate_dir.is_relative_to(candidate_root.resolve(strict=True)):
        raise ValueError("Candidate directory escapes candidate-pages")
    metadata_path = candidate_dir / "metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"Candidate metadata not found: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("candidate_id") != args.candidate_id:
        raise ValueError("Candidate metadata ID does not match its requested directory")
    if metadata.get("candidate_accepted") is not True or metadata.get("content_valid") is not True:
        raise ValueError("Candidate did not pass target-page capture validation")
    if not (metadata.get("offline_replay") or {}).get("ok"):
        raise ValueError("Candidate did not pass primary MHTML replay validation")
    if not (metadata.get("singlefile_replay") or {}).get("ok"):
        raise ValueError("Candidate did not pass portable SingleFile replay validation")
    if not (metadata.get("archive_visual_comparison") or {}).get("ok"):
        raise ValueError("Candidate did not pass online/offline visual comparison")
    if metadata.get("search_result_page") or is_search_result_url(metadata.get("final_url") or metadata.get("normalized_url") or ""):
        raise ValueError("Search-result pages can never be promoted into formal evidence")
    if metadata.get("page_state") in {"captcha", "login_required", "access_denied", "empty_shell", "empty_results", "search_result_page"}:
        raise ValueError(f"Blocked or non-substantive page cannot be promoted: {metadata.get('page_state')}")
    validate_artifacts(candidate_dir, metadata)

    consensus, reviews = load_verified_consensus(run_dir, args.candidate_id, candidate_dir, metadata, config)
    if args.page_role == "evidence":
        if not consensus:
            raise ValueError("Evidence pages require a recorded visual consensus")
        if consensus.get("status") == "invalid_role_assignment":
            raise ValueError("Evidence page has an invalid visual-review role assignment")
        if not expected_owner:
            raise ValueError("Evidence promotion requires run-config.trademark.owner")
        if not positive_confirmed(consensus, expected_owner):
            if negative_visual_finding(consensus) or any(negative_visual_finding(review) for review in reviews):
                raise ValueError("Negative visual/use/attribution findings cannot be promoted as positive evidence")
            raise ValueError(
                "Evidence requires a recomputed positive high-confidence consensus bound to the exact configured owner; "
                "single-model, inconclusive, conflicting or authorized-party-only findings may only be supporting/exclusion_reference"
            )

    source_root = run_dir / "source-pages"
    source_root.mkdir(parents=True, exist_ok=True)
    assert_safe_tree(source_root, run_dir, "source-pages")
    source_dir = source_root / args.source_id
    if not source_dir.resolve(strict=False).is_relative_to(source_root.resolve(strict=True)):
        raise ValueError("Formal source directory escapes source-pages")
    case_alias = next(
        (child for child in source_root.iterdir() if child.name.casefold() == args.source_id.casefold()),
        None,
    )
    if case_alias is not None:
        raise FileExistsError(f"Formal source ID already exists (case-insensitive): {case_alias.name}")
    if source_dir.exists():
        raise FileExistsError(f"Formal source already exists: {source_dir}")
    existing_sources = load_existing_sources(run_dir)
    max_formal_sources = max(1, int((config.get("budgets") or {}).get("max_formal_sources", 20)))
    if len(existing_sources) >= max_formal_sources:
        raise ValueError(f"max_formal_sources={max_formal_sources} reached")
    candidate_url = canonical_page_url(metadata)
    candidate_key = args.candidate_id.casefold()
    source_key = args.source_id.casefold()
    for existing in existing_sources:
        existing_source = require_safe_file_id(existing.get("source_id"), "existing source-id")
        existing_candidate = require_safe_file_id(
            existing.get("origin_candidate_id"), "existing origin-candidate-id"
        )
        if existing_source.casefold() == source_key:
            raise ValueError(f"Formal source ID differs only by case: {existing_source}")
        if existing_candidate.casefold() == candidate_key:
            raise ValueError(f"Candidate was already promoted as {existing.get('source_id')}")
        if type(existing.get("order")) is not int or existing.get("order") < 1:
            raise ValueError(f"Existing formal source has an invalid order: {existing.get('source_id')}")
        if existing.get("order") == args.order:
            raise ValueError(f"Formal source order is already used by {existing.get('source_id')}: {args.order}")
        if canonical_page_url(existing) == candidate_url:
            raise ValueError(f"Canonical URL was already promoted as {existing.get('source_id')}: {candidate_url}")

    promoted = dict(metadata)
    promoted.update({
        "schema_version": "2.0", "record_type": "target_page",
        "source_id": args.source_id, "origin_candidate_id": args.candidate_id,
        "order": args.order, "label": args.label, "page_role": args.page_role,
        "evidence_accepted": True, "accepted_at": datetime.now(timezone.utc).isoformat(),
        "visual_consensus": consensus,
        "manual_review_override": args.manual_review_reason if args.allow_manual_review else None,
        "promotion_confidence": "manual_review" if args.allow_manual_review else (
            "high" if args.page_role == "evidence" and positive_confirmed(consensus or {}, expected_owner) else "not_applicable"
        ),
    })
    temp_dir = source_root / f".{args.source_id}.promoting-{uuid.uuid4().hex}"
    try:
        shutil.copytree(candidate_dir, temp_dir)
        (temp_dir / "metadata.json").write_text(
            json.dumps(promoted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temp_dir.rename(source_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    print(json.dumps({"promoted": True, "source_id": args.source_id, "source_dir": str(source_dir), "page_role": args.page_role}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
