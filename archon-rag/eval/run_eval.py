"""Run retrieval evaluation against an Archon RAG knowledge base.

Example:
    python eval\\run_eval.py ^
        --base-dir ..\\..\\archon-data ^
        --dept general ^
        --dept-password change-me ^
        --eval-json eval_queries.json ^
        --k 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _doc_key(filename: str) -> str:
    """Map indexed chunk filenames back to the original document name."""
    stem, ext = os.path.splitext(str(filename))
    stem = re.sub(r"_chunk\d+_[0-9a-f]{6,}$", "", stem)
    return stem + ext


def _load_employee_search() -> object:
    path = ROOT / "employee-search" / "scripts"
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    import employee_search
    return employee_search


def _evaluate_query(employee_search, query_item: dict, k: int) -> dict:
    query = query_item.get("query", "")
    expected_files = set(_doc_key(f) for f in query_item.get("expected_filenames", []))
    expected_ids = set(query_item.get("expected_doc_ids", []))

    records, error = employee_search._do_search_internal(query)
    retrieved_files = []
    retrieved_ids = []
    for record in records:
        retrieved_files.append(str(record.get("source_filename") or record.get("filename") or ""))
        retrieved_ids.append(str(record.get("id") or ""))

    def is_expected(index: int) -> bool:
        return (
            _doc_key(retrieved_files[index]) in expected_files
            or retrieved_ids[index] in expected_ids
        )

    expected_total = len(expected_files) + len(expected_ids)
    top_hits = [i for i in range(min(k, len(records))) if is_expected(i)]
    all_hits = [i for i in range(len(records)) if is_expected(i)]
    recall_at_k = min(len(top_hits) / expected_total, 1.0) if expected_total else 0.0
    mrr = 1.0 / (all_hits[0] + 1) if all_hits else 0.0

    return {
        "id": query_item.get("id", ""),
        "query": query,
        "expected_total": expected_total,
        "recall_at_k": round(recall_at_k, 4),
        "mrr": round(mrr, 4),
        "top_hits": top_hits,
        "all_hit_count": len(all_hits),
        "retrieved_count": len(records),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Archon RAG retrieval evaluation")
    parser.add_argument("--base-dir", default=os.environ.get("ARCHON_BASE_DIR", "archon-data"))
    parser.add_argument("--dept", default=os.environ.get("ARCHON_DEPARTMENT", "general"))
    parser.add_argument("--dept-password", default=os.environ.get("ARCHON_DEPT_PASSWORD", ""))
    parser.add_argument("--eval-json", default=str(Path(__file__).with_name("eval_queries.json")))
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    employee_search = _load_employee_search()
    employee_search.configure(
        base_dir=args.base_dir,
        department=args.dept,
        password=args.dept_password or None,
    )

    with open(args.eval_json, "r", encoding="utf-8") as f:
        eval_data = json.load(f)

    results = [_evaluate_query(employee_search, item, args.k) for item in eval_data.get("queries", [])]
    total = len(results)
    mean_recall = sum(r["recall_at_k"] for r in results) / total if total else 0.0
    mean_mrr = sum(r["mrr"] for r in results) / total if total else 0.0
    hit_queries = sum(1 for r in results if r["all_hit_count"] > 0)

    output = {
        "eval_set": eval_data.get("name", ""),
        "k": args.k,
        "total_queries": total,
        "hit_queries": hit_queries,
        "mean_recall_at_k": round(mean_recall, 4),
        "mean_mrr": round(mean_mrr, 4),
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
