#!/usr/bin/env python3

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Create one isolated trademark web-evidence run directory")
    parser.add_argument(
        "--profile", choices=["quick", "forensic"], default="quick",
        help="quick is the compact CLI profile; forensic enables full-coverage thresholds (CherryStudio production uses its assisted orchestrator)",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--reference-file")
    parser.add_argument(
        "--allow-missing-reference", action="store_true",
        help="Initialize without a local reference only when a later hard-gated QCC capture is mandatory",
    )
    parser.add_argument("--trademark-name", required=True)
    parser.add_argument("--registration-number")
    parser.add_argument("--owner")
    parser.add_argument("--goods", default="")
    parser.add_argument("--period")
    parser.add_argument("--min-discovery-providers", type=int)
    parser.add_argument("--min-target-urls", type=int)
    parser.add_argument("--min-target-domains", type=int)
    parser.add_argument("--min-capture-attempts", type=int)
    parser.add_argument("--min-normal-target-pages", type=int)
    parser.add_argument("--max-results-per-query", type=int)
    parser.add_argument("--max-pages-per-domain", type=int)
    parser.add_argument("--max-visual-candidates", type=int)
    args = parser.parse_args()

    presets = {
        "quick": {
            "min_discovery_providers": 1,
            "min_target_urls": 8,
            "min_target_domains": 2,
            "min_probe_attempts": 6,
            "min_capture_attempts": 4,
            "min_normal_target_pages": 4,
            "min_formal_sources": 4,
            "max_results_per_query": 6,
            "max_pages_per_domain": 3,
            "max_ranked_candidates": 8,
            "max_ranked_sites": 3,
            "max_probe_pages_per_site": 3,
            "max_visual_candidates": 2,
            "max_query_count": 7,
            "max_probe_attempts": 8,
            "max_capture_attempts": 6,
            "target_full_pages": 5,
            "max_formal_sources": 5,
            "max_visual_invocations": 2,
            "require_each_query_success": False,
        },
        "forensic": {
            "min_discovery_providers": 2,
            "min_target_urls": 10,
            "min_target_domains": 3,
            "min_probe_attempts": 0,
            "min_capture_attempts": 8,
            "min_normal_target_pages": 5,
            "min_formal_sources": 1,
            "max_results_per_query": 20,
            "max_pages_per_domain": 5,
            "max_ranked_candidates": 20,
            "max_ranked_sites": 10,
            "max_probe_pages_per_site": 5,
            "max_visual_candidates": 10,
            "max_query_count": 36,
            "max_probe_attempts": 0,
            "max_capture_attempts": 30,
            "target_full_pages": 20,
            "max_formal_sources": 20,
            "max_visual_invocations": 30,
            "require_each_query_success": True,
        },
    }
    preset = presets[args.profile]

    def selected(name):
        value = getattr(args, name)
        return preset[name] if value is None else value

    workspace = Path(args.workspace).resolve()
    source = Path(args.reference_file).resolve() if args.reference_file else None
    if source is not None and not source.is_file():
        raise FileNotFoundError(f"Reference file not found: {source}")
    if source is None and not args.allow_missing_reference:
        raise ValueError("--reference-file is required unless --allow-missing-reference is explicit")

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.run_id) or ".." in args.run_id:
        raise ValueError("run-id may contain only letters, digits, dot, underscore and hyphen, without '..'")
    evidence_root = workspace if workspace.name.casefold() == "trademark-evidence" else workspace / "trademark-evidence"
    run_dir = evidence_root / args.run_id
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"Run directory already exists and is not empty; choose a new run-id: {run_dir}")
    for rel in (
        "reference",
        "discovery/raw",
        "discovery/providers",
        "candidate-pages",
        "source-pages",
        "capture-diagnostics",
        "candidates",
        "visual-reviews",
        "rejected-pages",
        "offline-validation",
    ):
        (run_dir / rel).mkdir(parents=True, exist_ok=True)
    destination = None
    if source is not None:
        destination = run_dir / "reference" / ("original" + source.suffix.lower())
        shutil.copy2(source, destination)

    created_at = datetime.now(timezone.utc).isoformat()
    goods = [item.strip() for item in args.goods.replace("；", ";").split(";") if item.strip()]
    config = {
        "schema_version": "2.0",
        "execution_profile": args.profile,
        "run_id": args.run_id,
        "run_dir": str(run_dir),
        "workspace": str(workspace),
        "created_at": created_at,
        "trademark": {
            "name": args.trademark_name,
            "registration_number": args.registration_number,
            "owner": args.owner,
            "goods_services": goods,
            "period": args.period,
            "reference_file": str(destination.relative_to(run_dir)).replace("\\", "/") if destination else None,
            "reference_sha256": file_hash(destination) if destination else None,
            "reference_pending_qcc_capture": destination is None,
        },
        "coverage_requirements": {
            "min_discovery_providers": max(1, selected("min_discovery_providers")),
            "min_target_urls": max(0, selected("min_target_urls")),
            "min_target_domains": max(0, selected("min_target_domains")),
            "min_probe_attempts": preset["min_probe_attempts"],
            "min_capture_attempts": max(0, selected("min_capture_attempts")),
            "min_normal_target_pages": max(0, selected("min_normal_target_pages")),
            "min_formal_sources": preset["min_formal_sources"],
            "require_each_query_success": preset["require_each_query_success"],
        },
        "budgets": {
            "max_results_per_query": max(1, selected("max_results_per_query")),
            "max_pages_per_domain": max(1, selected("max_pages_per_domain")),
            "max_ranked_candidates": preset["max_ranked_candidates"],
            "max_ranked_sites": preset["max_ranked_sites"],
            "max_probe_pages_per_site": preset["max_probe_pages_per_site"],
            "max_visual_candidates": max(1, selected("max_visual_candidates")),
            "max_query_count": preset["max_query_count"],
            "max_probe_attempts": preset["max_probe_attempts"],
            "max_capture_attempts": preset["max_capture_attempts"],
            "target_full_pages": preset["target_full_pages"],
            "max_formal_sources": preset["max_formal_sources"],
            "max_visual_invocations": preset["max_visual_invocations"],
        },
        "evidence_model": {
            "discovery_pages_are_evidence": False,
            "primary_web_archive": "mhtml_same_session",
            "portable_web_archive": "singlefile_html",
            "pdf_is_derivative": True,
        },
    }
    (run_dir / "run-config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reference = dict(config["trademark"])
    reference.update({
        "analysis_status": "pending" if destination else "pending_qcc_reference",
        "created_at": created_at,
    })
    (run_dir / "reference" / "reference.json").write_text(
        json.dumps(reference, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "run_dir": str(run_dir),
        "reference_file": str(destination) if destination else None,
        "reference_pending_qcc_capture": destination is None,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
