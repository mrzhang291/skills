#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fs_safety import (
    TRANSACTION_DIR_NAME,
    assert_safe_tree,
    audit_run_tree,
    is_reparse_point,
)
from provenance_utils import file_sha256, validate_provider
from process_utils import run_bounded
from qcc_reference_guard import validate_qcc_reference
from url_utils import (
    hostname,
    is_search_result_url,
    normalize_url,
    require_safe_file_id,
    site_key,
)


FORMAL_OUTPUTS = (
    "coverage-matrix.json",
    "capture-order.json",
    "evidence-binder.pdf",
    "evidence-binder.build.json",
    "skipped-links.json",
    "results.json",
    "report.md",
    "manifest.json",
    "validation.json",
)
TRANSIENT_OUTPUTS = ("evidence-binder.pdf.tmp",)
REQUIRED_ARCHIVE_ARTIFACTS = {
    "singlefile_html", "singlefile_raw_html", "rendered_dom", "mhtml", "fullpage", "pdf",
    "body_text", "images_index", "links_index", "offline_validation",
    "mhtml_validation", "mhtml_offline_screenshot", "archive_visual_comparison",
}

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


def resolve_skill_dir(raw: str | None, config: dict) -> Path:
    own_skill_dir = Path(__file__).resolve().parents[1]
    skill_dir = Path(raw).expanduser().resolve() if raw else own_skill_dir
    if skill_dir != own_skill_dir:
        override = os.environ.get("TRADEMARK_SKILL_TEST_OVERRIDE") == "1"
        if not (override and config.get("test_mode") is True):
            raise ValueError(
                "--skill-dir is production-locked to the skill that owns finalize-run.py; "
                "a different test fixture requires TRADEMARK_SKILL_TEST_OVERRIDE=1 and run-config.test_mode=true"
            )
    if not skill_dir.is_dir():
        raise NotADirectoryError(f"Skill directory does not exist: {skill_dir}")
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"Skill directory lacks SKILL.md: {skill_dir}")
    header = skill_md.read_text(encoding="utf-8", errors="strict")
    if not re.search(r"(?m)^name:\s*trademark-use-investigator\s*$", header):
        raise ValueError(f"SKILL.md is not trademark-use-investigator: {skill_md}")
    for name in ("merge-evidence-pdf.py", "validate-run.py", "url_utils.py"):
        script = (skill_dir / "scripts" / name).resolve()
        if not script.is_relative_to(skill_dir) or not script.is_file():
            raise FileNotFoundError(f"Required skill script is missing: {script}")
    return skill_dir


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Windows commonly refuses opening a directory handle through os.open.
        pass


