#!/usr/bin/env python3

"""Deterministic, fail-closed consensus for visual trademark reviews.

Model identity fields are audit assertions supplied by the caller.  Normalising and
cross-checking them prevents accidental/obvious alias reuse, but does not
cryptographically prove which remote model produced an answer.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone


REVIEW_SCHEMA_VERSION = "2.0"
ROLES = ("primary", "secondary", "tie_breaker")
CRITICAL_FIELDS = (
    "trademark_visible",
    "trademark_class",
    "commercial_use",
    "goods_match",
    "page_actor",
    "owner_attribution",
)
SCORE_TOLERANCE = 15
IDENTITY_FIELDS = ("model_name", "model_provider", "canonical_model_id", "model_family", "invocation_id")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso8601(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def parse_recorded_at(review: dict) -> datetime | None:
    """Return an aware timestamp. Do not fall back to a filename or review ID."""
    return parse_iso8601(review.get("recorded_at"))


def normalized_identity(value: object) -> str:
    """Canonicalise declared identity text with NFKC + casefold.

    Unicode dash variants are folded as well because they are a common way to make
    two visually identical family identifiers compare differently.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join("-" if unicodedata.category(char) == "Pd" else char for char in text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def normalized_actor(value: object) -> str:
    return normalized_identity(value)


def normalized_model(value: object) -> str:
    """Backward-compatible alias for callers that used the old helper."""
    return normalized_identity(value)


def model_family_key(value: object) -> str:
    """Return a punctuation-insensitive key for declared model-family independence."""
    return "".join(char for char in normalized_identity(value) if char.isalnum())


def canonical_json_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def review_fingerprint(review: dict) -> str:
    """Fingerprint the complete recorded base review, including inputs and identity."""
    return canonical_json_sha256(review)


def input_set_sha256(inputs: dict) -> str:
    """Hash the role-labelled visual input set in a deterministic order."""
    canonical = {
        "reference": sorted(inputs.get("reference") or [], key=lambda item: (item.get("path", ""), item.get("sha256", ""))),
        "fullpage": inputs.get("fullpage"),
        "regions": sorted(inputs.get("regions") or [], key=lambda item: (item.get("path", ""), item.get("sha256", ""))),
    }
    return canonical_json_sha256(canonical)


def field_value(review: dict, field: str):
    if field == "trademark_class":
        return (review.get("trademark_match") or {}).get("classification") or "unclear"
    if field == "page_actor":
        return str(review.get(field) or "").strip()
    return review.get(field) or "unclear"


def comparable(field: str, value: object):
    return normalized_actor(value) if field == "page_actor" else value


def is_unclear(field: str, value: object) -> bool:
    if field == "page_actor":
        return normalized_actor(value) in {"", "unclear", "unknown", "n/a", "not visible", "无法确定", "不明确", "未知"}
    return value == "unclear"


def score(review: dict) -> int:
    try:
        return int((review.get("trademark_match") or {}).get("score"))
    except (TypeError, ValueError):
        return -1


def _artifact_record_valid(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("path"), str)
        and value.get("path")
        and isinstance(value.get("size_bytes"), int)
        and value.get("size_bytes") >= 0
        and isinstance(value.get("sha256"), str)
        and SHA256_RE.fullmatch(value.get("sha256"))
    )


