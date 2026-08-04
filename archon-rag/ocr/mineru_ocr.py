"""MinerU online OCR integration for image-heavy PDFs.

Token is read from MINERU_API_TOKEN at runtime. Never store the token in
source files or generated documents.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import urllib.request
import urllib.error
import uuid
import zipfile
from pathlib import Path


def _base_url() -> str:
    return os.environ.get("MINERU_API_BASE", "https://mineru.net").rstrip("/")


def _token() -> str:
    return os.environ.get("MINERU_API_TOKEN", "")


def _request_json(method: str, url: str, headers: dict, payload=None) -> dict:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_image_pdf(filepath: str, min_chars_per_page: int = 20) -> bool:
    """Return True when a PDF has very little extractable text."""
    try:
        import fitz
    except Exception:
        return False
    try:
        doc = fitz.open(filepath)
        if not doc:
            return False
        low_text_pages = 0
        for page in doc:
            if len(page.get_text().strip()) < min_chars_per_page:
                low_text_pages += 1
        return low_text_pages >= max(1, len(doc) // 2)
    except Exception:
        return False


def mineru_parse_pdf(
    filepath: str,
    token: str = "",
    model_version: str = "",
    poll_interval: float = 5.0,
    timeout: float = 600.0,
) -> dict:
    """Submit one PDF to MinerU and return the parsed markdown."""
    token = token or _token()
    if not token:
        return {"status": "error", "message": "MINERU_API_TOKEN is not set"}
    model_version = model_version or os.environ.get("MINERU_MODEL_VERSION", "vlm")
    filename = Path(filepath).name
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {
        "files": [{"name": filename, "data_id": str(uuid.uuid4())}],
        "model_version": model_version,
    }

    try:
        submitted = _request_json(
            "POST",
            f"{_base_url()}/api/v4/file-urls/batch",
            headers,
            payload,
        )
    except Exception as exc:
        return {"status": "error", "message": f"submit failed: {exc}"}

    if submitted.get("code") != 0:
        return {"status": "error", "message": f"submit rejected: {submitted.get('msg', submitted)}"}
    batch_id = submitted.get("data", {}).get("batch_id", "")
    file_urls = submitted.get("data", {}).get("file_urls", [])
    if not batch_id or not file_urls:
        return {"status": "error", "message": "missing batch_id or file_urls in MinerU response"}

    try:
        with open(filepath, "rb") as f:
            file_bytes = f.read()
        upload_headers = {"Authorization": f"Bearer {token}"}
        req = urllib.request.Request(file_urls[0], data=file_bytes, headers=upload_headers, method="PUT")
        with urllib.request.urlopen(req, timeout=300) as resp:
            if resp.status not in (200, 201, 204):
                return {"status": "error", "message": f"upload failed: {resp.status}"}
    except Exception as exc:
        return {"status": "error", "message": f"upload failed: {exc}"}

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            result = _request_json(
                "GET",
                f"{_base_url()}/api/v4/extract-results/batch/{batch_id}",
                {"Authorization": f"Bearer {token}", "Accept": "*/*"},
            )
        except Exception as exc:
            continue
        data = result.get("data", {})
        item = data.get("extract_result") or (data.get("extract_results") or [{}])[0]
        state = item.get("state", "")
        if state == "done":
            zip_url = item.get("full_zip_url", "")
            return _download_markdown(zip_url, filename, token)
        if state in ("failed", "error"):
            return {"status": "error", "message": item.get("err_msg", "MinerU parse failed"), "state": state}

    return {"status": "error", "message": "MinerU parse timed out"}


def _download_markdown(zip_url: str, filename: str, token: str) -> dict:
    if not zip_url:
        return {"status": "error", "message": "MinerU done but missing zip url"}
    try:
        req = urllib.request.Request(zip_url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            zip_bytes = resp.read()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            md_name = next((n for n in names if n.endswith("full.md")), None)
            if not md_name:
                return {"status": "error", "message": "MinerU zip has no full.md", "files": names[:10]}
            markdown = zf.read(md_name).decode("utf-8", errors="ignore")
        return {"status": "success", "markdown": markdown, "source_filename": filename}
    except Exception as exc:
        return {"status": "error", "message": f"download/extract failed: {exc}"}


def ocr_pdf_if_needed(filepath: str, min_text_chars: int = 300) -> dict:
    """Parse a PDF locally; fall back to MinerU when it looks like a scan."""
    text_chars = 0
    try:
        import fitz
        with fitz.open(filepath) as doc:
            text_chars = sum(len(page.get_text().strip()) for page in doc)
    except Exception:
        pass
    if text_chars >= min_text_chars and not is_image_pdf(filepath):
        return {"status": "skipped", "used_ocr": False}
    return mineru_parse_pdf(filepath)
