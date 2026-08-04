"""Persistent HTTP service for Archon RAG search and reports."""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]


def _module(name: str):
    path = ROOT / name / "scripts"
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _configure_employee(dept_password: str = "") -> None:
    base_dir = os.environ.get("ARCHON_BASE_DIR", "archon-data")
    department = os.environ.get("ARCHON_DEPARTMENT", "general")
    _module("employee-search")
    import employee_search
    employee_search.configure(
        base_dir=base_dir,
        department=department,
        password=dept_password or None,
    )


class ArchonHandler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        expected = os.environ.get("ARCHON_API_TOKEN", "")
        if not expected:
            return False
        return self.headers.get("Authorization", "") == f"Bearer {expected}"

    def _audit(self, action: str, target: str, details: dict = None) -> None:
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            import audit_log
            audit_log.log_audit(
                os.environ.get("ARCHON_BASE_DIR", "archon-data"),
                actor="api",
                action=action,
                target=target,
                details=details,
            )
        except Exception:
            pass

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._json({"status": "ok"})
            return
        if not self._authorized():
            self._json({"status": "error", "message": "unauthorized"}, 401)
            return

        _configure_employee(query.get("dept_password", [""])[0])
        _module("employee-search")
        import employee_search

        if parsed.path == "/find":
            q = query.get("q", [""])[0]
            self._audit("find", q)
            meta = employee_search._find_docmeta(q)
            if meta:
                self._json({"status": "ok", "source": "docmeta", "matches": [employee_search._public_record(r) for r in meta]})
            else:
                matched, err = employee_search._do_search_internal(q)
                self._json({"status": "ok" if matched else "empty", "matches": [employee_search._public_record(r) for r in matched], "error": err})
            return

        if parsed.path == "/search":
            q = query.get("q", [""])[0]
            self._audit("search", q)
            result = employee_search.employee_search_full_pipeline(q)
            primary = result.get("primary_results", [])
            output = {
                "query": q,
                "stages_executed": result.get("stages_executed", []),
                "resolution": result.get("resolution", ""),
                "main_results": [employee_search._public_record(r) for r in primary[:10]],
            }
            try:
                sys.path.insert(0, str(ROOT / "scripts"))
                import query_rewriter
                output["citations"] = query_rewriter.build_citations(primary)
            except Exception:
                pass
            self._json(output)
            return

        if parsed.path == "/status":
            self._audit("status", "pipeline")
            _module("boss-upload")
            import boss_upload
            base = os.environ.get("ARCHON_BASE_DIR", "archon-data")
            department = os.environ.get("ARCHON_DEPARTMENT", "general")
            dept_password = os.environ.get("ARCHON_DEPT_PASSWORD", "")
            boss_upload.configure(base_dir=base, departments={department: dept_password})
            sys.path.insert(0, str(ROOT / "scripts"))
            import task_status
            self._json({"pipeline": boss_upload.boss_get_pending_status(), "tasks": task_status.list_task_status(base)})
            return

        self._json({"status": "error", "message": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/report":
            self._json({"status": "error", "message": "not found"}, 404)
            return
        if not self._authorized():
            self._json({"status": "error", "message": "unauthorized"}, 401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json({"status": "error", "message": "invalid JSON"}, 400)
            return
        self._audit("report", payload.get("doc_id", ""), {"format": payload.get("format", "pdf")})
        _module("report-generator")
        import report_generator
        try:
            path = report_generator.generate_report(
                doc_id=payload.get("doc_id", ""),
                answer=payload.get("answer", ""),
                title=payload.get("title", "Archon Report"),
                fmt=payload.get("format", "pdf"),
            )
            self._json({"status": "ok", "path": path})
        except Exception as exc:
            self._json({"status": "error", "message": str(exc)}, 500)

    def log_message(self, format, *args):
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Archon persistent search service")
    parser.add_argument("--host", default=os.environ.get("ARCHON_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ARCHON_PORT", "8765")))
    args = parser.parse_args()
    return run_server(args.host, args.port)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> int:
    if not os.environ.get("ARCHON_API_TOKEN"):
        raise RuntimeError("ARCHON_API_TOKEN must be set before starting the server")
    server = ThreadingHTTPServer((host, port), ArchonHandler)
    print(f"Archon server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
