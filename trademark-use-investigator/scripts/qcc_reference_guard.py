#!/usr/bin/env python3
"""Strict provenance validation for the QCC trademark visual reference."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

try:
    from PIL import Image
except (ImportError, OSError):  # Let preflight emit the actionable dependency error.
    Image = None


QCC_BRAND_PATH = re.compile(r"^/brandDetail/[a-f0-9]{32}\.html$", re.I)
HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$", re.I)
ALLOWED_HTML_MODES = {"network", "live_cdp_rendered_dom"}
ALLOWED_IMAGE_MODES = {"network", "live_cdp_browser_request"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def is_exact_qcc_brand_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "www.qcc.com"
        and port is None
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and QCC_BRAND_PATH.fullmatch(parsed.path)
    )


def is_loopback_cdp_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and port is not None
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def browser_product_matches(expected: str, detected: str) -> bool:
    product = detected.casefold()
    return (
        (expected == "edge" and ("edg/" in product or "microsoft edge" in product))
        or (expected == "chrome" and "chrome/" in product and "edg/" not in product)
    )


def has_timezone_timestamp(value: object) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def resolve_run_file(run_dir: Path, raw: object, label: str, errors: list[str]) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        errors.append(f"missing_{label}")
        return None
    path = (run_dir / value).resolve()
    if not path.is_relative_to(run_dir):
        errors.append(f"{label}_outside_run")
        return None
    if not path.is_file():
        errors.append(f"{label}_missing")
        return None
    return path


def diagnostic_qcc_navigation_candidate(run_dir: Path) -> str | None:
    """Return an exact QCC URL from this RUN's intact network diagnostic.

    This URL is navigation-only and is never treated as a validated visual
    reference.  The later CDP capture still has to verify the registration
    number, mark and owner before any evidence record can be produced.
    """
    run_dir = Path(run_dir).resolve()
    failure = read_json(run_dir / "capture-diagnostics" / "QCC" / "reference-fetch" / "failure.json")
    if failure.get("record_type") != "qcc_reference_fetch_failure":
        return None
    source_url = str(failure.get("source_url") or "").strip()
    if not is_exact_qcc_brand_url(source_url):
        return None
    expected_hash = str(failure.get("response_sha256") or "").strip().casefold()
    if not HEX_SHA256.fullmatch(expected_hash):
        return None
    errors: list[str] = []
    response_path = resolve_run_file(
        run_dir, failure.get("response_file"), "qcc_failure_response", errors,
    )
    if errors or response_path is None or sha256(response_path).casefold() != expected_hash:
        return None
    return source_url


def validate_live_capture_provenance(
    run_dir: Path,
    record: dict,
    html_path: Path | None,
    image_path: Path | None,
    expected_registration: str,
    expected_name: str,
    expected_owner: str,
    errors: list[str],
) -> None:
    provenance = record.get("live_capture_provenance")
    if not isinstance(provenance, dict):
        errors.append("missing_live_capture_provenance")
        return
    if provenance.get("schema_version") != "1.0":
        errors.append("invalid_live_provenance_schema")
    if provenance.get("record_type") != "qcc_live_capture_provenance":
        errors.append("invalid_live_provenance_record_type")

    metadata_path = resolve_run_file(
        run_dir, provenance.get("metadata_file"), "qcc_live_metadata", errors
    )
    metadata_hash = str(provenance.get("metadata_sha256") or "").strip().casefold()
    if not HEX_SHA256.fullmatch(metadata_hash):
        errors.append("invalid_qcc_live_metadata_sha256")
    elif metadata_path and sha256(metadata_path).casefold() != metadata_hash:
        errors.append("qcc_live_metadata_sha256_mismatch")
    metadata = read_json(metadata_path) if metadata_path else {}
    if metadata.get("schema_version") != "1.0" or metadata.get("record_type") != "qcc_live_dom_capture":
        errors.append("invalid_qcc_live_metadata_record")

    source_url = str(record.get("source_url") or "").strip()
    if (
        str(provenance.get("source_url") or "").strip() != source_url
        or str(provenance.get("final_url") or "").strip() != source_url
        or str(metadata.get("source_url") or "").strip() != source_url
        or str(metadata.get("final_url") or "").strip() != source_url
        or not is_exact_qcc_brand_url(source_url)
    ):
        errors.append("live_qcc_url_identity_mismatch")
    if not has_timezone_timestamp(provenance.get("captured_at")):
        errors.append("invalid_live_qcc_captured_at")
    if provenance.get("captured_at") != metadata.get("captured_at"):
        errors.append("live_qcc_captured_at_mismatch")

    endpoint = str(provenance.get("cdp_endpoint") or "").strip()
    if (
        provenance.get("cdp_attach_mode") is not True
        or provenance.get("cdp_endpoint_loopback") is not True
        or not is_loopback_cdp_endpoint(endpoint)
        or metadata.get("cdp_attach_mode") is not True
        or metadata.get("cdp_endpoint_loopback") is not True
        or str(metadata.get("cdp_endpoint") or "").strip() != endpoint
    ):
        errors.append("invalid_live_qcc_loopback_cdp_provenance")

    expected_browser = str(provenance.get("expected_browser_product") or "").strip().casefold()
    detected_browser = str(provenance.get("cdp_browser") or "").strip()
    if (
        expected_browser not in {"edge", "chrome"}
        or provenance.get("browser_product_validated") is not True
        or not browser_product_matches(expected_browser, detected_browser)
        or str(metadata.get("expected_browser_product") or "").strip().casefold() != expected_browser
        or str(metadata.get("cdp_browser") or "").strip() != detected_browser
        or metadata.get("browser_product_validated") is not True
    ):
        errors.append("invalid_live_qcc_browser_product_provenance")

    required_identity_flags = {
        "exact_brand_url", "source_final_url_match", "registration_number", "mark_name", "owner"
    }
    provenance_flags = provenance.get("identity_validation") or {}
    metadata_flags = metadata.get("identity_validation") or {}
    if (
        provenance.get("identity_validated") is not True
        or metadata.get("identity_validated") is not True
        or any(provenance_flags.get(field) is not True for field in required_identity_flags)
        or any(metadata_flags.get(field) is not True for field in required_identity_flags)
    ):
        errors.append("invalid_live_qcc_identity_validation")
    if (
        str(metadata.get("registration_number") or "").strip() != expected_registration
        or normalize(metadata.get("name")) != normalize(expected_name)
        or (expected_owner and normalize(metadata.get("owner")) != normalize(expected_owner))
        or str(metadata.get("image_url") or "").strip() != str(record.get("image_url") or "").strip()
    ):
        errors.append("live_qcc_metadata_identity_mismatch")

    for kind, copied_path in (("html", html_path), ("image", image_path)):
        provenance_rel = str(provenance.get(f"{kind}_file") or "").strip()
        metadata_rel = str(metadata.get(f"{kind}_file") or "").strip()
        if metadata_rel != provenance_rel:
            errors.append(f"live_qcc_{kind}_path_mismatch")
        source_path = resolve_run_file(
            run_dir, provenance_rel, f"qcc_live_{kind}", errors
        )
        provenance_hash = str(provenance.get(f"{kind}_sha256") or "").strip().casefold()
        metadata_file_hash = str(metadata.get(f"{kind}_sha256") or "").strip().casefold()
        provenance_bytes = provenance.get(f"{kind}_bytes")
        metadata_bytes = metadata.get(f"{kind}_bytes")
        if not HEX_SHA256.fullmatch(provenance_hash) or metadata_file_hash != provenance_hash:
            errors.append(f"invalid_live_qcc_{kind}_sha256")
        if (
            not isinstance(provenance_bytes, int)
            or isinstance(provenance_bytes, bool)
            or provenance_bytes <= 0
            or metadata_bytes != provenance_bytes
        ):
            errors.append(f"invalid_live_qcc_{kind}_bytes")
        if source_path:
            if source_path.stat().st_size != provenance_bytes:
                errors.append(f"live_qcc_{kind}_bytes_mismatch")
            if HEX_SHA256.fullmatch(provenance_hash) and sha256(source_path).casefold() != provenance_hash:
                errors.append(f"live_qcc_{kind}_sha256_mismatch")
        if copied_path:
            if copied_path.stat().st_size != provenance_bytes:
                errors.append(f"live_qcc_{kind}_copy_bytes_mismatch")
            if HEX_SHA256.fullmatch(provenance_hash) and sha256(copied_path).casefold() != provenance_hash:
                errors.append(f"live_qcc_{kind}_copy_sha256_mismatch")
            if record.get(f"{kind}_bytes") != provenance_bytes:
                errors.append(f"live_qcc_{kind}_record_bytes_mismatch")
            if str(record.get(f"{kind}_sha256") or "").casefold() != provenance_hash:
                errors.append(f"live_qcc_{kind}_record_sha256_mismatch")


def validate_qcc_reference(run_dir: Path, *, allow_supplied_file: bool = False) -> dict:
    run_dir = Path(run_dir).resolve()
    config = read_json(run_dir / "run-config.json")
    trademark = config.get("trademark") or {}
    orchestration = config.get("cherrystudio_orchestration") or {}
    record = read_json(run_dir / "reference" / "qcc-reference.json")
    errors: list[str] = []

    if record.get("schema_version") != "1.0":
        errors.append("invalid_schema_version")
    if record.get("record_type") != "qcc_trademark_visual_reference":
        errors.append("invalid_record_type")
    if record.get("visual_reference_primary") is not True:
        errors.append("visual_reference_not_primary")
    if record.get("source") == "user_provided_reference" or record.get("fetch_status") == "user_provided":
        errors.append("user_provided_reference_cannot_replace_qcc")

    source_url = str(record.get("source_url") or "").strip()
    if not is_exact_qcc_brand_url(source_url):
        errors.append("invalid_qcc_brand_url")
    expected_source_url = str(orchestration.get("qcc_brand_url_hint") or "").strip()
    if expected_source_url:
        if not is_exact_qcc_brand_url(expected_source_url):
            errors.append("invalid_expected_qcc_brand_url")
        elif source_url != expected_source_url:
            errors.append("explicit_qcc_brand_url_mismatch")

    expected_registration = str(trademark.get("registration_number") or "").strip()
    expected_name = str(trademark.get("name") or "").strip()
    expected_owner = str(trademark.get("owner") or "").strip()
    if str(record.get("registration_number") or "").strip() != expected_registration:
        errors.append("registration_number_mismatch")
    if normalize(record.get("name")) != normalize(expected_name):
        errors.append("mark_name_mismatch")
    if expected_owner and normalize(record.get("owner")) != normalize(expected_owner):
        errors.append("owner_mismatch")

    html_mode = str(record.get("html_retrieval_mode") or "")
    image_mode = str(record.get("image_retrieval_mode") or "")
    if allow_supplied_file:
        allowed_html_modes = ALLOWED_HTML_MODES | {"supplied_file"}
        allowed_image_modes = ALLOWED_IMAGE_MODES | {"supplied_file"}
    else:
        allowed_html_modes = ALLOWED_HTML_MODES
        allowed_image_modes = ALLOWED_IMAGE_MODES
    if html_mode not in allowed_html_modes:
        errors.append("untrusted_html_retrieval_mode")
    if image_mode not in allowed_image_modes:
        errors.append("untrusted_image_retrieval_mode")

    html_path = resolve_run_file(run_dir, record.get("html_file"), "qcc_html", errors)
    image_path = resolve_run_file(run_dir, record.get("image_file"), "qcc_image", errors)
    for path, field, label in (
        (html_path, "html_sha256", "qcc_html"),
        (image_path, "image_sha256", "qcc_image"),
    ):
        expected_hash = str(record.get(field) or "")
        if not HEX_SHA256.fullmatch(expected_hash):
            errors.append(f"invalid_{label}_sha256")
        elif path and sha256(path).lower() != expected_hash.lower():
            errors.append(f"{label}_sha256_mismatch")

    is_live = html_mode == "live_cdp_rendered_dom" or image_mode == "live_cdp_browser_request"
    if is_live and not (
        html_mode == "live_cdp_rendered_dom" and image_mode == "live_cdp_browser_request"
    ):
        errors.append("live_qcc_retrieval_mode_pair_mismatch")
    if is_live:
        validate_live_capture_provenance(
            run_dir,
            record,
            html_path,
            image_path,
            expected_registration,
            expected_name,
            expected_owner,
            errors,
        )

    if html_path:
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
        for value, label in (
            (expected_registration, "registration"),
            (expected_name, "mark"),
            (expected_owner, "owner"),
        ):
            escaped = json.dumps(value, ensure_ascii=True)[1:-1] if value else ""
            if value and normalize(value) not in normalize(html_text) and escaped not in html_text:
                errors.append(f"qcc_html_missing_{label}")

    if image_path:
        if Image is None:
            errors.append("pillow_unavailable")
        else:
            try:
                with Image.open(image_path) as image:
                    image_format = str(image.format or "").lower()
                suffix = image_path.suffix.lower()
                expected_suffixes = {"jpeg": {".jpg", ".jpeg"}, "png": {".png"}}
                if image_format not in expected_suffixes or suffix not in expected_suffixes[image_format]:
                    errors.append("qcc_image_extension_mismatch")
            except Exception:
                errors.append("qcc_image_unreadable")

    reference = read_json(run_dir / "reference" / "reference.json")
    embedded = reference.get("visual_reference") or {}
    if embedded != record:
        errors.append("reference_visual_record_mismatch")

    return {
        "schema_version": "1.0",
        "record_type": "qcc_reference_validation",
        "ok": not errors,
        "run_dir": str(run_dir),
        "source_url": source_url or None,
        "expected_source_url": expected_source_url or None,
        "html_retrieval_mode": html_mode or None,
        "image_retrieval_mode": image_mode or None,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate QCC visual-reference provenance")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--allow-supplied-file", action="store_true", help="Tests/offline fixtures only")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = validate_qcc_reference(Path(args.run_dir), allow_supplied_file=args.allow_supplied_file)
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 3)


if __name__ == "__main__":
    main()