def _durable_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _durable_copy(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    with destination.open("r+b") as stream:
        os.fsync(stream.fileno())
    shutil.copystat(source, destination)
    _fsync_directory(destination.parent)
    return sha256(destination)


def capture_invocation_count(attempts: list[dict]) -> int:
    """Count every invocation for audit only; retries do not increase coverage."""
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


def validate_discovery_provider_runs(run_dir: Path, config: dict, queries: list[dict]) -> list[str]:
    successful: set[str] = set()
    raw_claims: dict[str, set[str]] = {}
    for item in queries:
        provider, expected_key = validate_provider(
            str(item.get("provider") or ""),
            source_kind=str(item.get("source_kind") or ""),
            test_mode=config.get("test_mode") is True,
            provider_instance_id=item.get("provider_instance_id"),
        )
        if item.get("provider_key") != expected_key:
            raise ValueError(f"Discovery provider_key is inconsistent: {item.get('query_id')}/{provider}")
        raw_rel = item.get("raw_file")
        raw_path = (run_dir / str(raw_rel or "__missing__")).resolve()
        raw_root = (run_dir / "discovery" / "raw").resolve()
        if item.get("state") in {"normal", "zero_results"}:
            if not raw_rel or not raw_path.is_relative_to(raw_root) or not raw_path.is_file():
                raise ValueError(f"Successful discovery run lacks raw evidence: {item.get('query_id')}/{provider}")
            actual = file_sha256(raw_path)
            if item.get("raw_sha256") != actual:
                raise ValueError(f"Discovery raw response hash mismatch: {item.get('query_id')}/{provider}")
            successful.add(provider)
            raw_claims.setdefault(actual, set()).add(provider)
    duplicate = {digest: values for digest, values in raw_claims.items() if len(values) > 1}
    if duplicate:
        raise ValueError(f"Raw discovery payload reused across providers: {duplicate}")
    return sorted(successful)


def reject_sensitive_inputs(run_dir: Path, records: list[dict]) -> None:
    """Fail before manifest creation if private browser inputs entered RUN_DIR."""
    for record in records:
        label = record.get("candidate_id") or record.get("source_id") or "unknown"
        for item in record.get("sensitive_inputs") or []:
            if not isinstance(item, dict) or item.get("inside_run_dir") is not False:
                raise ValueError(f"Sensitive input is inside RUN_DIR or lacks outside-run provenance: {label}")
            for key in ("path", "absolute_path", "resolved_path"):
                if item.get(key):
                    candidate = Path(str(item[key])).expanduser().resolve()
                    if candidate == run_dir or candidate.is_relative_to(run_dir):
                        raise ValueError(f"Sensitive input path is inside RUN_DIR: {label}")


class FormalOutputTransaction:
    """Crash-recoverable journal for a multi-file (not single-atomic) commit."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.transaction_dir = run_dir / TRANSACTION_DIR_NAME
        self.backup_dir = self.transaction_dir / "backups"
        self.journal_path = self.transaction_dir / "journal.json"
        self.preexisting: set[str] = set()
        self.backups: dict[str, str] = {}
        self.validation_nonce: str | None = None
        self.committed = False

    @staticmethod
    def recover_stale(run_dir: Path) -> bool:
        transaction_dir = run_dir / TRANSACTION_DIR_NAME
        if not transaction_dir.exists() and not is_reparse_point(transaction_dir):
            return False
        if is_reparse_point(transaction_dir) or not transaction_dir.is_dir():
            raise ValueError(f"Unsafe finalization transaction path: {transaction_dir}")
        assert_safe_tree(transaction_dir, run_dir, "finalization transaction")
        journal_path = transaction_dir / "journal.json"
        if not journal_path.is_file():
            raise ValueError("Unfinished finalization transaction has no recovery journal")
        journal = read_json(journal_path, {}) or {}
        if journal.get("formal_outputs") != list(FORMAL_OUTPUTS):
            raise ValueError("Unfinished finalization journal has an unexpected output set")
        phase = journal.get("phase")
        if phase in {"committed", "preparing"}:
            # Preparing never mutates formal outputs. Committed means validation
            # passed and only cleanup was interrupted.
            shutil.rmtree(transaction_dir)
            _fsync_directory(run_dir)
            return True
        if phase not in {"ready", "mutating", "validated"}:
            raise ValueError(f"Unrecognized finalization transaction phase: {phase!r}")
        preexisting = journal.get("preexisting") or []
        backups = journal.get("backups") or {}
        if (
            not isinstance(preexisting, list)
            or not all(item in FORMAL_OUTPUTS for item in preexisting)
            or set(backups) != set(preexisting)
        ):
            raise ValueError("Unfinished finalization journal has an incomplete backup set")
        backup_root = transaction_dir / "backups"
        for rel in preexisting:
            backup = backup_root / rel
            if (
                is_reparse_point(backup)
                or not backup.is_file()
                or not backup.resolve(strict=True).is_relative_to(transaction_dir.resolve(strict=True))
                or sha256(backup) != backups[rel]
            ):
                raise ValueError(f"Finalization recovery backup is missing or corrupt: {rel}")
        for rel in (*FORMAL_OUTPUTS, *TRANSIENT_OUTPUTS):
            destination = run_dir / rel
            if is_reparse_point(destination):
                raise ValueError(f"Cannot recover over a reparse-backed formal output: {destination}")
            if destination.exists():
                if not destination.is_file():
                    raise ValueError(f"Cannot recover over a non-file formal output: {destination}")
                destination.unlink()
        for rel in preexisting:
            _durable_copy(backup_root / rel, run_dir / rel)
        _fsync_directory(run_dir)
        shutil.rmtree(transaction_dir)
        _fsync_directory(run_dir)
        return True

    def _write_journal(self, phase: str) -> None:
        _durable_json(self.journal_path, {
            "schema_version": "2.0",
            "phase": phase,
            "formal_outputs": list(FORMAL_OUTPUTS),
            "transient_outputs": list(TRANSIENT_OUTPUTS),
            "preexisting": sorted(self.preexisting),
            "backups": dict(sorted(self.backups.items())),
            "validation_nonce": self.validation_nonce,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def __enter__(self):
        self.recover_stale(self.run_dir)
        try:
            for rel in FORMAL_OUTPUTS:
                source = self.run_dir / rel
                if is_reparse_point(source):
                    raise ValueError(f"Formal output cannot be a reparse point: {source}")
                if source.exists() and not source.is_file():
                    raise ValueError(f"Formal output path is not a file: {source}")
                if source.is_file():
                    self.preexisting.add(rel)
            self.backup_dir.mkdir(parents=True, exist_ok=False)
            self._write_journal("preparing")
            for rel in sorted(self.preexisting):
                self.backups[rel] = _durable_copy(self.run_dir / rel, self.backup_dir / rel)
                self._write_journal("preparing")
            self._write_journal("ready")
            return self
        except Exception:
            shutil.rmtree(self.transaction_dir, ignore_errors=True)
            raise

    def begin_mutation(self) -> None:
        self._write_journal("mutating")

    def bind_validation_nonce(self, nonce: str) -> None:
        self.validation_nonce = nonce
        self._write_journal("ready")

    def mark_validated(self) -> None:
        self._write_journal("validated")

    def commit(self) -> None:
        self._write_journal("committed")
        self.committed = True

    def _restore(self) -> None:
        for rel in (*FORMAL_OUTPUTS, *TRANSIENT_OUTPUTS):
            destination = self.run_dir / rel
            if is_reparse_point(destination):
                raise ValueError(f"Cannot restore over a reparse-backed formal output: {destination}")
            if destination.is_file():
                destination.unlink()
        for rel in self.preexisting:
            source = self.backup_dir / rel
            destination = self.run_dir / rel
            if sha256(source) != self.backups[rel]:
                raise ValueError(f"Finalization rollback backup is corrupt: {rel}")
            _durable_copy(source, destination)
        _fsync_directory(self.run_dir)

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is not None or not self.committed:
                self._restore()
        finally:
            shutil.rmtree(self.transaction_dir, ignore_errors=True)
            _fsync_directory(self.run_dir)
        return False


def load_discovery(run_dir: Path) -> tuple[list[dict], list[dict], dict]:
    queries = read_jsonl(run_dir / "discovery" / "queries.jsonl")
    results = read_jsonl(run_dir / "discovery" / "results.jsonl")
    frontier = read_json(run_dir / "discovery" / "url-frontier.json", {}) or {}
    if not queries:
        raise ValueError("No discovery provider runs were recorded")
    for record in queries:
        raw = record.get("raw_file")
        if raw and not (run_dir / raw).is_file():
            raise ValueError(f"Discovery raw response is missing: {raw}")
    for item in results:
        if is_search_result_url(item.get("normalized_url") or ""):
            raise ValueError(f"Discovery result still points to a search page: {item.get('normalized_url')}")
    return queries, results, frontier


def load_sources(run_dir: Path) -> list[dict]:
    sources = []
    source_root = run_dir / "source-pages"
    candidate_root = run_dir / "candidate-pages"
    assert_safe_tree(source_root, run_dir, "source-pages")
    assert_safe_tree(candidate_root, run_dir, "candidate-pages")
    review_root = run_dir / "visual-reviews"
    if review_root.exists():
        assert_safe_tree(review_root, run_dir, "visual-reviews")
    for metadata_path in source_root.glob("*/metadata.json"):
        metadata = read_json(metadata_path, {}) or {}
        source_dir = metadata_path.parent.resolve(strict=True)
        if not source_dir.is_relative_to(source_root.resolve(strict=True)):
            raise ValueError(f"Formal source directory escapes source-pages: {metadata_path.parent}")
        pdf_record = (metadata.get("artifacts") or {}).get("pdf") or {}
        pdf = (source_dir / pdf_record.get("path", "__missing__")).resolve(strict=False)
        metadata["_dir"] = source_dir
        metadata["_pdf"] = pdf if pdf.is_relative_to(source_dir) and pdf.is_file() else None
        sources.append(metadata)
    if not sources:
        raise ValueError("No promoted target pages exist; a search-page or conclusion-only binder is forbidden")

    source_ids: set[str] = set()
    candidate_ids: set[str] = set()
    orders: set[int] = set()
    canonical_urls: set[str] = set()
    for item in sources:
        source_id = item.get("source_id")
        origin = item.get("origin_candidate_id")
        order = item.get("order")
        source_id = require_safe_file_id(source_id, "source-id")
        origin = require_safe_file_id(origin, "origin-candidate-id")
        if item["_dir"].name != source_id:
            raise ValueError(f"Formal source directory/metadata ID mismatch: {item['_dir'].name}/{source_id}")
        url = item.get("final_url") or item.get("normalized_url") or item.get("requested_url") or ""
        canonical_url = normalize_url(url)
        if item.get("record_type") != "target_page":
            raise ValueError(f"Formal source is not a promoted target_page: {source_id}")
        if item.get("evidence_accepted") is not True or item.get("content_valid") is not True:
            raise ValueError(f"Formal source is invalid or unpromoted: {source_id}")
        if item.get("search_result_page") or is_search_result_url(url):
            raise ValueError(f"Search-result page is present in formal evidence: {source_id}")
        if not (item.get("offline_replay") or {}).get("ok"):
            raise ValueError(f"Formal source failed offline replay: {source_id}")
        if not item.get("_pdf"):
            raise ValueError(f"Formal source PDF is missing: {source_id}")
        if source_id.casefold() in source_ids:
            raise ValueError(f"Formal source ID is missing or duplicated: {source_id}")
        if origin.casefold() in candidate_ids:
            raise ValueError(f"Candidate was promoted more than once: {origin}")
        if type(order) is not int or order < 1 or order in orders:
            raise ValueError(f"Formal source order is missing, invalid or duplicated: {order}")
        if not canonical_url or canonical_url in canonical_urls:
            raise ValueError(f"Formal source canonical URL is missing or duplicated: {canonical_url or url}")
        source_ids.add(source_id.casefold())
        candidate_ids.add(origin.casefold())
        orders.add(order)
        canonical_urls.add(canonical_url)
    sources.sort(key=lambda item: (item["order"], item["source_id"].casefold()))
    return sources


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
    """Build a deterministic, non-evidentiary list of URLs omitted from the PDF."""
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


def coverage(run_dir: Path, config: dict, queries: list[dict], frontier: dict, sources: list[dict]) -> dict:
    attempts = read_jsonl(run_dir / "capture-attempts.jsonl")
    candidate_rows: dict[str, dict] = {}
    url_rows: dict[str, dict] = {}
    history_ids: set[str] = set()
    accepted_rows: dict[str, dict] = {}
    for item in attempts:
        if not isinstance(item, dict):
            raise ValueError("capture-attempts.jsonl contains a non-object row")
        candidate_id = require_safe_file_id(item.get("candidate_id"), "capture candidate-id")
        candidate_key = candidate_id.casefold()
        attempt_url = normalize_url(item.get("url") or "")
        if not attempt_url or attempt_url != normalize_url(item.get("requested_normalized_url") or ""):
            raise ValueError(f"Capture row has an invalid or inconsistent canonical URL: {candidate_id}")
        expected_site = site_key(attempt_url)
        if not expected_site or item.get("site_key") != expected_site:
            raise ValueError(f"Capture row has an inconsistent site_key: {candidate_id}")
        if candidate_key in candidate_rows:
            raise ValueError(f"capture-attempts.jsonl contains duplicate candidate rows: {candidate_id}")
        if attempt_url in url_rows:
            raise ValueError(f"capture-attempts.jsonl contains duplicate normalized-URL rows: {attempt_url}")
        candidate_rows[candidate_key] = item
        url_rows[attempt_url] = item
        if item.get("status") == "started":
            raise ValueError(f"Capture attempt log contains a started/orphan row: {candidate_id}")
        history = item.get("attempt_history")
        if not isinstance(history, list) or not history:
            raise ValueError(f"Capture row has no durable invocation history: {candidate_id}")
        row_history_ids: set[str] = set()
        for entry in history:
            if not isinstance(entry, dict):
                raise ValueError(f"Capture history contains a non-object entry: {candidate_id}")
            invocation_id = str(entry.get("invocation_id") or entry.get("attempt_id") or "")
            if not invocation_id or invocation_id in row_history_ids or invocation_id in history_ids:
                raise ValueError(f"Capture invocation ID is missing or duplicated: {candidate_id}/{invocation_id}")
            if entry.get("invocation_id") and entry.get("attempt_id") and entry["invocation_id"] != entry["attempt_id"]:
                raise ValueError(f"Capture invocation/attempt ID mismatch: {candidate_id}/{invocation_id}")
            if entry.get("status") == "started":
                raise ValueError(f"Capture history contains a started/orphan invocation: {invocation_id}")
            row_history_ids.add(invocation_id)
            history_ids.add(invocation_id)
        if type(item.get("attempt_count")) is not int or item["attempt_count"] != len(history):
            raise ValueError(f"Capture attempt_count does not equal durable history length: {candidate_id}")
        if item.get("status") == "accepted_candidate":
            if not all(item.get(flag) is True for flag in (
                "content_valid", "offline_ok", "portable_html_ok", "visual_similarity_ok"
            )):
                raise ValueError(f"Accepted capture row lacks archive/offline success flags: {candidate_id}")
            accepted_rows[candidate_key] = item
    successful_providers = validate_discovery_provider_runs(run_dir, config, queries)
    query_ids = sorted({item.get("query_id") for item in queries if item.get("query_id")})
    successful_query_ids = {
        item.get("query_id") for item in queries
        if item.get("state") in {"normal", "zero_results"} and item.get("query_id")
    }
    failed_query_ids = sorted(set(query_ids) - successful_query_ids)
    items = frontier.get("items") or []
    frontier_urls: set[str] = set()
    target_domains: set[str] = set()
    for item in items:
        normalized = normalize_url(item.get("normalized_url") or "") if isinstance(item, dict) else None
        if not normalized or is_search_result_url(normalized):
            raise ValueError(f"Frontier contains an invalid target URL: {item!r}")
        recomputed_site = site_key(normalized)
        if item.get("site_key") not in {None, "", recomputed_site}:
            raise ValueError(f"Frontier site_key is inconsistent with its URL: {normalized}")
        frontier_urls.add(normalized)
        target_domains.add(recomputed_site)
    normal_candidate_urls: set[str] = set()
    candidate_root = run_dir / "candidate-pages"
    for metadata_path in candidate_root.glob("*/metadata.json"):
        item = read_json(metadata_path, {}) or {}
        candidate_id = require_safe_file_id(item.get("candidate_id"), "candidate metadata ID")
        candidate_key = candidate_id.casefold()
        candidate_dir = metadata_path.parent.resolve(strict=True)
        if candidate_dir.name != candidate_id or not candidate_dir.is_relative_to(candidate_root.resolve(strict=True)):
            raise ValueError(f"Candidate metadata directory/ID mismatch: {metadata_path}")
        accepted_attempt = accepted_rows.get(candidate_key)
        is_normal = (
            item.get("candidate_accepted") is True
            and item.get("content_valid") is True
            and item.get("page_state") == "normal"
        )
        if is_normal and not accepted_attempt:
            raise ValueError(f"Normal candidate has no matching accepted capture row: {candidate_id}")
        if not is_normal:
            continue
        candidate_url = normalize_url(
            item.get("normalized_url") or item.get("requested_url") or item.get("final_url") or ""
        )
        attempt_url = normalize_url(accepted_attempt.get("url") or "")
        if not candidate_url or candidate_url != attempt_url or candidate_url in normal_candidate_urls:
            raise ValueError(f"Normal candidate URL is missing, mismatched or duplicated: {candidate_id}")
        if not all((item.get(field) or {}).get("ok") is True for field in (
            "offline_replay", "singlefile_replay", "archive_visual_comparison"
        )):
            raise ValueError(f"Normal candidate lacks required offline/archive success: {candidate_id}")
        artifacts = item.get("artifacts") or {}
        missing = REQUIRED_ARCHIVE_ARTIFACTS - set(artifacts)
        if missing:
            raise ValueError(f"Normal candidate lacks required archive artifacts: {candidate_id}/{sorted(missing)}")
        for name in REQUIRED_ARCHIVE_ARTIFACTS:
            record = artifacts.get(name) or {}
            artifact_path = (candidate_dir / str(record.get("path") or "__missing__")).resolve(strict=False)
            if (
                is_reparse_point(artifact_path)
                or not artifact_path.is_relative_to(candidate_dir)
                or not artifact_path.is_file()
                or sha256(artifact_path) != record.get("sha256")
            ):
                raise ValueError(f"Normal candidate artifact is missing, escaping or corrupt: {candidate_id}/{name}")
        normal_candidate_urls.add(candidate_url)
    requirements = config.get("coverage_requirements") or {}
    execution_profile = config.get("execution_profile") or "forensic"
    if execution_profile not in {"quick", "forensic"}:
        raise ValueError(f"Unsupported execution_profile: {execution_profile!r}")
    actual = {
        "discovery_providers": len(successful_providers),
        "unique_target_urls": len(frontier_urls),
        "target_domains": len(target_domains),
        # One row is one candidate bound to one canonical URL. Retries remain in
        # attempt_history and must not let a single failing URL satisfy coverage.
        # Failed, blocked and captcha pages remain auditable attempts but do
        # not satisfy coverage. Only complete, accepted target-page archives
        # count toward the direct-capture requirement.
        "direct_capture_attempts": len(accepted_rows),
        "capture_invocations_audit": len(history_ids),
        "normal_target_pages": len(normal_candidate_urls),
        "formal_sources": len(sources),
    }
    required = {
        "discovery_providers": int(requirements.get("min_discovery_providers", 2)),
        "unique_target_urls": int(requirements.get("min_target_urls", 10)),
        "target_domains": int(requirements.get("min_target_domains", 3)),
        "direct_capture_attempts": int(requirements.get("min_capture_attempts", 8)),
        "normal_target_pages": int(requirements.get("min_normal_target_pages", 5)),
        "formal_sources": int(requirements.get("min_formal_sources", 1)),
    }
    deficits = {
        key: {"actual": actual[key], "required": value}
        for key, value in required.items() if actual[key] < value
    }
    require_each_query_success = bool(requirements.get("require_each_query_success", True))
    if require_each_query_success != (execution_profile == "forensic"):
        raise ValueError("coverage query-success policy is inconsistent with execution_profile")
    if failed_query_ids and require_each_query_success:
        deficits["queries_without_successful_provider"] = {
            "actual": len(failed_query_ids), "required": 0, "query_ids": failed_query_ids
        }
    return {
        "schema_version": "2.0", "status": "complete" if not deficits else "insufficient_coverage",
        "requirements": required, "actual": actual, "deficits": deficits,
        "provider_names": successful_providers, "domain_names": sorted(target_domains),
        "execution_profile": execution_profile,
        "query_count": len(query_ids),
        "successful_query_count": len(successful_query_ids),
        "failed_query_ids": failed_query_ids,
        "require_each_query_success": require_each_query_success,
        "formal_sources": len(sources), "formal_serp_count": 0,
        "source_roles": {
            role: sum(item.get("page_role") == role for item in sources)
            for role in ("evidence", "supporting", "exclusion_reference")
        },
    }


def write_coverage_matrix(
    run_dir: Path, query_runs: list[dict], frontier: dict, sources: list[dict], summary: dict
) -> None:
    matrix = {
        "schema_version": "2.0", "summary": summary,
        "discovery_runs": [{
            "query_id": item.get("query_id"), "query": item.get("query"), "provider": item.get("provider"),
            "state": item.get("state"), "result_count": item.get("result_count"), "raw_file": item.get("raw_file"),
        } for item in query_runs],
        "target_frontier": frontier.get("items") or [],
        "formal_sources": [{
            "source_id": item.get("source_id"), "origin_candidate_id": item.get("origin_candidate_id"),
            "page_role": item.get("page_role"), "page_type": item.get("page_type"),
            "title": item.get("title"), "url": item.get("final_url") or item.get("normalized_url"),
            "domain": hostname(item.get("final_url") or item.get("normalized_url") or ""),
            "offline_replay_ok": bool((item.get("offline_replay") or {}).get("ok")),
        } for item in sources],
    }
    (run_dir / "coverage-matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_manifest(run_dir: Path) -> None:
    excluded_names = {"manifest.json", "validation.json"}
    audit_run_tree(run_dir, allow_active_transaction=True)
    files = []
    for path in sorted(run_dir.rglob("*")):
        relative = path.relative_to(run_dir)
        if relative.parts and relative.parts[0] == TRANSACTION_DIR_NAME:
            continue
        if (
            not path.is_file()
            or path.name in excluded_names
        ):
            continue
        files.append({
            "path": str(relative).replace("\\", "/"),
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        })
    payload = {
        "schema_version": "2.0", "run_id": run_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(), "files": files,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Finalize target-page archives and build the derivative evidence PDF"
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--skill-dir")
    parser.add_argument("--title")
    args = parser.parse_args()

    raw_run_dir = Path(args.run_dir).expanduser().absolute()
    if is_reparse_point(raw_run_dir):
        raise ValueError("RUN_DIR cannot be a symlink, junction or reparse point")
    run_dir = raw_run_dir.resolve(strict=True)
    FormalOutputTransaction.recover_stale(run_dir)
    audit_run_tree(run_dir)
    config = read_json(run_dir / "run-config.json")
    reference_record = read_json(run_dir / "reference" / "reference.json", {}) or {}
    if not config:
        raise FileNotFoundError("run-config.json is required")
    skill_dir = resolve_skill_dir(args.skill_dir, config)
    if reference_record.get("analysis_status") != "complete":
        raise ValueError("reference/reference.json analysis_status must be complete")
    if config.get("test_mode") is not True:
        qcc_validation = validate_qcc_reference(run_dir)
        if qcc_validation.get("ok") is not True:
            raise ValueError(
                "QCC reference provenance validation failed: "
                + json.dumps(qcc_validation.get("errors") or [], ensure_ascii=False)
            )

    query_runs, discovered, frontier = load_discovery(run_dir)
    sources = load_sources(run_dir)
    capture_attempts = read_jsonl(run_dir / "capture-attempts.jsonl")
    probe_attempts = read_jsonl(run_dir / "probe-attempts.jsonl")
    sensitive_records = capture_attempts + sources
    sensitive_records.extend(
        read_json(path, {}) or {} for path in (run_dir / "candidate-pages").glob("*/metadata.json")
    )
    reject_sensitive_inputs(run_dir, sensitive_records)
    coverage_summary = coverage(run_dir, config, query_runs, frontier, sources)
    if coverage_summary["status"] != "complete":
        raise ValueError(
            "Investigation coverage is insufficient: "
            + json.dumps(coverage_summary["deficits"], ensure_ascii=False)
        )

    reference = config.get("trademark") or {}
    results = read_json(run_dir / "results.json", {}) or {}
    execution_profile = config.get("execution_profile") or "forensic"
    if execution_profile == "quick":
        if results.get("investigation_scope") != "quick_non_exhaustive":
            raise ValueError(
                "Quick results.json must declare investigation_scope=quick_non_exhaustive"
            )
        if not str(results.get("conclusion") or "").strip():
            raise ValueError("Quick results.json must contain a scoped conclusion")
    conclusion = results.get("conclusion") or (
        "本卷仅汇编经过内容、离线回放和完整性校验的真实目标网页；"
        "商标匹配与商业使用结论以逐页记录为准。"
    )
    limitations = results.get("limitations") or [
        "搜索页仅用于发现 URL，未作为正式网页证据或并入 PDF。",
        "公开网络未覆盖登录后、线下或未被索引的渠道。",
        "同会话 MHTML 为高保真主归档，SingleFile HTML 为便携副本，PDF 为派生阅读件。",
    ]
    title = args.title or (
        f"{reference.get('registration_number') or reference.get('name') or '商标'}"
        "公开网络目标网页证据材料"
    )
    order = {
        "schema_version": "2.0", "title": title,
        "cover": {
            "trademark_name": reference.get("name"),
            "registration_number": reference.get("registration_number"),
            "owner": reference.get("owner"), "period": reference.get("period"),
            "summary": conclusion, "limitations": limitations,
        },
        "items": [],
    }
    for item in sources:
        order["items"].append({
            "id": item.get("source_id"),
            "label": item.get("label") or item.get("title") or item.get("source_id"),
            "pdf": str(item["_pdf"].relative_to(run_dir)).replace("\\", "/"),
            "source_url": item.get("final_url") or item.get("normalized_url"),
            "display_url": item.get("display_url") or item.get("final_url"),
            "captured_at": item.get("captured_at"), "page_state": item.get("page_state"),
            "content_valid": True, "page_role": item.get("page_role"),
        })

    order_path = run_dir / "capture-order.json"
    merge_script = skill_dir / "scripts" / "merge-evidence-pdf.py"
    validator = skill_dir / "scripts" / "validate-run.py"
    binder = run_dir / "evidence-binder.pdf"
    build: dict = {}
    skipped_links = build_skipped_links(frontier, probe_attempts, capture_attempts, sources)
    skipped_path = run_dir / "skipped-links.json"
    validation_nonce = secrets.token_hex(24)

    with FormalOutputTransaction(run_dir) as transaction:
        transaction.bind_validation_nonce(validation_nonce)
        transaction.begin_mutation()
        for rel in TRANSIENT_OUTPUTS:
            transient = run_dir / rel
            if transient.is_file() or transient.is_symlink():
                transient.unlink()
        write_coverage_matrix(run_dir, query_runs, frontier, sources, coverage_summary)
        skipped_path.write_text(
            json.dumps(skipped_links, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        order_path.write_text(
            json.dumps(order, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        merged = run_bounded(
            [sys.executable, str(merge_script), "--manifest", str(order_path), "--output", str(binder)],
            cwd=skill_dir, timeout=600,
        )
        if merged.returncode:
            raise RuntimeError(
                f"Evidence PDF merge failed ({merged.returncode}):\n{merged.stdout}\n{merged.stderr}"
            )
        build = read_json(run_dir / "evidence-binder.build.json", {}) or {}
        if not binder.is_file() or not build.get("sha256"):
            raise RuntimeError("Evidence PDF merge did not produce both the binder and its build record")

        high = sum(
            (item.get("visual_consensus") or {}).get("positive_evidence_eligible") is True
            and (item.get("visual_consensus") or {}).get("confidence") == "high"
            and not item.get("manual_review_override")
            for item in sources if item.get("page_role") == "evidence"
        )
        manual = sum(bool(item.get("manual_review_override")) for item in sources)
        results.update({
            "schema_version": "2.0", "run_id": config.get("run_id"), "trademark": reference,
            "execution_profile": execution_profile,
            "coverage": coverage_summary, "conclusion": conclusion, "limitations": limitations,
            "artifact_model": {
                "primary": "same-session MHTML",
                "portable": "SingleFile HTML",
                "derivative": "PDF",
            },
            "discovery": {
                "provider_runs": len(query_runs),
                "result_records": len(discovered),
                "unique_target_urls": frontier.get("target_count", len(frontier.get("items") or [])),
            },
            "formal_sources": len(sources),
            "skipped_link_count": skipped_links["count"],
            "high_confidence_evidence": high,
            "manual_review_sources": manual,
            "deliverables": {
                "evidence_binder": "evidence-binder.pdf",
                "capture_order": "capture-order.json",
                "skipped_links": "skipped-links.json",
                "skipped_links_sha256": sha256(skipped_path),
                "page_count": build.get("page_count"),
                "sha256": build.get("sha256"),
            },
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        (run_dir / "results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        skipped_report = [
            "", "## 未归档链接", "",
            f"共 {skipped_links['count']} 条。以下链接仅用于后续人工复查，未进入证据 PDF；完整清单见 `skipped-links.json`。", "",
        ]
        for skipped in skipped_links["items"][:20]:
            safe_url = skipped["url"].replace(">", "%3E")
            reason = " ".join(str(skipped["reason"]).splitlines())
            skipped_report.append(
                f"- [{skipped['category']} / {skipped['status']}] "
                f"{skipped['site_key']} — <{safe_url}> — {reason}"
            )
        if skipped_links["count"] > 20:
            skipped_report.extend([
                "",
                f"报告仅显示前 20 条；其余 {skipped_links['count'] - 20} 条见 `skipped-links.json`。",
            ])

        report = [
            "# 商标公开网络目标网页调查报告", "",
            f"- 商标：{reference.get('name') or '未提供'}",
            f"- 注册号：{reference.get('registration_number') or '未提供'}",
            f"- 权利人：{reference.get('owner') or '未提供'}",
            f"- 调查模式：{coverage_summary.get('execution_profile')}", "",
            "## 结论", "", conclusion, "",
            "## 覆盖与网页证据", "",
            f"- 发现提供方：{coverage_summary['actual']['discovery_providers']}（{', '.join(coverage_summary['provider_names'])}）",
            f"- 非搜索页目标 URL：{coverage_summary['actual']['unique_target_urls']}",
            f"- 目标域名：{coverage_summary['actual']['target_domains']}",
            f"- 去重直达目标 URL 抓取：{coverage_summary['actual']['direct_capture_attempts']}",
            f"- 抓取调用总数（含同 URL 重试，仅审计）：{coverage_summary['actual']['capture_invocations_audit']}",
            f"- 正常直达候选页：{coverage_summary['actual']['normal_target_pages']}",
            f"- 正式目标网页：{len(sources)}", "- 正式搜索结果页：0", "",
            "## 未成功查询", "",
            *( ["- 无"] if not coverage_summary.get("failed_query_ids") else [
                f"- {query_id}" for query_id in coverage_summary["failed_query_ids"]
            ] ), "",
            "## 交付", "", f"- PDF：{binder}",
            f"- PDF页数：{build.get('page_count')}",
            f"- PDF SHA-256：{build.get('sha256')}",
            "- 每个来源目录包含同会话 page.mhtml、SingleFile 原始/安全 HTML、"
            "rendered-dom.html、在线/离线截图、page.pdf、metadata.json 和离线回放结果。",
            "", "## 限制", "",
        ] + [f"- {item}" for item in limitations] + skipped_report
        (run_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        write_manifest(run_dir)

        completed = run_bounded(
            [
                sys.executable, str(validator), "--run-dir", str(run_dir), "--strict",
                "--validation-nonce", validation_nonce,
            ],
            cwd=skill_dir, timeout=300,
        )
        if completed.returncode:
            raise RuntimeError(
                f"Strict final validation failed ({completed.returncode}):\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
        audit_run_tree(run_dir, allow_active_transaction=True)
        validation = read_json(run_dir / "validation.json", {}) or {}
        if (
            validation.get("ok") is not True
            or validation.get("strict") is not True
            or validation.get("validation_nonce") != validation_nonce
        ):
            raise RuntimeError("Strict validator did not produce a fresh ok=true/strict=true nonce-bound record")
        transaction.mark_validated()
        transaction.commit()

    if (run_dir / TRANSACTION_DIR_NAME).exists() or is_reparse_point(run_dir / TRANSACTION_DIR_NAME):
        raise RuntimeError("Finalization transaction residue survived commit")
    audit_run_tree(run_dir)
    print(json.dumps({
        "run_dir": str(run_dir), "evidence_binder": str(binder),
        "page_count": build.get("page_count"), "sha256": build.get("sha256"),
        "formal_serp_count": 0, "skipped_link_count": skipped_links["count"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
