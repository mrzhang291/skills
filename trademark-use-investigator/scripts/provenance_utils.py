#!/usr/bin/env python3

"""Shared provider/provenance rules for discovery, capture and validation."""

from __future__ import annotations

import hashlib
from pathlib import Path

from url_utils import require_safe_file_id


TRUSTED_PROVIDERS = {"firecrawl", "bing", "so360", "yahoo", "baidu", "cherrystudio-web"}
TEST_ONLY_PROVIDERS = {"fixture", "self-test"}
TRUSTED_SOURCE_KINDS = {
    "browser_search", "search_api", "cherrystudio_export", "search_api_export",
    "self_test_fixture",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_provider(
    provider: str,
    *,
    source_kind: str,
    test_mode: bool = False,
    provider_instance_id: str | None = None,
) -> tuple[str, str]:
    provider = require_safe_file_id(provider, "provider").lower()
    if source_kind not in TRUSTED_SOURCE_KINDS:
        raise ValueError(f"Unsupported discovery source_kind: {source_kind!r}")
    if provider in TEST_ONLY_PROVIDERS:
        if not test_mode or source_kind != "self_test_fixture":
            raise ValueError("fixture/self-test providers require run-config test_mode=true and source_kind=self_test_fixture")
    elif provider not in TRUSTED_PROVIDERS:
        raise ValueError(f"Unknown/untrusted discovery provider: {provider}")
    if source_kind == "self_test_fixture" and provider not in TEST_ONLY_PROVIDERS:
        raise ValueError("source_kind=self_test_fixture is reserved for explicit test-only providers")
    if provider == "cherrystudio-web" and source_kind != "cherrystudio_export":
        raise ValueError("cherrystudio-web imports require source_kind=cherrystudio_export")
    if provider_instance_id:
        provider_instance_id = require_safe_file_id(provider_instance_id, "provider-instance-id")
    key = provider if not provider_instance_id else f"{provider}@{provider_instance_id.lower()}"
    return provider, key


def provenance_trace(record: dict) -> tuple:
    """Canonical trace tuple used to cross-check frontier against results.jsonl."""
    return (
        str(record.get("query_id") or ""),
        str(record.get("query") or ""),
        str(record.get("provider") or ""),
        str(record.get("provider_key") or ""),
        str(record.get("discovery_id") or ""),
        str(record.get("raw_sha256") or ""),
    )


def result_traces(record: dict) -> list[dict]:
    primary = {
        key: record.get(key)
        for key in ("query_id", "query", "provider", "provider_key", "rank", "discovery_id", "raw_sha256")
    }
    return [primary, *(record.get("additional_discoveries") or [])]
