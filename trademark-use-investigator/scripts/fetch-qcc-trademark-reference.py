#!/usr/bin/env python3
"""Resolve a QCC trademark detail page and save its exact mark image as reference."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import html as html_module
import json
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
QCC_BRAND_PATH = re.compile(r"^/brandDetail/[a-f0-9]{32}\.html$", re.I)
HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$", re.I)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def supplied_image_content_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    raise ValueError("Supplied QCC image must use .png, .jpg or .jpeg")


def fetch(url: str, referer: str | None = None) -> tuple[bytes, str]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        return response.read(), str(response.headers.get("Content-Type") or "")


def clean_yahoo_redirect(url: str) -> str:
    match = re.search(r"/RU=([^/]+)/RK=", html_module.unescape(url))
    return unquote(match.group(1)) if match else html_module.unescape(url)


def discover_brand_url(registration: str, mark: str) -> str:
    query = quote(f"site:qcc.com {registration} {mark} 商标")
    payload, _ = fetch(f"https://search.yahoo.com/search?p={query}")
    page = payload.decode("utf-8", errors="replace")
    urls = re.findall(r'href="([^"]*(?:qcc\.com|search\.yahoo\.com)[^"]*)"', page, flags=re.I)
    candidates = []
    for raw in urls:
        candidate = clean_yahoo_redirect(raw)
        if re.fullmatch(r"https://www\.qcc\.com/brandDetail/[a-f0-9]{32}\.html", candidate, flags=re.I):
            candidates.append(candidate)
    candidates.extend(re.findall(r"https://www\.qcc\.com/brandDetail/[a-f0-9]{32}\.html", page, flags=re.I))
    for candidate in dict.fromkeys(candidates):
        try:
            payload, _ = fetch(candidate, "https://www.qcc.com/web_searchBrand")
            parsed = parse_brand(payload.decode("utf-8", errors="replace"))
            if (
                parsed.get("registration_number") == registration
                and str(parsed.get("name") or "").replace(" ", "") == mark.replace(" ", "")
            ):
                return candidate
        except Exception:
            continue
    raise ValueError("QCC trademark detail URL was not found in the public index")


def decode_json_string(raw: str) -> str:
    return json.loads(f'"{raw}"')


def parse_brand(html: str) -> dict:
    marker = '"brandEntity"'
    start = html.find(marker)
    sample = html[start:] if start >= 0 else html

    def value(key: str, required: bool = True) -> str | None:
        match = re.search(rf'"{re.escape(key)}":"((?:\\.|[^"\\])*)"', sample)
        if not match:
            if required:
                raise ValueError(f"QCC trademark field is missing: {key}")
            return None
        return decode_json_string(match.group(1))

    image = value("ImageUrl")
    return {
        "registration_number": value("RegNo"),
        "name": value("Name"),
        "owner": value("ApplicantCn"),
        "image_url": image,
        "has_image": '"HasImage":true' in sample[:30000],
    }


def is_exact_qcc_brand_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "www.qcc.com"
        and parsed.port is None
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


def resolve_live_file(run_dir: Path, raw: object, label: str) -> Path:
    value = str(raw or "").strip()
    if not value or Path(value).is_absolute():
        raise ValueError(f"QCC live metadata {label} must be a RUN-relative path")
    resolved = (run_dir / value).resolve()
    if not resolved.is_relative_to(run_dir) or not resolved.is_file():
        raise ValueError(f"QCC live metadata {label} is missing or outside the RUN")
    return resolved


def parse_live_metadata(
    path: Path,
    brand_url: str,
    *,
    run_dir: Path,
    html_file: Path,
    image_file: Path,
    expected_registration: str,
    expected_name: str,
    expected_owner: str,
) -> tuple[dict, dict]:
    """Verify the complete provenance envelope from the attached rendered QCC tab."""
    path = path.resolve()
    run_dir = run_dir.resolve()
    if not path.is_relative_to(run_dir) or not path.is_file():
        raise ValueError("QCC live metadata file must be inside the RUN")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1.0" or payload.get("record_type") != "qcc_live_dom_capture":
        raise ValueError("QCC live metadata has an unsupported schema or record type")
    source_url = str(payload.get("source_url") or "").strip()
    final_url = str(payload.get("final_url") or "").strip()
    if not is_exact_qcc_brand_url(brand_url):
        raise ValueError("--brand-url is not an exact canonical QCC brandDetail URL")
    if source_url != brand_url or final_url != brand_url:
        raise ValueError("QCC live metadata source/final URLs do not exactly match --brand-url")

    captured_at = str(payload.get("captured_at") or "").strip()
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("QCC live metadata captured_at is not an ISO-8601 timestamp") from error
    if captured.tzinfo is None:
        raise ValueError("QCC live metadata captured_at must include a timezone")

    if payload.get("cdp_attach_mode") is not True:
        raise ValueError("QCC live metadata does not prove CDP attachment mode")
    cdp_endpoint = str(payload.get("cdp_endpoint") or "").strip()
    if payload.get("cdp_endpoint_loopback") is not True or not is_loopback_cdp_endpoint(cdp_endpoint):
        raise ValueError("QCC live metadata does not prove an HTTP(S) loopback CDP endpoint")
    expected_browser = str(payload.get("expected_browser_product") or "").strip().casefold()
    detected_browser = str(payload.get("cdp_browser") or "").strip()
    if expected_browser not in {"edge", "chrome"}:
        raise ValueError("QCC live metadata expected browser product must be edge or chrome")
    if payload.get("browser_product_validated") is not True or not browser_product_matches(
        expected_browser, detected_browser
    ):
        raise ValueError("QCC live metadata browser product validation is missing or inconsistent")

    identity_validation = payload.get("identity_validation") or {}
    required_identity_flags = {
        "exact_brand_url", "source_final_url_match", "registration_number", "mark_name", "owner"
    }
    if payload.get("identity_validated") is not True or any(
        identity_validation.get(field) is not True for field in required_identity_flags
    ):
        raise ValueError("QCC live metadata identity validation flags are incomplete")
    parsed = {
        "registration_number": str(payload.get("registration_number") or "").strip(),
        "name": str(payload.get("name") or "").strip(),
        "owner": str(payload.get("owner") or "").strip(),
        "image_url": str(payload.get("image_url") or "").strip(),
        "has_image": bool(payload.get("has_image")),
    }
    missing = [key for key, value in parsed.items() if key != "has_image" and not value]
    if missing:
        raise ValueError(f"QCC live metadata fields are missing: {', '.join(missing)}")
    if not parsed["image_url"].startswith("https://trademark-img.qcc.com/"):
        raise ValueError("QCC live metadata image URL is not a QCC trademark image")
    if parsed["registration_number"] != expected_registration:
        raise ValueError("QCC live metadata registration number does not match the RUN")
    if normalize_identity(parsed["name"]) != normalize_identity(expected_name):
        raise ValueError("QCC live metadata mark name does not match the RUN")
    if expected_owner and normalize_identity(parsed["owner"]) != normalize_identity(expected_owner):
        raise ValueError("QCC live metadata owner does not match the RUN")

    metadata_html = resolve_live_file(run_dir, payload.get("html_file"), "html_file")
    metadata_image = resolve_live_file(run_dir, payload.get("image_file"), "image_file")
    if metadata_html != html_file.resolve() or metadata_image != image_file.resolve():
        raise ValueError("QCC live metadata files do not exactly match --html-file/--image-file")
    for actual, hash_field, bytes_field, label in (
        (metadata_html, "html_sha256", "html_bytes", "HTML"),
        (metadata_image, "image_sha256", "image_bytes", "image"),
    ):
        expected_hash = str(payload.get(hash_field) or "").strip().casefold()
        expected_bytes = payload.get(bytes_field)
        if not HEX_SHA256.fullmatch(expected_hash):
            raise ValueError(f"QCC live metadata {label} SHA-256 is invalid")
        if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes <= 0:
            raise ValueError(f"QCC live metadata {label} byte count is invalid")
        if actual.stat().st_size != expected_bytes:
            raise ValueError(f"QCC live metadata {label} byte count does not match the file")
        if sha256(actual).casefold() != expected_hash:
            raise ValueError(f"QCC live metadata {label} SHA-256 does not match the file")

    html_text = metadata_html.read_text(encoding="utf-8", errors="replace")
    for expected, label in (
        (expected_registration, "registration number"),
        (expected_name, "mark name"),
        (expected_owner, "owner"),
    ):
        if expected and normalize_identity(expected) not in normalize_identity(html_text):
            raise ValueError(f"QCC live HTML does not contain the expected {label}")

    provenance = {
        "schema_version": "1.0",
        "record_type": "qcc_live_capture_provenance",
        "metadata_file": str(path.relative_to(run_dir)).replace("\\", "/"),
        "metadata_sha256": sha256(path),
        "source_url": source_url,
        "final_url": final_url,
        "captured_at": captured_at,
        "cdp_attach_mode": True,
        "cdp_endpoint": cdp_endpoint,
        "cdp_endpoint_loopback": True,
        "cdp_browser": detected_browser,
        "expected_browser_product": expected_browser,
        "browser_product_validated": True,
        "identity_validated": True,
        "identity_validation": {field: True for field in sorted(required_identity_flags)},
        "html_file": str(metadata_html.relative_to(run_dir)).replace("\\", "/"),
        "html_bytes": metadata_html.stat().st_size,
        "html_sha256": sha256(metadata_html),
        "image_file": str(metadata_image.relative_to(run_dir)).replace("\\", "/"),
        "image_bytes": metadata_image.stat().st_size,
        "image_sha256": sha256(metadata_image),
    }
    return parsed, provenance


def normalize_identity(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def write_failure_diagnostic(
    run_dir: Path,
    *,
    brand_url: str,
    html_bytes: bytes,
    retrieval_mode: str,
    error: Exception,
) -> Path:
    diagnostic_dir = run_dir / "capture-diagnostics" / "QCC" / "reference-fetch"
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    response_path = diagnostic_dir / "qcc-response.html"
    response_path.write_bytes(html_bytes)
    record = {
        "schema_version": "1.0",
        "record_type": "qcc_reference_fetch_failure",
        "source_url": brand_url,
        "retrieval_mode": retrieval_mode,
        "response_file": str(response_path.relative_to(run_dir)).replace("\\", "/"),
        "response_sha256": sha256(response_path),
        "error_type": type(error).__name__,
        "error": str(error),
    }
    record_path = diagnostic_dir / "failure.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record_path


def assisted_live_resolution_pending(config: dict, *, brand_url: str | None, supplied_inputs: bool) -> bool:
    """Avoid public-index discovery when CherryStudio already owns a live browser path."""
    orchestration = config.get("cherrystudio_orchestration") or {}
    return bool(
        orchestration.get("workflow_mode") == "free_assisted_browser"
        and not str(brand_url or "").strip()
        and not supplied_inputs
    )


def emit_pending_live_resolution() -> None:
    print(json.dumps({
        "schema_version": "1.0",
        "record_type": "qcc_reference_fetch_status",
        "status": "pending_live_browser_resolution",
        "source_url": None,
        "network_request_performed": False,
        "reason": "No exact QCC brandDetail URL was supplied for the assisted-browser RUN",
        "next_action": (
            "Open the QCC trademark search page in the dedicated browser and use "
            "cherrystudio_orchestrator.py resume to resolve and capture the exact brandDetail via CDP"
        ),
        "manual_record_construction_allowed": False,
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch exact trademark appearance from QCC")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--brand-url")
    parser.add_argument("--html-file", help="Offline fixture or user-supplied QCC HTML")
    parser.add_argument("--image-file", help="Offline fixture matching --html-file")
    parser.add_argument(
        "--metadata-file",
        help="Metadata emitted by capture-qcc-reference-from-cdp.mjs for a rendered QCC tab",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    config_path = run_dir / "run-config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"run-config.json not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    trademark = config.get("trademark") or {}
    registration = str(trademark.get("registration_number") or "").strip()
    mark = str(trademark.get("name") or "").strip()
    owner = str(trademark.get("owner") or "").strip()
    if not registration or not mark:
        raise ValueError("run config must contain trademark name and registration number")

    html_input = Path(args.html_file).resolve() if args.html_file else None
    image_input = Path(args.image_file).resolve() if args.image_file else None
    metadata_input = Path(args.metadata_file).resolve() if args.metadata_file else None
    if metadata_input and (not html_input or not image_input):
        raise ValueError("--metadata-file requires both --html-file and --image-file")
    if assisted_live_resolution_pending(
        config,
        brand_url=args.brand_url,
        supplied_inputs=bool(html_input or image_input or metadata_input),
    ):
        emit_pending_live_resolution()
        raise SystemExit(6)

    brand_url = args.brand_url or discover_brand_url(registration, mark)
    html_retrieval_mode = "live_cdp_rendered_dom" if metadata_input else ("supplied_file" if html_input else "network")
    if html_input:
        html_bytes = html_input.read_bytes()
    else:
        html_bytes, _ = fetch(brand_url, "https://www.qcc.com/web_searchBrand")
    html_text = html_bytes.decode("utf-8", errors="replace")
    live_capture_provenance = None
    try:
        if metadata_input:
            parsed, live_capture_provenance = parse_live_metadata(
                metadata_input,
                brand_url,
                run_dir=run_dir,
                html_file=html_input,
                image_file=image_input,
                expected_registration=registration,
                expected_name=mark,
                expected_owner=owner,
            )
        else:
            parsed = parse_brand(html_text)
    except Exception as error:
        write_failure_diagnostic(
            run_dir,
            brand_url=brand_url,
            html_bytes=html_bytes,
            retrieval_mode=html_retrieval_mode,
            error=error,
        )
        if (
            metadata_input is None
            and html_input is None
            and isinstance(error, ValueError)
            and "QCC trademark field is missing" in str(error)
        ):
            print(json.dumps({
                "schema_version": "1.0",
                "record_type": "qcc_reference_fetch_status",
                "status": "live_cdp_required",
                "source_url": brand_url,
                "reason": str(error),
                "next_action": (
                    "Use cherrystudio_orchestrator.py resume for the same RUN; "
                    "it will capture the logged-in QCC tab through the locked loopback CDP browser"
                ),
                "manual_record_construction_allowed": False,
            }, ensure_ascii=False, indent=2))
            raise SystemExit(6)
        raise
    if parsed["registration_number"] != registration:
        raise ValueError("QCC registration number does not match the investigation")
    if parsed["name"].replace(" ", "") != mark.replace(" ", ""):
        raise ValueError("QCC mark name does not match the investigation")
    if owner and parsed["owner"] != owner:
        raise ValueError("QCC applicant does not match the investigation owner")
    if not parsed["has_image"] or not parsed["image_url"]:
        raise ValueError("QCC record has no trademark image")

    image_retrieval_mode = "live_cdp_browser_request" if metadata_input else ("supplied_file" if image_input else "network")
    if image_input:
        image_bytes = image_input.read_bytes()
        content_type = supplied_image_content_type(image_input)
    else:
        image_bytes, content_type = fetch(parsed["image_url"], brand_url)
    suffix = ".png" if "png" in content_type.lower() else ".jpg"
    reference_dir = run_dir / "reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    html_path = reference_dir / "qcc-brand-detail.html"
    image_path = reference_dir / f"qcc-trademark-{registration}{suffix}"
    html_path.write_bytes(html_bytes)
    image_path.write_bytes(image_bytes)

    record = {
        "schema_version": "1.0",
        "record_type": "qcc_trademark_visual_reference",
        "source_url": brand_url,
        "registration_number": parsed["registration_number"],
        "name": parsed["name"],
        "owner": parsed["owner"],
        "image_url": parsed["image_url"],
        "html_retrieval_mode": html_retrieval_mode,
        "image_retrieval_mode": image_retrieval_mode,
        "html_file": str(html_path.relative_to(run_dir)).replace("\\", "/"),
        "html_bytes": html_path.stat().st_size,
        "html_sha256": sha256(html_path),
        "image_file": str(image_path.relative_to(run_dir)).replace("\\", "/"),
        "image_bytes": image_path.stat().st_size,
        "image_sha256": sha256(image_path),
        "visual_reference_primary": True,
    }
    if live_capture_provenance:
        record["live_capture_provenance"] = live_capture_provenance
    if html_input or image_input or metadata_input:
        record["supplied_input_provenance"] = {
            "html_source": str(html_input) if html_input else None,
            "html_source_sha256": sha256(html_input) if html_input else None,
            "image_source": str(image_input) if image_input else None,
            "image_source_sha256": sha256(image_input) if image_input else None,
            "metadata_source": str(metadata_input) if metadata_input else None,
            "metadata_source_sha256": sha256(metadata_input) if metadata_input else None,
        }
    record_path = reference_dir / "qcc-reference.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    reference_path = reference_dir / "reference.json"
    reference = json.loads(reference_path.read_text(encoding="utf-8")) if reference_path.is_file() else {}
    reference["analysis_status"] = "complete"
    reference["visual_reference"] = record
    reference_path.write_text(json.dumps(reference, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "qcc_reference_complete": True,
        "source_url": brand_url,
        "image_file": str(image_path),
        "html_file": str(html_path),
        "record": str(record_path),
        "html_retrieval_mode": html_retrieval_mode,
        "image_retrieval_mode": image_retrieval_mode,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
