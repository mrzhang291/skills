#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from visual_consensus import (
    REVIEW_SCHEMA_VERSION,
    base_conflicts,
    build_consensus,
    input_set_sha256,
    model_family_key,
    normalized_identity,
    select_latest_by_role,
    tie_binding_for,
    validate_review_set,
)


ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(path: Path, root: Path) -> dict:
    return {
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "size_bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def resolve_scoped(root: Path, raw: str, label: str) -> Path:
    path = (root / raw).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"{label} is missing or outside its permitted directory: {raw}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Record one traceable visual review for a captured target page")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--model-name", required=True, help="Human-readable deployed model name (audit assertion)")
    parser.add_argument("--model-provider", required=True)
    parser.add_argument("--canonical-model-id", required=True)
    parser.add_argument("--model-family", required=True)
    parser.add_argument("--invocation-id", required=True, help="Provider/tool invocation identifier, unique within this candidate")
    parser.add_argument("--receipt-sha256", help="Optional SHA-256 of a separately retained provider receipt")
    parser.add_argument("--model-role", choices=["primary", "secondary", "tie_breaker"], required=True)
    parser.add_argument("--trademark-visible", choices=["yes", "no", "unclear"], required=True)
    parser.add_argument("--trademark-class", choices=["exact", "near", "different", "unclear"], required=True)
    parser.add_argument("--trademark-score", type=int, required=True)
    parser.add_argument("--commercial-use", choices=["yes", "no", "unclear"], required=True)
    parser.add_argument("--goods-match", choices=["yes", "no", "unclear"], required=True)
    parser.add_argument("--page-actor", required=True)
    parser.add_argument("--owner-attribution", choices=["owner", "authorized_party", "third_party", "unrelated", "unclear"], required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument(
        "--reference-input", action="append", required=True,
        help="Path relative to RUN_DIR/reference; repeat for every reference image shown to the model",
    )
    parser.add_argument(
        "--fullpage-input", required=True,
        help="Path relative to the candidate directory; must equal metadata.artifacts.fullpage.path",
    )
    parser.add_argument(
        "--region-input", action="append", required=True,
        help="Path relative to the candidate directory; repeat for every crop shown to the model",
    )
    args = parser.parse_args()

    if not 0 <= args.trademark_score <= 100:
        raise ValueError("trademark-score must be between 0 and 100")
    for value, label in ((args.candidate_id, "candidate-id"), (args.review_id, "review-id")):
        if not ID_RE.fullmatch(value) or ".." in value:
            raise ValueError(f"{label} contains unsupported characters")
    if args.receipt_sha256 and not SHA256_RE.fullmatch(args.receipt_sha256):
        raise ValueError("receipt-sha256 must be exactly 64 hexadecimal characters")
    if not args.page_actor.strip():
        raise ValueError("page-actor must not be empty; use the explicit value 'unclear' when necessary")
    if not args.rationale.strip():
        raise ValueError("rationale must not be empty")

    normalized_model_name = normalized_identity(args.model_name)
    identity = {
        "model_provider": normalized_identity(args.model_provider),
        "canonical_model_id": normalized_identity(args.canonical_model_id),
        "model_family": normalized_identity(args.model_family),
        "invocation_id": normalized_identity(args.invocation_id),
    }
    if any(not value for value in [normalized_model_name, *identity.values()]):
        raise ValueError("All model identity fields must be non-empty after NFKC/casefold normalization")
    if not model_family_key(identity["model_family"]):
        raise ValueError("model-family must contain letters or digits")
    if args.receipt_sha256:
        identity["receipt_sha256"] = args.receipt_sha256.lower()

    run_dir = Path(args.run_dir).resolve()
    candidate_dir = (run_dir / "candidate-pages" / args.candidate_id).resolve()
    metadata_path = candidate_dir / "metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"Candidate metadata not found: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("candidate_accepted") is not True:
        raise ValueError("Visual review can only be recorded for an accepted, offline-verified candidate")

    config_path = run_dir / "run-config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"Run configuration not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    profile = str(config.get("execution_profile") or "forensic").strip().lower()
    default_visual_invocations = 2 if profile == "quick" else 30
    try:
        max_visual_candidates = int((config.get("budgets") or {})["max_visual_candidates"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("run-config.budgets.max_visual_candidates must be a positive integer") from exc
    if max_visual_candidates < 1:
        raise ValueError("run-config.budgets.max_visual_candidates must be a positive integer")
    try:
        max_visual_invocations = int(
            (config.get("budgets") or {}).get("max_visual_invocations", default_visual_invocations)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("run-config.budgets.max_visual_invocations must be a positive integer") from exc
    if max_visual_invocations < 1:
        raise ValueError("run-config.budgets.max_visual_invocations must be a positive integer")

    reference_root = (run_dir / "reference").resolve()
    reference_paths = [resolve_scoped(reference_root, raw, "reference-input") for raw in args.reference_input]
    fullpage_path = resolve_scoped(candidate_dir, args.fullpage_input, "fullpage-input")
    fullpage_metadata = (metadata.get("artifacts") or {}).get("fullpage") or {}
    expected_fullpage = str(fullpage_metadata.get("path") or "").replace("\\", "/")
    actual_fullpage = str(fullpage_path.relative_to(candidate_dir)).replace("\\", "/")
    if not expected_fullpage or actual_fullpage != expected_fullpage:
        raise ValueError(
            "fullpage-input must exactly equal candidate metadata.artifacts.fullpage.path "
            f"({expected_fullpage or 'missing'})"
        )
    if sha256(fullpage_path) != fullpage_metadata.get("sha256") or fullpage_path.stat().st_size != fullpage_metadata.get("size_bytes"):
        raise ValueError("fullpage-input does not match the candidate metadata hash/size")
    region_paths = [resolve_scoped(candidate_dir, raw, "region-input") for raw in args.region_input]
    if any(path == fullpage_path for path in region_paths):
        raise ValueError("Every region-input must differ from fullpage-input")
    if len(set(reference_paths)) != len(reference_paths) or len(set(region_paths)) != len(region_paths):
        raise ValueError("Duplicate reference-input or region-input values are not allowed")

    inputs = {
        "reference": [artifact(path, reference_root) for path in reference_paths],
        "fullpage": artifact(fullpage_path, candidate_dir),
        "regions": [artifact(path, candidate_dir) for path in region_paths],
    }
    declared_input_set = input_set_sha256(inputs)

    visual_root = run_dir / "visual-reviews"
    reviewed_candidates: set[str] = set()
    recorded_reviews: list[Path] = []
    if visual_root.is_dir():
        for directory in visual_root.iterdir():
            if not directory.is_dir():
                continue
            candidate_reviews = [path for path in directory.glob("*.json") if path.name != "consensus.json"]
            recorded_reviews.extend(candidate_reviews)
            if candidate_reviews:
                reviewed_candidates.add(directory.name)
    if args.candidate_id not in reviewed_candidates and len(reviewed_candidates) >= max_visual_candidates:
        raise ValueError(
            "Visual candidate budget exhausted: "
            f"{len(reviewed_candidates)}/{max_visual_candidates} unique candidates already reviewed"
        )

    review_dir = visual_root / args.candidate_id
    review_dir.mkdir(parents=True, exist_ok=True)
    review_path = review_dir / f"{args.review_id}.json"
    if review_path.exists():
        raise FileExistsError(f"Review already exists: {review_path}")
    if len(recorded_reviews) >= max_visual_invocations:
        raise ValueError(
            "Visual invocation budget exhausted: "
            f"{len(recorded_reviews)}/{max_visual_invocations} unique review calls already recorded"
        )
    reviews: list[dict] = []
    for path in sorted(review_dir.glob("*.json")):
        if path.name == "consensus.json":
            continue
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("review_id") != path.stem:
            raise ValueError(f"Existing visual review filename does not match review_id: {path.name}")
        reviews.append(item)
    existing_errors = validate_review_set(
        reviews,
        args.candidate_id,
        run_created_at=config.get("created_at"),
    ) if reviews else []
    if existing_errors:
        raise ValueError("Existing visual review set is invalid: " + ", ".join(existing_errors))
    if any(item.get("review_id") == args.review_id for item in reviews):
        raise ValueError("review-id must be unique within the candidate")
    if any(normalized_identity((item.get("model_identity") or {}).get("invocation_id")) == identity["invocation_id"] for item in reviews):
        raise ValueError("invocation-id must be unique within the candidate")

    selected, selection_errors = select_latest_by_role(reviews)
    if selection_errors:
        raise ValueError("Existing visual review roles are invalid: " + ", ".join(selection_errors))
    if args.model_role in {"primary", "secondary"}:
        counterpart_role = "secondary" if args.model_role == "primary" else "primary"
        counterpart = selected.get(counterpart_role)
        if counterpart and model_family_key((counterpart.get("model_identity") or {}).get("model_family")) == model_family_key(identity["model_family"]):
            raise ValueError("primary and secondary must use two different normalized model-family values")

    recorded_at = datetime.now(timezone.utc).isoformat()
    record = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "visual_review",
        "candidate_id": args.candidate_id,
        "review_id": args.review_id,
        "model_name": normalized_model_name,
        "model_identity": identity,
        "model_role": args.model_role,
        "recorded_at": recorded_at,
        "inputs": inputs,
        "input_set_sha256": declared_input_set,
        "trademark_visible": args.trademark_visible,
        "trademark_match": {"classification": args.trademark_class, "score": args.trademark_score},
        "commercial_use": args.commercial_use,
        "goods_match": args.goods_match,
        "page_actor": args.page_actor.strip(),
        "owner_attribution": args.owner_attribution,
        "rationale": args.rationale.strip(),
    }
    if args.model_role == "tie_breaker":
        primary, secondary = selected.get("primary"), selected.get("secondary")
        if not primary or not secondary:
            raise ValueError("tie_breaker is allowed only after both primary and secondary reviews")
        if not base_conflicts(primary, secondary):
            raise ValueError("tie_breaker is allowed only when the active primary and secondary reviews disagree")
        family = model_family_key(identity["model_family"])
        base_families = {
            model_family_key((primary.get("model_identity") or {}).get("model_family")),
            model_family_key((secondary.get("model_identity") or {}).get("model_family")),
        }
        if family in base_families:
            raise ValueError("tie_breaker must use a third, different normalized model-family")
        if primary.get("input_set_sha256") != secondary.get("input_set_sha256"):
            raise ValueError("tie_breaker cannot be recorded until active base reviews use the same input set")
        if declared_input_set != primary.get("input_set_sha256"):
            raise ValueError("tie_breaker must use exactly the same input set as the active base reviews")
        record["tie_binding"] = tie_binding_for(primary, secondary)

    prospective = [*reviews, record]
    integrity_errors = validate_review_set(
        prospective,
        args.candidate_id,
        run_created_at=config.get("created_at"),
    )
    if integrity_errors:
        raise ValueError("New visual review would create an invalid review set: " + ", ".join(integrity_errors))
    owner = str((config.get("trademark") or {}).get("owner") or "").strip()
    consensus = build_consensus(
        prospective,
        args.candidate_id,
        updated_at=recorded_at,
        expected_owner=owner,
        run_created_at=config.get("created_at"),
    )
    review_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (review_dir / "consensus.json").write_text(json.dumps(consensus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"review": str(review_path), "consensus": consensus}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