def validate_review_record(
    review: dict,
    candidate_id: str,
    *,
    run_created_at: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Validate review structure and timestamp bounds without trusting filenames."""
    errors: list[str] = []
    review_id = review.get("review_id")
    if review.get("schema_version") != REVIEW_SCHEMA_VERSION:
        errors.append("schema_version_invalid")
    if review.get("record_type") != "visual_review":
        errors.append("record_type_invalid")
    if review.get("candidate_id") != candidate_id:
        errors.append("candidate_id_mismatch")
    if not isinstance(review_id, str) or not ID_RE.fullmatch(review_id) or ".." in review_id:
        errors.append("review_id_invalid")
    if review.get("model_role") not in ROLES:
        errors.append("model_role_invalid")

    stamp = parse_recorded_at(review)
    if stamp is None:
        errors.append("recorded_at_invalid")
    else:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if stamp > current + timedelta(minutes=5):
            errors.append("recorded_at_in_future")
        created = parse_iso8601(run_created_at)
        if created is not None and stamp < created - timedelta(minutes=5):
            errors.append("recorded_at_before_run")

    identity = review.get("model_identity")
    if not isinstance(identity, dict):
        errors.append("model_identity_missing")
        identity = {}
    for field in IDENTITY_FIELDS:
        value = review.get(field) if field == "model_name" else identity.get(field)
        if not isinstance(value, str) or not value or value != normalized_identity(value):
            errors.append(f"{field}_missing_or_not_normalized")
    if identity.get("model_name") not in {None, review.get("model_name")}:
        errors.append("model_name_identity_mismatch")
    if not model_family_key(identity.get("model_family")):
        errors.append("model_family_invalid")
    receipt = identity.get("receipt_sha256")
    if receipt is not None and (not isinstance(receipt, str) or not SHA256_RE.fullmatch(receipt)):
        errors.append("receipt_sha256_invalid")

    inputs = review.get("inputs")
    if not isinstance(inputs, dict):
        errors.append("inputs_missing")
        inputs = {}
    references = inputs.get("reference")
    regions = inputs.get("regions")
    fullpage = inputs.get("fullpage")
    if not isinstance(references, list) or not references or any(not _artifact_record_valid(item) for item in references):
        errors.append("reference_inputs_invalid")
    if not _artifact_record_valid(fullpage):
        errors.append("fullpage_input_invalid")
    if not isinstance(regions, list) or not regions or any(not _artifact_record_valid(item) for item in regions):
        errors.append("region_inputs_invalid")
    if _artifact_record_valid(fullpage) and isinstance(regions, list):
        if any(item.get("path") == fullpage.get("path") for item in regions if isinstance(item, dict)):
            errors.append("region_must_differ_from_fullpage")
    declared_input_hash = review.get("input_set_sha256")
    if not isinstance(declared_input_hash, str) or not SHA256_RE.fullmatch(declared_input_hash):
        errors.append("input_set_sha256_invalid")
    elif input_set_sha256(inputs) != declared_input_hash:
        errors.append("input_set_sha256_mismatch")

    if review.get("trademark_visible") not in {"yes", "no", "unclear"}:
        errors.append("trademark_visible_invalid")
    trademark = review.get("trademark_match")
    if not isinstance(trademark, dict) or trademark.get("classification") not in {"exact", "near", "different", "unclear"}:
        errors.append("trademark_class_invalid")
    if score(review) not in range(0, 101):
        errors.append("trademark_score_invalid")
    if review.get("commercial_use") not in {"yes", "no", "unclear"}:
        errors.append("commercial_use_invalid")
    if review.get("goods_match") not in {"yes", "no", "unclear"}:
        errors.append("goods_match_invalid")
    if not isinstance(review.get("page_actor"), str) or not review.get("page_actor").strip():
        errors.append("page_actor_invalid")
    if review.get("owner_attribution") not in {"owner", "authorized_party", "third_party", "unrelated", "unclear"}:
        errors.append("owner_attribution_invalid")
    if not isinstance(review.get("rationale"), str) or not review.get("rationale").strip():
        errors.append("rationale_invalid")
    return errors


def validate_review_set(
    reviews: list[dict],
    candidate_id: str,
    *,
    run_created_at: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    errors: list[str] = []
    review_ids: dict[str, int] = {}
    invocation_ids: dict[str, int] = {}
    for index, review in enumerate(reviews):
        for error in validate_review_record(review, candidate_id, run_created_at=run_created_at, now=now):
            errors.append(f"review[{index}]:{error}")
        review_id = str(review.get("review_id") or "")
        review_ids[review_id] = review_ids.get(review_id, 0) + 1
        identity = review.get("model_identity") or {}
        invocation = normalized_identity(identity.get("invocation_id"))
        if invocation:
            invocation_ids[invocation] = invocation_ids.get(invocation, 0) + 1
    if any(count > 1 for count in review_ids.values()):
        errors.append("duplicate_review_id")
    if any(count > 1 for count in invocation_ids.values()):
        errors.append("duplicate_invocation_id")
    by_id = {
        str(review.get("review_id")): review
        for review in reviews
        if isinstance(review.get("review_id"), str)
    }
    for review in reviews:
        if review.get("model_role") != "tie_breaker":
            continue
        binding = review.get("tie_binding")
        if not isinstance(binding, dict):
            errors.append(f"review[{review.get('review_id')}]:tie_binding_missing")
            continue
        bound_reviews: list[dict] = []
        binding_valid = True
        for role in ("primary", "secondary"):
            pointer = binding.get(role)
            if not isinstance(pointer, dict):
                binding_valid = False
                continue
            bound = by_id.get(str(pointer.get("review_id") or ""))
            if (
                not bound
                or bound.get("model_role") != role
                or not isinstance(pointer.get("review_fingerprint"), str)
                or not SHA256_RE.fullmatch(pointer.get("review_fingerprint"))
            ):
                binding_valid = False
                continue
            bound_reviews.append(bound)
        if not binding_valid or len(bound_reviews) != 2:
            errors.append(f"review[{review.get('review_id')}]:tie_binding_invalid")
            continue
        tie_stamp = parse_recorded_at(review)
        if not tie_stamp or any(not parse_recorded_at(item) or tie_stamp <= parse_recorded_at(item) for item in bound_reviews):
            errors.append(f"review[{review.get('review_id')}]:tie_not_later_than_bound_base")
    return errors


def _review_pointer(review: dict) -> dict:
    identity = review.get("model_identity") or {}
    return {
        "review_id": review.get("review_id"),
        "review_fingerprint": review_fingerprint(review),
        "model_provider": identity.get("model_provider"),
        "canonical_model_id": identity.get("canonical_model_id"),
        "model_family": identity.get("model_family"),
        "invocation_id": identity.get("invocation_id"),
        "model_role": review.get("model_role"),
        "recorded_at": review.get("recorded_at"),
        "input_set_sha256": review.get("input_set_sha256"),
    }


def select_latest_by_role(reviews: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """Select solely by recorded_at; equal latest timestamps are an ambiguity."""
    selected: dict[str, dict] = {}
    errors: list[str] = []
    for role in ROLES:
        candidates = [item for item in reviews if item.get("model_role") == role]
        if not candidates:
            continue
        dated = [(parse_recorded_at(item), item) for item in candidates]
        if any(stamp is None for stamp, _ in dated):
            errors.append(f"{role}_review_missing_or_invalid_recorded_at")
            continue
        latest_stamp = max(stamp for stamp, _ in dated if stamp is not None)
        latest = [item for stamp, item in dated if stamp == latest_stamp]
        if len(latest) != 1:
            errors.append(f"{role}_latest_recorded_at_is_ambiguous")
            continue
        selected[role] = latest[0]
    return selected, errors


def base_conflicts(primary: dict, secondary: dict) -> list[str]:
    conflicts = []
    for field in CRITICAL_FIELDS:
        if comparable(field, field_value(primary, field)) != comparable(field, field_value(secondary, field)):
            conflicts.append(field)
    left_score, right_score = score(primary), score(secondary)
    if left_score < 0 or right_score < 0 or abs(left_score - right_score) > SCORE_TOLERANCE:
        conflicts.append("trademark_score")
    return conflicts


def tie_binding_for(primary: dict, secondary: dict) -> dict:
    return {
        "primary": {"review_id": primary.get("review_id"), "review_fingerprint": review_fingerprint(primary)},
        "secondary": {"review_id": secondary.get("review_id"), "review_fingerprint": review_fingerprint(secondary)},
    }


def tie_matches_active_base(tie: dict, primary: dict, secondary: dict) -> bool:
    binding = tie.get("tie_binding")
    if binding != tie_binding_for(primary, secondary):
        return False
    tie_stamp = parse_recorded_at(tie)
    p_stamp, s_stamp = parse_recorded_at(primary), parse_recorded_at(secondary)
    return bool(tie_stamp and p_stamp and s_stamp and tie_stamp > p_stamp and tie_stamp > s_stamp)


def _resolved_score(primary: dict, secondary: dict, tie: dict | None) -> tuple[int | None, dict]:
    p_score, s_score = score(primary), score(secondary)
    detail = {"primary": p_score, "secondary": s_score, "tie_breaker": score(tie or {}) if tie else None}
    if min(p_score, s_score) < 0:
        detail.update({"resolved": None, "rule": "invalid_score", "supporting_review_ids": []})
        return None, detail
    if abs(p_score - s_score) <= SCORE_TOLERANCE:
        resolved = round((p_score + s_score) / 2)
        detail.update({
            "resolved": resolved,
            "rule": "primary_secondary_within_tolerance",
            "supporting_review_ids": [primary.get("review_id"), secondary.get("review_id")],
            "minimum_supporting_score": min(p_score, s_score),
        })
        return resolved, detail
    if not tie:
        detail.update({"resolved": None, "rule": "tie_breaker_required", "supporting_review_ids": []})
        return None, detail
    t_score = score(tie)
    if t_score < 0:
        detail.update({"resolved": None, "rule": "invalid_tie_breaker_score", "supporting_review_ids": []})
        return None, detail
    supported = []
    if abs(p_score - t_score) <= SCORE_TOLERANCE:
        supported.append(primary)
    if abs(s_score - t_score) <= SCORE_TOLERANCE:
        supported.append(secondary)
    if not supported:
        detail.update({"resolved": None, "rule": "tie_breaker_did_not_resolve_score", "supporting_review_ids": []})
        return None, detail
    values = [t_score, *[score(item) for item in supported]]
    resolved = round(sum(values) / len(values))
    detail.update({
        "resolved": resolved,
        "rule": "tie_breaker_within_tolerance_of_base_review",
        "supporting_review_ids": [tie.get("review_id"), *[item.get("review_id") for item in supported]],
        "minimum_supporting_score": min(values),
    })
    return resolved, detail


def build_consensus(
    reviews: list[dict],
    candidate_id: str,
    updated_at: str | None = None,
    *,
    expected_owner: str | None = None,
    run_created_at: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Build consensus from complete review records and fail closed on any defect."""
    ordered = sorted(
        reviews,
        key=lambda item: (parse_recorded_at(item) or datetime.min.replace(tzinfo=timezone.utc), str(item.get("review_id") or "")),
    )
    selected, selection_errors = select_latest_by_role(reviews)
    integrity_errors = validate_review_set(reviews, candidate_id, run_created_at=run_created_at, now=now)
    family_keys = {
        model_family_key((item.get("model_identity") or {}).get("model_family"))
        for item in reviews
        if model_family_key((item.get("model_identity") or {}).get("model_family"))
    }
    consensus = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "visual_consensus",
        "candidate_id": candidate_id,
        "updated_at": updated_at or utc_now(),
        "review_ids": [item.get("review_id") for item in ordered],
        "model_family_count": len(family_keys),
        "selected_reviews": {role: _review_pointer(item) for role, item in selected.items() if role != "tie_breaker"},
        "resolution_rule": "active_base_bound_tie_breaker_field_majority",
        "tie_breaker_used": False,
        "tie_breaker_valid_for_active_base": False,
        "status": "single_model_manual_review",
        "confidence": "manual_review",
        "positive_evidence_eligible": False,
        "expected_owner": expected_owner,
        "actor_owner_match": False,
    }
    if integrity_errors or selection_errors:
        consensus.update({
            "status": "invalid_review_set",
            "integrity_errors": integrity_errors,
            "selection_errors": selection_errors,
        })
        return consensus

    primary, secondary = selected.get("primary"), selected.get("secondary")
    if not primary or not secondary:
        consensus["selection_errors"] = ["both_primary_and_secondary_are_required"]
        return consensus
    primary_family = model_family_key((primary.get("model_identity") or {}).get("model_family"))
    secondary_family = model_family_key((secondary.get("model_identity") or {}).get("model_family"))
    if not primary_family or not secondary_family or primary_family == secondary_family:
        consensus.update({
            "status": "invalid_role_assignment",
            "selection_errors": ["primary_and_secondary_must_use_different_normalized_model_families"],
        })
        return consensus
    if primary.get("input_set_sha256") != secondary.get("input_set_sha256"):
        consensus.update({"status": "conflict", "selection_errors": ["active_reviews_input_set_mismatch"]})
        return consensus

    conflicts = base_conflicts(primary, secondary)
    consensus["base_conflicts"] = conflicts
    selected_tie = selected.get("tie_breaker") if conflicts else None
    tie = None
    if selected_tie:
        consensus["selected_reviews"]["tie_breaker"] = _review_pointer(selected_tie)
        tie_family = model_family_key((selected_tie.get("model_identity") or {}).get("model_family"))
        if not tie_family or tie_family in {primary_family, secondary_family}:
            consensus.update({
                "status": "invalid_role_assignment",
                "selection_errors": ["tie_breaker_must_use_a_third_normalized_model_family"],
            })
            return consensus
        if selected_tie.get("input_set_sha256") != primary.get("input_set_sha256"):
            consensus.update({"status": "conflict", "selection_errors": ["active_reviews_input_set_mismatch"]})
            return consensus
        if not tie_matches_active_base(selected_tie, primary, secondary):
            consensus.update({
                "status": "conflict",
                "selection_errors": ["tie_breaker_not_bound_to_active_base_reviews"],
                "tie_breaker_invalidated": True,
            })
            return consensus
        tie = selected_tie
        consensus["tie_breaker_used"] = True
        consensus["tie_breaker_valid_for_active_base"] = True
        consensus["active_base_binding"] = tie_binding_for(primary, secondary)

    field_resolution: dict[str, dict] = {}
    unresolved: list[str] = []
    resolved: dict[str, object] = {}
    for field in CRITICAL_FIELDS:
        p_value, s_value = field_value(primary, field), field_value(secondary, field)
        p_comp, s_comp = comparable(field, p_value), comparable(field, s_value)
        detail = {
            "primary": p_value,
            "secondary": s_value,
            "tie_breaker": field_value(tie, field) if tie else None,
            "resolved": None,
            "rule": None,
            "supporting_review_ids": [],
        }
        if p_comp == s_comp:
            detail.update({
                "resolved": p_value,
                "rule": "primary_secondary_agreement",
                "supporting_review_ids": [primary.get("review_id"), secondary.get("review_id")],
            })
        elif tie:
            t_value = field_value(tie, field)
            t_comp = comparable(field, t_value)
            if t_comp == p_comp:
                detail.update({
                    "resolved": p_value,
                    "rule": "tie_breaker_supports_primary",
                    "supporting_review_ids": [primary.get("review_id"), tie.get("review_id")],
                })
            elif t_comp == s_comp:
                detail.update({
                    "resolved": s_value,
                    "rule": "tie_breaker_supports_secondary",
                    "supporting_review_ids": [secondary.get("review_id"), tie.get("review_id")],
                })
        if detail["resolved"] is None:
            unresolved.append(field)
        else:
            resolved[field] = detail["resolved"]
        field_resolution[field] = detail

    resolved_score, score_resolution = _resolved_score(primary, secondary, tie)
    if resolved_score is None:
        unresolved.append("trademark_score")
    consensus["resolution"] = {
        "method": "field_majority_using_active_base_bound_reviews",
        "fields": field_resolution,
        "score": score_resolution,
        "unresolved": unresolved,
    }
    if unresolved:
        consensus.update({"status": "conflict", "confidence": "manual_review"})
        return consensus

    unclear_fields = [field for field, value in resolved.items() if is_unclear(field, value)]
    actor_owner_match = bool(expected_owner and normalized_actor(resolved["page_actor"]) == normalized_actor(expected_owner))
    consensus.update({
        "trademark_visible": resolved["trademark_visible"],
        "trademark_class": resolved["trademark_class"],
        "trademark_score": resolved_score,
        "commercial_use": resolved["commercial_use"],
        "goods_match": resolved["goods_match"],
        "page_actor": resolved["page_actor"],
        "owner_attribution": resolved["owner_attribution"],
        "actor_owner_match": actor_owner_match,
        "unclear_fields": unclear_fields,
    })
    if unclear_fields:
        consensus.update({"status": "inconclusive", "confidence": "manual_review"})
        return consensus

    positive = (
        resolved["trademark_visible"] == "yes"
        and resolved["trademark_class"] in {"exact", "near"}
        and resolved["commercial_use"] == "yes"
        and resolved["goods_match"] == "yes"
        and resolved["owner_attribution"] == "owner"
        and actor_owner_match
    )
    consensus.update({
        "status": "confirmed" if positive else "confirmed_non_positive",
        "positive_evidence_eligible": positive,
        "confidence": "high" if (
            positive
            and resolved_score is not None
            and resolved_score >= 75
            and score_resolution.get("minimum_supporting_score", -1) >= 75
        ) else "medium",
    })
    return consensus
