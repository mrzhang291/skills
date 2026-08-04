"""Doctor command for Archon RAG environment checks."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _check(name: str, ok: bool, detail: str = "", severity: str = "error") -> dict:
    return {"name": name, "ok": ok, "detail": detail, "severity": severity}


def run_doctor(base_dir: str = "archon-data", department: str = "general",
               dept_password: str = "", load_model: bool = False) -> dict:
    checks = []
    checks.append(_check("python", sys.version_info >= (3, 10), sys.version.split()[0]))

    required_modules = [
        "lancedb", "sentence_transformers", "jieba", "fitz", "docx",
        "pdfplumber", "fpdf2", "matplotlib", "pyarrow",
    ]
    for mod in required_modules:
        checks.append(_check(f"module:{mod}", importlib.util.find_spec(mod) is not None, mod, "error"))

    base = Path(base_dir)
    required_dirs = [
        base / "private" / "originals",
        base / "shared" / "encrypted",
        base / "shared" / "knowledge_rag" / "data" / "lancedb",
        base / "shared" / "wiki",
        base / ".agent_data" / "pending",
        base / ".agent_data" / "structured",
    ]
    for d in required_dirs:
        checks.append(_check(f"dir:{d.relative_to(base)}", d.is_dir(), str(d)))

    checks.append(_check(
        "env:MINERU_API_TOKEN",
        bool(os.environ.get("MINERU_API_TOKEN")),
        "set" if os.environ.get("MINERU_API_TOKEN") else "not set",
        "warning",
    ))
    checks.append(_check(
        "env:ARCHON_BASE_DIR",
        bool(os.environ.get("ARCHON_BASE_DIR")),
        os.environ.get("ARCHON_BASE_DIR", "not set"),
        "warning",
    ))

    if load_model:
        try:
            sys.path.insert(0, str(ROOT / "knowledge-rag" / "scripts"))
            import knowledge_rag_wrapper as kr
            vec = kr._embed_text("Archon doctor")
            checks.append(_check("embedding", vec is not None, str(len(vec)) if vec else "unavailable"))
        except Exception as exc:
            checks.append(_check("embedding", False, str(exc)))

    try:
        sys.path.insert(0, str(ROOT / "boss-upload" / "scripts"))
        import boss_upload
        boss_upload.configure(base_dir=str(base), departments={department: dept_password})
        checks.append(_check("structure", boss_upload.validate_structure().get("status") == "ok", "ok"))
        status = boss_upload.boss_get_pending_status()
        checks.append(_check("queue", True, f"pending={status['pending']} processing={status['processing']} done={status['done']}"))
    except Exception as exc:
        checks.append(_check("structure", False, str(exc)))

    errors = [c for c in checks if not c["ok"] and c["severity"] == "error"]
    warnings = [c for c in checks if not c["ok"] and c["severity"] == "warning"]
    return {
        "status": "ok" if not errors else "error",
        "warnings": len(warnings),
        "errors": len(errors),
        "checks": checks,
    }
