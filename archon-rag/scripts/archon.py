r"""Unified CLI for the Archon RAG skill.

Examples:
    python archon.py init --base-dir .\archon-data --dept general --dept-password secret
    python archon.py upload .\docs\report.pdf
    python archon.py status
    python archon.py finalize
    python archon.py search "throughput"
    python archon.py report <doc_id> --answer "Summary" --format pdf
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module(name: str):
    path = ROOT / name / "scripts"
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _configure() -> tuple[str, str, str, str]:
    base_dir = os.environ.get("ARCHON_BASE_DIR") or os.environ.get(
        "DOCBRAIN_BASE_DIR", str(Path.cwd() / "archon-data")
    )
    department = os.environ.get("ARCHON_DEPARTMENT") or os.environ.get(
        "DOCBRAIN_DEPARTMENT", "general"
    )
    boss_password = os.environ.get("ARCHON_BOSS_PASSWORD", "")
    dept_password = os.environ.get("ARCHON_DEPT_PASSWORD", "")
    _module("boss-upload")
    import boss_upload
    boss_upload.configure(
        base_dir=base_dir,
        boss_password=boss_password or None,
        departments={department: dept_password},
    )
    return base_dir, department, boss_password, dept_password


def _configure_employee(dept_password: str = "") -> None:
    base_dir = os.environ.get("ARCHON_BASE_DIR") or os.environ.get(
        "DOCBRAIN_BASE_DIR", str(Path.cwd() / "archon-data")
    )
    department = os.environ.get("ARCHON_DEPARTMENT") or os.environ.get(
        "DOCBRAIN_DEPARTMENT", "general"
    )
    _module("employee-search")
    import employee_search
    employee_search.configure(
        base_dir=base_dir,
        department=department,
        password=dept_password or None,
    )


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_init(args) -> int:
    base_dir = args.base_dir
    department = args.dept
    if args.profile:
        os.environ["ARCHON_PROFILE"] = args.profile
    _module("boss-upload")
    import boss_upload
    boss_upload.configure(
        base_dir=base_dir,
        boss_password=args.boss_password or None,
        departments={department: args.dept_password or ""},
    )
    _print_json({"status": "ok", "base_dir": base_dir, "department": department, "structure": boss_upload.validate_structure()})
    return 0


def cmd_upload(args) -> int:
    base_dir, department, boss_password, dept_password = _configure()
    if args.profile:
        os.environ["ARCHON_PROFILE"] = args.profile
    if args.base_dir:
        base_dir = args.base_dir
    if args.dept:
        department = args.dept
    if args.dept_password:
        dept_password = args.dept_password
    if args.boss_password:
        boss_password = args.boss_password
    if not dept_password:
        _print_json({"status": "error", "message": "ARCHON_DEPT_PASSWORD or --dept-password is required for upload"})
        return 1
    _module("boss-upload")
    import boss_upload
    boss_upload.configure(
        base_dir=base_dir,
        boss_password=boss_password or None,
        departments={department: dept_password},
    )
    results = []
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import audit_log
        audit_log.log_audit(base_dir, "cli", "upload", department, {"files": args.files})
    except Exception:
        pass
    for file_path in args.files:
        try:
            results.append(boss_upload.boss_upload_step1(file_path, password=boss_password, department=department))
        except Exception as exc:
            results.append({"filename": Path(file_path).name, "error": str(exc)})
    _print_json(results)
    return 0 if all("error" not in r for r in results) else 1


def cmd_status(args) -> int:
    base_dir, department, _, dept_password = _configure()
    if args.base_dir:
        base_dir = args.base_dir
    if args.dept:
        department = args.dept
    if args.dept_password:
        dept_password = args.dept_password
    _module("boss-upload")
    import boss_upload
    boss_upload.configure(
        base_dir=base_dir,
        departments={department: dept_password},
    )
    sys.path.insert(0, str(ROOT / "scripts"))
    import task_status
    _print_json({
        "pipeline": boss_upload.boss_get_pending_status(),
        "tasks": task_status.list_task_status(base_dir),
    })
    return 0


def cmd_serve(args) -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    import archon_server
    return archon_server.run_server(args.host, args.port)


def cmd_watch(args) -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    import hot_folder
    return hot_folder.run_watch(args)


def cmd_doctor(args) -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    import archon_doctor
    result = archon_doctor.run_doctor(
        base_dir=args.base_dir,
        department=args.dept,
        dept_password=args.dept_password,
        load_model=args.load_model,
    )
    _print_json(result)
    return 0 if result.get("status") == "ok" else 1


def cmd_backup(args) -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    import archon_ops
    _print_json(archon_ops.backup(args.base_dir, args.output))
    return 0


def cmd_restore(args) -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    import archon_ops
    _print_json(archon_ops.restore(args.archive, args.target, force=args.force))
    return 0


def cmd_rebuild(args) -> int:
    dept_password = args.dept_password or os.environ.get("ARCHON_DEPT_PASSWORD", "")
    sys.path.insert(0, str(ROOT / "scripts"))
    import archon_ops
    _print_json(archon_ops.rebuild_index(args.base_dir, args.dept, dept_password))
    return 0


def cmd_retry(args) -> int:
    dept_password = args.dept_password or os.environ.get("ARCHON_DEPT_PASSWORD", "")
    sys.path.insert(0, str(ROOT / "scripts"))
    import archon_ops
    _print_json(archon_ops.retry(args.base_dir, args.dept, dept_password, mode=args.mode))
    return 0


def cmd_finalize(args) -> int:
    base_dir, department, boss_password, dept_password = _configure()
    if args.base_dir:
        base_dir = args.base_dir
    if args.dept:
        department = args.dept
    if args.dept_password:
        dept_password = args.dept_password
    if args.boss_password:
        boss_password = args.boss_password
    if not dept_password:
        _print_json({"status": "error", "message": "ARCHON_DEPT_PASSWORD or --dept-password is required for finalize"})
        return 1
    _module("boss-upload")
    import boss_upload
    boss_upload.configure(
        base_dir=base_dir,
        boss_password=boss_password or None,
        departments={department: dept_password},
    )
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import audit_log
        audit_log.log_audit(base_dir, "cli", "finalize", department)
    except Exception:
        pass
    _print_json(boss_upload.boss_upload_auto_finalize(boss_password))
    return 0


def cmd_wiki(args) -> int:
    base_dir, department, _, dept_password = _configure()
    if args.base_dir:
        base_dir = args.base_dir
    if args.dept:
        department = args.dept
    if args.dept_password:
        dept_password = args.dept_password
    _module("boss-upload")
    import boss_upload
    boss_upload.configure(
        base_dir=base_dir,
        departments={department: dept_password},
    )
    _print_json(boss_upload.boss_upload_get_wiki_data(department))
    return 0


def cmd_find(args) -> int:
    dept_password = os.environ.get("ARCHON_DEPT_PASSWORD", args.dept_password or "")
    _configure_employee(dept_password)
    _module("employee-search")
    import employee_search
    meta_matches = employee_search._find_docmeta(args.query)
    if meta_matches:
        _print_json({
            "status": "ok",
            "query": args.query,
            "source": "docmeta",
            "matched_count": len(meta_matches),
            "matches": [employee_search._public_record(r) for r in meta_matches],
        })
        return 0
    matched, err = employee_search._do_search_internal(args.query)
    if matched:
        _print_json({"status": "ok", "query": args.query, "matched_count": len(matched), "matches": [employee_search._public_record(r) for r in matched]})
    else:
        _print_json({"status": "empty", "query": args.query, "matched_count": 0, "error": err})
    return 0


def cmd_search(args) -> int:
    dept_password = os.environ.get("ARCHON_DEPT_PASSWORD", args.dept_password or "")
    _configure_employee(dept_password)
    _module("employee-search")
    import employee_search
    result = employee_search.employee_search_full_pipeline(args.query)
    primary = result.get("primary_results", [])
    secondary = result.get("secondary_results", [])
    output = {
        "query": result.get("query", args.query),
        "department": result.get("department", ""),
        "stages_executed": result.get("stages_executed", []),
        "resolution": result.get("resolution", "unknown"),
        "main_results": [employee_search._public_record(r) for r in primary[:10]],
        "main_results_total": len(primary),
        "mention_results": [employee_search._public_record(r) for r in secondary[:10] if isinstance(r, dict)],
        "mention_results_total": len(secondary),
    }
    try:
        scripts_dir = ROOT / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import query_rewriter
        output["citations"] = query_rewriter.build_citations(primary)
    except Exception:
        pass
    if result.get("merged"):
        merged = result["merged"]
        output["merged"] = {
            "total": merged.get("total", 0),
            "main_count": merged.get("main_count", 0),
            "mention_count": merged.get("mention_count", 0),
        }
    _print_json(output)
    return 0


def cmd_report(args) -> int:
    dept_password = os.environ.get("ARCHON_DEPT_PASSWORD", args.dept_password or "")
    _configure_employee(dept_password)
    _module("report-generator")
    import report_generator
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import audit_log
        audit_log.log_audit(
            os.environ.get("ARCHON_BASE_DIR", "archon-data"),
            "cli",
            "report",
            args.doc_id,
            {"format": args.format},
        )
    except Exception:
        pass
    try:
        path = report_generator.generate_report(
            doc_id=args.doc_id,
            answer=args.answer,
            title=args.title,
            fmt=args.format,
            chart_data=args.chart_data,
            table_data=args.table_data,
        )
        _print_json({"status": "ok", "path": path})
        return 0
    except Exception as exc:
        _print_json({"status": "error", "message": str(exc)})
        return 1


def cmd_verify(args) -> int:
    _configure_employee(args.password)
    _module("employee-search")
    import employee_search
    _print_json(employee_search.verify_and_save(args.password))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Archon RAG unified CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create the Archon data structure")
    p_init.add_argument("--base-dir", default=os.environ.get("ARCHON_BASE_DIR", "archon-data"))
    p_init.add_argument("--dept", default=os.environ.get("ARCHON_DEPARTMENT", "general"))
    p_init.add_argument("--boss-password", default=os.environ.get("ARCHON_BOSS_PASSWORD", ""))
    p_init.add_argument("--dept-password", default=os.environ.get("ARCHON_DEPT_PASSWORD", ""))
    p_init.add_argument("--profile", default=os.environ.get("ARCHON_PROFILE", ""))
    p_init.set_defaults(func=cmd_init)

    p_upload = sub.add_parser("upload", help="Parse files into pending tasks")
    p_upload.add_argument("files", nargs="+")
    p_upload.add_argument("--base-dir", default="")
    p_upload.add_argument("--dept", default="")
    p_upload.add_argument("--boss-password", default="")
    p_upload.add_argument("--dept-password", default="")
    p_upload.add_argument("--profile", default="")
    p_upload.set_defaults(func=cmd_upload)

    p_status = sub.add_parser("status", help="Show pending/processing/done counts")
    p_status.add_argument("--base-dir", default="")
    p_status.add_argument("--dept", default="")
    p_status.add_argument("--dept-password", default="")
    p_status.set_defaults(func=cmd_status)

    p_serve = sub.add_parser("serve", help="Run persistent search HTTP service")
    p_serve.add_argument("--host", default=os.environ.get("ARCHON_HOST", "127.0.0.1"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("ARCHON_PORT", "8765")))
    p_serve.set_defaults(func=cmd_serve)

    p_watch = sub.add_parser("watch", help="Watch a folder and upload new files")
    p_watch.add_argument("--base-dir", default=os.environ.get("ARCHON_BASE_DIR", "archon-data"))
    p_watch.add_argument("--dept", default=os.environ.get("ARCHON_DEPARTMENT", "general"))
    p_watch.add_argument("--dept-password", default=os.environ.get("ARCHON_DEPT_PASSWORD", ""))
    p_watch.add_argument("--boss-password", default=os.environ.get("ARCHON_BOSS_PASSWORD", ""))
    p_watch.add_argument("--watch-dir", default=os.environ.get("ARCHON_WATCH_DIR", "inbox"))
    p_watch.add_argument("--interval", type=float, default=5.0)
    p_watch.add_argument("--once", action="store_true")
    p_watch.set_defaults(func=cmd_watch)

    p_doctor = sub.add_parser("doctor", help="Check Archon environment and dependencies")
    p_doctor.add_argument("--base-dir", default=os.environ.get("ARCHON_BASE_DIR", "archon-data"))
    p_doctor.add_argument("--dept", default=os.environ.get("ARCHON_DEPARTMENT", "general"))
    p_doctor.add_argument("--dept-password", default=os.environ.get("ARCHON_DEPT_PASSWORD", ""))
    p_doctor.add_argument("--load-model", action="store_true")
    p_doctor.set_defaults(func=cmd_doctor)

    p_backup = sub.add_parser("backup", help="Back up Archon data to a zip")
    p_backup.add_argument("--base-dir", default=os.environ.get("ARCHON_BASE_DIR", "archon-data"))
    p_backup.add_argument("--output", default="")
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="Restore Archon data from a zip")
    p_restore.add_argument("archive")
    p_restore.add_argument("--target", required=True)
    p_restore.add_argument("--force", action="store_true")
    p_restore.set_defaults(func=cmd_restore)

    p_rebuild = sub.add_parser("rebuild", help="Rebuild LanceDB/docmeta from encrypted records")
    p_rebuild.add_argument("--base-dir", default=os.environ.get("ARCHON_BASE_DIR", "archon-data"))
    p_rebuild.add_argument("--dept", default=os.environ.get("ARCHON_DEPARTMENT", "general"))
    p_rebuild.add_argument("--dept-password", default="")
    p_rebuild.set_defaults(func=cmd_rebuild)

    p_retry = sub.add_parser("retry", help="Retry dead-letter or review tasks")
    p_retry.add_argument("--base-dir", default=os.environ.get("ARCHON_BASE_DIR", "archon-data"))
    p_retry.add_argument("--dept", default=os.environ.get("ARCHON_DEPARTMENT", "general"))
    p_retry.add_argument("--dept-password", default="")
    p_retry.add_argument("--mode", default="dead_letter", choices=["dead_letter", "review"])
    p_retry.set_defaults(func=cmd_retry)

    p_finalize = sub.add_parser("finalize", help="Finalize all scanned structured records")
    p_finalize.add_argument("--base-dir", default="")
    p_finalize.add_argument("--dept", default="")
    p_finalize.add_argument("--boss-password", default="")
    p_finalize.add_argument("--dept-password", default="")
    p_finalize.set_defaults(func=cmd_finalize)

    p_wiki = sub.add_parser("wiki", help="Collect wiki compilation data")
    p_wiki.add_argument("--base-dir", default="")
    p_wiki.add_argument("--dept", default="")
    p_wiki.add_argument("--dept-password", default="")
    p_wiki.set_defaults(func=cmd_wiki)

    p_find = sub.add_parser("find", help="Quick metadata search")
    p_find.add_argument("query")
    p_find.add_argument("--dept-password", default="")
    p_find.set_defaults(func=cmd_find)

    p_search = sub.add_parser("search", help="Full three-stage search")
    p_search.add_argument("query")
    p_search.add_argument("--dept-password", default="")
    p_search.set_defaults(func=cmd_search)

    p_report = sub.add_parser("report", help="Generate PDF/Word report")
    p_report.add_argument("doc_id")
    p_report.add_argument("--answer", required=True)
    p_report.add_argument("--title", default="Archon Report")
    p_report.add_argument("--format", default="pdf", choices=["pdf", "docx"])
    p_report.add_argument("--chart-data", default=None)
    p_report.add_argument("--table-data", default=None)
    p_report.add_argument("--dept-password", default="")
    p_report.set_defaults(func=cmd_report)

    p_verify = sub.add_parser("verify", help="Verify and save department password")
    p_verify.add_argument("--password", required=True)
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
