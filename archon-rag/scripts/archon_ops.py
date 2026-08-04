"""Operational helpers: backup/restore, index rebuild, dead-letter retry."""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def backup(base_dir: str, output_path: str = "") -> dict:
    base = Path(base_dir).resolve()
    if not base.is_dir():
        return {"status": "error", "message": f"base_dir not found: {base}"}
    if not output_path:
        output_path = str(base.parent / f"archon-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip")
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "uploads")]
            for fname in files:
                if fname.endswith(".pyc") or fname.endswith(".lock"):
                    continue
                full = Path(root) / fname
                rel = full.relative_to(base).as_posix()
                zf.write(full, rel)
                count += 1
    return {"status": "ok", "path": str(out), "file_count": count}


def restore(archive_path: str, target_dir: str, force: bool = False) -> dict:
    archive = Path(archive_path).resolve()
    target = Path(target_dir).resolve()
    if not archive.is_file():
        return {"status": "error", "message": f"archive not found: {archive}"}
    if target.exists() and any(target.iterdir()) and not force:
        return {"status": "error", "message": "target directory is not empty; pass --force to overwrite"}
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zf:
        for name in zf.namelist():
            resolved = (target / name).resolve()
            if not resolved.is_relative_to(target):
                return {"status": "error", "message": f"unsafe path in archive: {name}"}
        zf.extractall(target)
    return {"status": "ok", "path": str(target)}


def _split_fragments(full_text: str, fragments: list = None) -> list:
    if fragments:
        return [str(f) for f in fragments if str(f).strip()]
    parts = re.split(r"\n---\n", full_text or "")
    return [p.strip() for p in parts if p.strip()]


def rebuild_index(base_dir: str, department: str, dept_password: str) -> dict:
    base = Path(base_dir).resolve()
    os.environ["KNOWLEDGE_RAG_DIR"] = str(base / "shared" / "knowledge_rag")

    sys.path.insert(0, str(ROOT / "secure-storage" / "scripts"))
    from secure_store import pw_decrypt_store
    records = pw_decrypt_store(
        dept_password,
        base_dir=str(base),
        department=department,
        encrypted_dir=str(base / "shared" / "encrypted"),
    )

    sys.path.insert(0, str(ROOT / "knowledge-rag" / "scripts"))
    import knowledge_rag_wrapper as kr
    kr.configure(str(base / "shared" / "knowledge_rag"))
    kr.set_dept_password(department, dept_password)

    db = kr._get_db()
    safe = kr._sanitize_dept_name(department)
    for table_name in [f"docs_{safe}", f"docmeta_{safe}"]:
        try:
            db.drop_table(table_name)
        except Exception:
            pass

    sys.path.insert(0, str(ROOT / "boss-upload" / "scripts"))
    import boss_upload
    boss_upload.configure(base_dir=str(base), departments={department: dept_password})

    indexed_chunks = 0
    docmeta_count = 0
    for record in records:
        filename = record.get("source_filename", "unknown")
        summary = record.get("summary", "")
        tags = record.get("tags", [])
        entities = record.get("entities", [])
        key_data = record.get("key_data", {})
        fragments = _split_fragments(record.get("full_text", ""), record.get("fragments"))
        for fragment in fragments:
            lines = fragment.splitlines()
            heading = "Section"
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#") or re.match(r"^[\d.]+", stripped):
                    heading = stripped.lstrip("#").strip() or heading
                    break
            content = f"# {filename}\n摘要：{summary}\n标签：{', '.join(tags)}\n\n## {heading}\n{fragment}"
            indexed = kr.kr_add_document(
                content=content,
                filename=filename,
                department=department,
                summary=summary,
                tags=tags,
                entities=entities,
                key_data=key_data,
            )
            if indexed.get("status") == "success":
                indexed_chunks += 1

        sys.path.insert(0, str(ROOT / "knowledge-rag" / "scripts"))
        from index_generator import store_doc_meta
        quality_summary = record.get("quality_summary", "")
        if not quality_summary and key_data:
            quality_summary = ", ".join(f"{k} {v}" for k, v in list(key_data.items())[:5])
        process = record.get("process", "")
        if not process and tags:
            process = ", ".join(tags[:5])
        store_doc_meta({
            "doc_id": record.get("id", str(uuid.uuid4())),
            "filename": filename,
            "client_name": record.get("client_name", ""),
            "project_name": record.get("project_name", ""),
            "product_capacity": record.get("product_capacity", ""),
            "quality_summary": quality_summary,
            "objective": record.get("objective", ""),
            "process": process,
            "conclusion_chunk_id": "",
            "upload_time": record.get("upload_time", ""),
        }, department)
        docmeta_count += 1
        if key_data:
            try:
                boss_upload._append_key_metrics_to_csv(department, filename, key_data, summary)
            except Exception:
                pass

    return {"status": "ok", "records": len(records), "indexed_chunks": indexed_chunks, "docmeta": docmeta_count}


def retry(base_dir: str, department: str, dept_password: str, mode: str = "dead_letter") -> dict:
    base = Path(base_dir).resolve()
    source_dir = base / ".agent_data" / mode
    structured_dir = base / ".agent_data" / "structured"
    if not source_dir.is_dir():
        return {"status": "ok", "retried": 0, "message": f"no {mode} directory"}

    sys.path.insert(0, str(ROOT / "boss-upload" / "scripts"))
    import boss_upload
    boss_upload.configure(base_dir=str(base), departments={department: dept_password})

    retried = []
    errors = []
    for fname in sorted(os.listdir(source_dir)):
        if not fname.startswith(("dead_", "review_")):
            continue
        path = source_dir / fname
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            structured = data.get("structured", data)
            pending_id = data.get("pending_id", structured.get("pending_id", ""))
            if not pending_id:
                continue
            structured.setdefault("department", department)
            with open(structured_dir / f"structured_{pending_id}.json", "w", encoding="utf-8") as f:
                json.dump(structured, f, ensure_ascii=False)
            result = boss_upload.boss_upload_step2(pending_id)
            if result.get("status") == "success":
                os.remove(path)
                retried.append(pending_id)
            else:
                errors.append({"pending_id": pending_id, "error": result.get("message")})
        except Exception as exc:
            errors.append({"file": fname, "error": str(exc)})
    return {"status": "ok", "retried": len(retried), "errors": errors, "retried_list": retried}
