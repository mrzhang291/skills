#!/usr/bin/env python3

"""Deterministic run-config limits shared by discovery entry points."""

from __future__ import annotations


def positive_budget(config: dict, name: str, default: int) -> int:
    value = (config.get("budgets") or {}).get(name, default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"run-config budget {name} must be a positive integer") from error


def validate_query_slot(config: dict, records: list[dict], query_id: str, query: str) -> None:
    """Keep a run inside its fixed query budget and prevent one ID changing meaning."""
    existing_ids = {str(item.get("query_id") or "") for item in records if item.get("query_id")}
    existing_queries = {
        str(item.get("query") or "") for item in records
        if str(item.get("query_id") or "") == query_id
    }
    if existing_queries and existing_queries != {query}:
        raise ValueError(f"query-id {query_id} is already bound to a different query")
    max_queries = positive_budget(config, "max_query_count", 36)
    if query_id not in existing_ids and len(existing_ids) >= max_queries:
        raise ValueError(f"run-config max_query_count={max_queries} reached")


def cap_query_results(records: list[dict], limit: int) -> list[dict]:
    """Keep at most ``limit`` unique URLs, round-robin by provider and then rank."""
    buckets: dict[str, list[dict]] = {}
    for item in records:
        candidate = dict(item)
        candidate["additional_discoveries"] = list(item.get("additional_discoveries") or [])
        buckets.setdefault(str(candidate.get("provider") or ""), []).append(candidate)
    for provider_records in buckets.values():
        provider_records.sort(key=lambda item: (int(item.get("rank") or 0), str(item.get("discovery_id") or "")))
    ordered_providers = sorted(buckets, key=str.casefold)
    retained: list[dict] = []
    retained_urls: set[str] = set()
    positions = {provider: 0 for provider in ordered_providers}
    while len(retained) < limit:
        added = False
        for provider in ordered_providers:
            provider_records = buckets[provider]
            while positions[provider] < len(provider_records):
                candidate = provider_records[positions[provider]]
                positions[provider] += 1
                url = str(candidate.get("normalized_url") or "")
                if url and url not in retained_urls:
                    retained.append(candidate)
                    retained_urls.add(url)
                    added = True
                    if len(retained) >= limit:
                        break
                    break
            if len(retained) >= limit:
                break
        if not added:
            break

    selected = {str(item.get("normalized_url") or ""): item for item in retained}
    selected_discovery_ids = {item.get("discovery_id") for item in retained}
    for item in records:
        canonical = selected.get(str(item.get("normalized_url") or ""))
        if not canonical or item.get("discovery_id") in selected_discovery_ids:
            continue
        traces = [{
            "query_id": item.get("query_id"), "query": item.get("query"),
            "provider": item.get("provider"), "provider_key": item.get("provider_key"),
            "rank": item.get("rank"), "discovery_id": item.get("discovery_id"),
            "raw_sha256": item.get("raw_sha256"),
        }]
        traces.extend(item.get("additional_discoveries") or [])
        existing = {
            (trace.get("provider"), trace.get("discovery_id"))
            for trace in canonical["additional_discoveries"]
        }
        canonical["additional_discoveries"].extend(
            trace for trace in traces
            if (trace.get("provider"), trace.get("discovery_id")) not in existing
        )
    return retained


def remove_provider_results(
    records: list[dict], query_id: str, provider: str, provider_key: str | None = None
) -> list[dict]:
    """Remove one provider while preserving alternate provenance on the same URL."""
    output = []
    for item in records:
        if item.get("query_id") != query_id:
            output.append(item)
            continue
        def same_provider(trace: dict) -> bool:
            if provider_key:
                return trace.get("provider_key") == provider_key
            return trace.get("provider") == provider

        alternatives = [
            trace for trace in (item.get("additional_discoveries") or [])
            if not same_provider(trace)
        ]
        if same_provider(item):
            if not alternatives:
                continue
            replacement, *remaining = alternatives
            promoted = dict(item)
            promoted.update({
                "query_id": replacement.get("query_id") or item.get("query_id"),
                "query": replacement.get("query") or item.get("query"),
                "provider": replacement.get("provider"),
                "provider_key": replacement.get("provider_key"),
                "rank": replacement.get("rank"),
                "discovery_id": replacement.get("discovery_id"),
                "raw_sha256": replacement.get("raw_sha256"),
                "additional_discoveries": remaining,
            })
            output.append(promoted)
        else:
            retained = dict(item)
            retained["additional_discoveries"] = alternatives
            output.append(retained)
    return output
