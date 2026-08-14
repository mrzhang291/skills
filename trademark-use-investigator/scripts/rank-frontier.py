#!/usr/bin/env python3

"""Rank an existing discovery frontier without network or browser activity.

This is the inexpensive bridge between discovery and page capture.  It uses
only metadata already recorded in the run directory and writes a small,
explainable shortlist for a later capture phase.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import unquote

from discovery_limits import positive_budget
from url_utils import is_search_result_url, normalize_url, site_key


DEFAULT_LIMIT = 8
COMMERCIAL_TERMS = (
    "购买", "价格", "售价", "商品", "产品", "店铺", "旗舰店", "下单",
    "销售", "批发", "厂家", "生产", "包装", "标签", "规格", "库存",
    "销量", "buy", "price", "shop", "store", "sale", "product",
    "manufacturer",
)
SCORE_WEIGHTS = {
    "registration_number": 28,
    "owner": 24,
    "trademark": 16,
    "goods_each": 5,
    "goods_cap": 25,
    "commercial_each": 2,
    "commercial_cap": 12,
    "provider_each": 1,
    "provider_cap": 3,
    "best_rank_top3": 3,
    "best_rank_top10": 2,
    "best_rank_other": 1,
    "new_site_bonus": 8,
    "second_site_item_bonus": 2,
}


def read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Required input not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"Required input not found: {path}")
    records: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Expected a JSON object at {path}:{line_number}")
        records.append(value)
    return records


def normalized_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def term_variants(value: object) -> list[str]:
    """Return useful exact substrings for one configured goods/service label."""
    original = normalized_text(value)
    if not original:
        return []
    variants = {original}
    without_parenthetical = normalized_text(re.sub(r"[（(][^）)]*[）)]", " ", original))
    if len(without_parenthetical) >= 2:
        variants.add(without_parenthetical)
    for part in re.split(r"[\s/、,，;；:：()（）\[\]【】_-]+", original):
        part = part.strip()
        if len(part) >= 2:
            variants.add(part)
    return sorted(variants, key=lambda item: (-len(item), item))


def contains(text: str, value: object) -> bool:
    term = normalized_text(value)
    return bool(term and term in text)


def result_url(record: dict) -> str | None:
    return normalize_url(str(record.get("normalized_url") or record.get("result_url") or ""))


def best_rank(records: list[dict]) -> int | None:
    ranks = []
    for record in records:
        try:
            rank = int(record.get("rank"))
        except (TypeError, ValueError):
            continue
        if rank > 0:
            ranks.append(rank)
    return min(ranks) if ranks else None


def provenance_score(records: list[dict]) -> tuple[int, list[str], int | None]:
    providers = sorted({
        normalized_text(record.get("provider_key") or record.get("provider"))
        for record in records
        if normalized_text(record.get("provider_key") or record.get("provider"))
    })
    rank = best_rank(records)
    score = min(len(providers), SCORE_WEIGHTS["provider_cap"]) * SCORE_WEIGHTS["provider_each"]
    if rank is not None:
        if rank <= 3:
            score += SCORE_WEIGHTS["best_rank_top3"]
        elif rank <= 10:
            score += SCORE_WEIGHTS["best_rank_top10"]
        else:
            score += SCORE_WEIGHTS["best_rank_other"]
    return score, providers, rank


def semantic_signals(config: dict, text: str) -> tuple[dict, dict, int]:
    trademark = config.get("trademark") or {}
    mark = trademark.get("name")
    registration = trademark.get("registration_number")
    owner = trademark.get("owner")
    goods = trademark.get("goods_services") or []
    if isinstance(goods, str):
        goods = [item.strip() for item in re.split(r"[;；]", goods) if item.strip()]

    matched_goods = []
    for label in goods:
        if any(variant in text for variant in term_variants(label)):
            matched_goods.append(str(label))
    matched_commercial = sorted({term for term in COMMERCIAL_TERMS if normalized_text(term) in text})
    exact = {
        "trademark": str(mark) if contains(text, mark) else None,
        "registration_number": str(registration) if contains(text, registration) else None,
        "owner": str(owner) if contains(text, owner) else None,
    }

    components = {
        "registration_number": SCORE_WEIGHTS["registration_number"] if exact["registration_number"] else 0,
        "owner": SCORE_WEIGHTS["owner"] if exact["owner"] else 0,
        "trademark": SCORE_WEIGHTS["trademark"] if exact["trademark"] else 0,
        "goods": min(len(matched_goods) * SCORE_WEIGHTS["goods_each"], SCORE_WEIGHTS["goods_cap"]),
        "commercial": min(
            len(matched_commercial) * SCORE_WEIGHTS["commercial_each"],
            SCORE_WEIGHTS["commercial_cap"],
        ),
    }
    signals = {
        **exact,
        "goods": matched_goods,
        "commercial": matched_commercial,
    }
    return signals, components, sum(components.values())


def build_candidates(config: dict, results: list[dict], frontier: dict) -> tuple[list[dict], list[dict]]:
    results_by_url: dict[str, list[dict]] = defaultdict(list)
    for record in results:
        url = result_url(record)
        if url:
            results_by_url[url].append(record)

    candidates: list[dict] = []
    excluded: list[dict] = []
    seen_urls: set[str] = set()
    for item in frontier.get("items") or []:
        raw_url = str(item.get("normalized_url") or item.get("url") or "")
        url = normalize_url(raw_url)
        if not url:
            excluded.append({"target_id": item.get("target_id"), "url": raw_url, "reason": "invalid_url"})
            continue
        if url in seen_urls:
            excluded.append({"target_id": item.get("target_id"), "url": url, "reason": "duplicate_url"})
            continue
        seen_urls.add(url)
        if item.get("search_result_page") or is_search_result_url(url):
            excluded.append({"target_id": item.get("target_id"), "url": url, "reason": "search_result_page"})
            continue

        records = results_by_url.get(url, [])
        text_chunks = [item.get("title"), unquote(url)]
        for record in records:
            text_chunks.extend((record.get("title"), record.get("snippet"), unquote(str(record.get("result_url") or ""))))
        text = normalized_text("\n".join(str(chunk or "") for chunk in text_chunks))
        signals, components, semantic_score = semantic_signals(config, text)
        if not any(signals.get(name) for name in ("registration_number", "owner", "trademark")):
            excluded.append({
                "target_id": item.get("target_id"),
                "url": url,
                "reason": "no_trademark_owner_or_registration_anchor",
            })
            continue
        trace_score, providers, rank = provenance_score(records)
        components["provenance"] = trace_score
        base_score = semantic_score + trace_score
        queries = sorted({
            str(record.get("query")) for record in records if str(record.get("query") or "").strip()
        })
        candidates.append({
            "target_id": item.get("target_id"),
            "normalized_url": url,
            "site_key": site_key(url),
            "title": str(item.get("title") or next((record.get("title") for record in records if record.get("title")), "")),
            "frontier_status": item.get("status"),
            "base_score": base_score,
            "semantic_score": semantic_score,
            "score_components": components,
            "matched_signals": signals,
            "providers": providers,
            "best_result_rank": rank,
            "discovery_record_count": len(records),
            "queries": queries,
        })
    return candidates, excluded


def candidate_priority(item: dict) -> tuple:
    rank = item.get("best_result_rank")
    return (
        -item["base_score"],
        -item["semantic_score"],
        rank if rank is not None else 10**9,
        item["site_key"],
        item["normalized_url"],
    )


def select_diverse(
    candidates: list[dict],
    limit: int,
    *,
    per_site_limit: int,
    max_site_limit: int,
    minimum_site_target: int,
) -> list[dict]:
    """Select several strong pages from a small, bounded set of sites.

    The first phase establishes the configured minimum site diversity.  The
    second phase fills those sites up to their per-site limit before opening a
    further site.  This keeps Quick investigations concentrated without
    sacrificing the number of useful destination pages.
    """
    by_site: dict[str, list[dict]] = defaultdict(list)
    for item in candidates:
        by_site[item["site_key"]].append(dict(item))
    for site_candidates in by_site.values():
        site_candidates.sort(key=candidate_priority)

    ordered_sites = sorted(
        by_site,
        key=lambda site: (candidate_priority(by_site[site][0]), site),
    )
    selected: list[dict] = []
    site_counts: Counter[str] = Counter()
    active_sites: list[str] = []

    def add_candidate(chosen: dict, reason: str) -> None:
        site = chosen["site_key"]
        site_count = site_counts[site]
        diversity_bonus = (
            SCORE_WEIGHTS["new_site_bonus"] if site_count == 0
            else SCORE_WEIGHTS["second_site_item_bonus"]
        )
        chosen["diversity_bonus"] = diversity_bonus
        chosen["score"] = chosen["base_score"] + diversity_bonus
        chosen["rank"] = len(selected) + 1
        chosen["selection_reason"] = reason
        selected.append(chosen)
        site_counts[site] += 1

    required_sites = min(
        max(1, minimum_site_target),
        max_site_limit,
        limit,
        len(ordered_sites),
    )
    for site in ordered_sites[:required_sites]:
        active_sites.append(site)
        add_candidate(by_site[site].pop(0), "minimum_site_diversity")

    unopened_sites = ordered_sites[required_sites:max_site_limit]
    while len(selected) < limit:
        eligible = [
            item
            for site in active_sites
            if site_counts[site] < per_site_limit
            for item in by_site[site][:1]
        ]
        if eligible:
            chosen = min(eligible, key=candidate_priority)
            by_site[chosen["site_key"]].pop(0)
            add_candidate(chosen, "additional_candidate_from_selected_site")
            continue
        if unopened_sites:
            site = unopened_sites.pop(0)
            active_sites.append(site)
            add_candidate(by_site[site].pop(0), "additional_site_needed_for_page_count")
            continue
        break
    return selected


def rank_run(run_dir: Path, limit: int) -> tuple[Path, dict]:
    run_dir = run_dir.resolve()
    config = read_json(run_dir / "run-config.json")
    profile = config.get("execution_profile") or "forensic"
    if profile not in {"quick", "forensic"}:
        raise ValueError(f"Unsupported execution_profile: {profile!r}")
    configured_limit = int((config.get("budgets") or {}).get(
        "max_ranked_candidates", 8 if profile == "quick" else 20
    ))
    limit = min(limit, max(1, configured_limit))
    per_site_limit = positive_budget(
        config,
        "max_pages_per_domain",
        3 if profile == "quick" else 5,
    )
    max_site_limit = positive_budget(
        config,
        "max_ranked_sites",
        3 if profile == "quick" else 10,
    )
    raw_minimum_sites = (config.get("coverage_requirements") or {}).get(
        "min_target_domains", 2 if profile == "quick" else 3
    )
    try:
        minimum_site_target = max(1, int(raw_minimum_sites))
    except (TypeError, ValueError) as error:
        raise ValueError("run-config coverage requirement min_target_domains must be an integer") from error
    results = read_jsonl(run_dir / "discovery" / "results.jsonl")
    frontier = read_json(run_dir / "discovery" / "url-frontier.json")
    candidates, excluded = build_candidates(config, results, frontier)
    selected = select_diverse(
        candidates,
        limit,
        per_site_limit=per_site_limit,
        max_site_limit=max_site_limit,
        minimum_site_target=minimum_site_target,
    )
    output = {
        "schema_version": "1.0",
        "record_type": "local_ranked_frontier",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": config.get("run_id"),
        "execution_profile": profile,
        "mode": f"{profile}_local",
        "network_used": False,
        "browser_used": False,
        "limit": limit,
        "per_site_limit": per_site_limit,
        "max_site_limit": max_site_limit,
        "minimum_site_target": minimum_site_target,
        "input_counts": {
            "discovery_results": len(results),
            "frontier_items": len(frontier.get("items") or []),
            "eligible_candidates": len(candidates),
            "excluded_candidates": len(excluded),
        },
        "selected_count": len(selected),
        "selected_site_count": len({item["site_key"] for item in selected}),
        "scoring_weights": SCORE_WEIGHTS,
        "excluded": excluded,
        "items": selected,
    }
    output_path = run_dir / "discovery" / "ranked-candidates.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path, output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Locally rank an existing URL frontier; never opens a browser or performs network I/O"
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be a positive integer")
    output_path, output = rank_run(Path(args.run_dir), args.limit)
    print(json.dumps({
        "ranked": True,
        "output": str(output_path),
        "selected_count": output["selected_count"],
        "selected_site_count": output["selected_site_count"],
        "network_used": False,
        "browser_used": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
