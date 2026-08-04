"""
Ա��������ģ�飺�������ܴ洢 �� query JSON �� AI�ܽ� �� ����PDF/Word����
���� secure-storage ���ܣ����ܼ�������pdf/word-report-generator ���ܣ��������ɣ�
���� knowledge-rag�����ټ����������ʡtoken��Ա��ֻ����ժҪ��chunkƬ�Σ�

���ó־û���configure() �����뱣���� .agent_data/.emp_config.json��
�´�����Զ���ȡ��Ա�������ظ��������롣
"""
import os
import sys
import json
import csv
import uuid
from datetime import datetime
import re

# ȫ������
_config = {
    "base_dir": None,       # ����Ŀ¼����ʱ�ļ���
    "encrypted_dir": None,  # �����ļ�Ŀ¼
    "password": None,       # �����������루�Զ��־û���
    "department": None,     # ������ţ�ֻ����������store_xxx.enc��
}

_CIPHERTEXT_RUN_RE = re.compile(r"(?:[A-Za-z0-9]|[<>=;|{}\[\]~`^\\/:@#$%&*+_,.\-]){96,}")
_FERNET_TOKEN_RE = re.compile(r"\bgAAAAA[A-Za-z0-9_-]{80,}\b")


def _looks_like_ciphertext_or_binary(text: str) -> bool:
    if not text:
        return False
    s = str(text)
    if "\x00" in s:
        return True
    compact = re.sub(r"\s+", "", s)
    if len(compact) < 80:
        return False
    if _FERNET_TOKEN_RE.search(compact):
        return True
    symbol_count = sum(1 for ch in compact if ch in "<>=;|{}[]~`^\\/:@#$%&*+_@")
    ascii_count = sum(1 for ch in compact if 33 <= ord(ch) <= 126)
    cjk_count = sum(1 for ch in compact if "\u4e00" <= ch <= "\u9fff")
    return (
        len(compact) >= 120
        and symbol_count / len(compact) > 0.18
        and ascii_count / len(compact) > 0.60
        and cjk_count / len(compact) < 0.12
    )


def _sanitize_visible_text(text: str) -> str:
    if not text:
        return ""
    cleaned_lines = []
    for line in str(text).splitlines():
        if _looks_like_ciphertext_or_binary(line):
            continue
        line = _FERNET_TOKEN_RE.sub("", line)
        line = _CIPHERTEXT_RUN_RE.sub(" ", line)
        if _looks_like_ciphertext_or_binary(line):
            continue
        cleaned_lines.append(line.rstrip())
    return "\n".join(cleaned_lines).strip()


def _sanitize_obj(value):
    if isinstance(value, str):
        return _sanitize_visible_text(value)
    if isinstance(value, list):
        return [_sanitize_obj(v) for v in value if _sanitize_obj(v) not in ("", None, [], {})]
    if isinstance(value, dict):
        return {
            _sanitize_visible_text(str(k)): _sanitize_obj(v)
            for k, v in value.items()
            if _sanitize_visible_text(str(k))
        }
    return value


def _get_config_path(base_dir: str) -> str:
    return os.path.join(base_dir, ".agent_data", ".emp_config.json")


def _auto_load_config():
    """Auto-load saved config. Searches: home pointer, CWD chain, common locations."""
    if _config["password"] and _config["base_dir"]:
        return

    import glob as _glob
    search_paths = []
    if _config["base_dir"]:
        search_paths.append(_config["base_dir"])

    # 1. Home directory pointer file (most reliable)
    pointer_path = os.path.join(os.path.expanduser("~"), ".emp_base_dir.txt")
    if os.path.exists(pointer_path):
        try:
            with open(pointer_path, "r", encoding="utf-8") as f:
                home_base = f.read().strip()
            if home_base and os.path.isdir(home_base):
                search_paths.append(home_base)
        except Exception:
            pass

    # 2. CWD and parent directories
    cwd = os.getcwd()
    while cwd and len(cwd) > 3:
        if os.path.isdir(os.path.join(cwd, ".agent_data")):
            search_paths.append(cwd)
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent

    # 3. Common locations (one level deep from drives)
    for drive in ["D:\\", "E:\\", "C:\\"]:
        try:
            for entry in os.listdir(drive):
                full = os.path.join(drive, entry)
                if os.path.isdir(full) and os.path.isdir(os.path.join(full, ".agent_data")):
                    search_paths.append(full)
                    break  # one per drive is enough
        except Exception:
            pass

    for sp in search_paths:
        cp = _get_config_path(sp)
        if os.path.exists(cp):
            try:
                with open(cp, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                _config["base_dir"] = saved.get("base_dir", _config["base_dir"])
                _config["encrypted_dir"] = saved.get("encrypted_dir", _config["encrypted_dir"])
                encrypted_pwd = saved.get("password", "")
                if encrypted_pwd:
                    ss = _import_secure_store()
                    _config["password"] = ss.decrypt_value(encrypted_pwd, _config["base_dir"])
                _config["department"] = saved.get("department", _config["department"])
                return
            except Exception:
                pass

def configure(base_dir: str, encrypted_dir: str = None, password: str = None,
              department: str = None):
    """���ù���Ŀ¼�������ļ�Ŀ¼�Ͳ��ţ������ѡ���״ο��Ȳ�������ѯʱ������֤��"""
    _config["base_dir"] = base_dir
    if encrypted_dir:
        _config["encrypted_dir"] = encrypted_dir
    if department:
        _config["department"] = department
    os.makedirs(os.path.join(base_dir, ".agent_data", "pending"), exist_ok=True)

    # Always persist base config; password saved separately via verify_and_save
    cp = _get_config_path(base_dir)
    saved = {
        "base_dir": base_dir,
        "encrypted_dir": encrypted_dir,
        "department": _config["department"],
    }
    # Merge existing password if on disk
    if os.path.exists(cp):
        try:
            with open(cp, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if existing.get("password"):
                saved["password"] = existing["password"]
        except Exception:
            pass

    if password:
        _config["password"] = password
        ss = _import_secure_store()
        saved["password"] = ss.encrypt_value(password, base_dir)

    with open(cp, "w", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False)

    # Write home directory pointer for auto-discovery
    pointer_path = os.path.join(os.path.expanduser("~"), ".emp_base_dir.txt")
    try:
        with open(pointer_path, "w", encoding="utf-8") as f:
            f.write(base_dir)
    except Exception:
        pass

    return _config


def has_saved_password() -> bool:
    """��鱾���Ƿ��ѱ�������"""
    if _config["password"]:
        return True
    _auto_load_config()
    return bool(_config["password"])


def verify_and_save(password: str) -> dict:
    """
    ��֤�����Ƿ���ȷ�����Խ���store������ȷ�����浽���أ����������ʧ��
    agent�յ�need_password�������û����������룬�ٵ��˺�����֤
    """
    if not _config["base_dir"]:
        return {"status": "error", "message": "���ȵ��� configure() ���� base_dir"}
    if not _config["department"]:
        return {"status": "error", "message": "���ȵ��� configure() ���� department"}

    secure_store = _import_secure_store()
    try:
        records = secure_store.pw_decrypt_store(password, base_dir=_config["base_dir"],
                                                 department=_config["department"],
                                                 encrypted_dir=_config.get("encrypted_dir"))
        # ������ȷ�����ܺ󱣴浽���أ����ļ�������������й¶��
        _config["password"] = password
        ss = _import_secure_store()
        encrypted_pwd = ss.encrypt_value(password, _config["base_dir"])
        cp = _get_config_path(_config["base_dir"])
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({
                "base_dir": _config["base_dir"],
                "encrypted_dir": _config.get("encrypted_dir"),
                "password": encrypted_pwd,
                "department": _config["department"],
            }, f, ensure_ascii=False)
        return {"status": "ok", "message": f"������֤�ɹ����ѱ��档�� {len(records)} ����¼�ɲ�ѯ"}
    except Exception:
        return {"status": "error", "message": "�������������"}


def clear_saved_password() -> dict:
    """������ر��������"""
    _config["password"] = None
    if _config["base_dir"]:
        cp = _get_config_path(_config["base_dir"])
        if os.path.exists(cp):
            os.remove(cp)
    return {"status": "ok", "message": "�������������"}


def _get_pending_dir():
    return os.path.join(_config["base_dir"], ".agent_data", "pending")


def cleanup_old_pending(max_age_hours: int = 24):
    """�������ָ��ʱ��Ĺ¶� pending �ļ���step1 д�˵� step2 ��δ�����õĲ������"""
    pending_dir = _get_pending_dir()
    if not os.path.exists(pending_dir):
        return {"status": "ok", "cleaned": 0, "message": "�� pending Ŀ¼"}
    import time
    now = time.time()
    cleaned = 0
    for fname in os.listdir(pending_dir):
        fpath = os.path.join(pending_dir, fname)
        if not fname.startswith(("query_", "summary_")):
            continue
        try:
            age_hours = (now - os.path.getmtime(fpath)) / 3600
            if age_hours > max_age_hours:
                os.remove(fpath)
                cleaned += 1
        except Exception:
            pass
    return {"status": "ok", "cleaned": cleaned, "message": f"������ {cleaned} ������ pending �ļ�"}


def _import_knowledge_rag():
    """????knowledge-rag wrapper????LanceDB????????"""
    import sys
    # ? employee-search/scripts/ ../../ ? knowledge-rag/scripts/
    skills_base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    kr_wrapper_path = os.path.join(skills_base, "knowledge-rag", "scripts")
    if kr_wrapper_path not in sys.path:
        sys.path.insert(0, kr_wrapper_path)

    # Ensure KNOWLEDGE_RAG_DIR env var is set before import (controls LanceDB path)
    base_dir = _config.get("base_dir", "")
    if base_dir:
        kr_dir = os.path.join(base_dir, "shared", "knowledge_rag")
        os.environ["KNOWLEDGE_RAG_DIR"] = kr_dir

    import knowledge_rag_wrapper as kr_mod
    # Always reconfigure with correct path (even if previously imported with wrong path)
    if base_dir:
        kr_mod.configure(knowledge_rag_dir=os.path.join(base_dir, "shared", "knowledge_rag"))
    return kr_mod



def _import_report_generator():
    import sys
    skills_base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    rpt_path = os.path.join(skills_base, "report-generator", "scripts")
    if rpt_path not in sys.path:
        sys.path.insert(0, rpt_path)
    import report_generator
    return report_generator


def _import_secure_store():
    import sys
    skills_base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ss_path = os.path.join(skills_base, "secure-storage", "scripts")
    if ss_path not in sys.path:
        sys.path.insert(0, ss_path)
    import secure_store
    return secure_store


def employee_search_step1(query: str) -> dict:
    """
    �������̵�һ�����ؼ��ʼ��� �� дquery JSON �� �ȴ�AI�ܽ�
    ���� query_id��agent��ȡquery�ļ�����AI�ܽ�
    """
    _auto_load_config()
    if not _config["base_dir"]:
        return {"status": "error", "message": "���ȵ��� configure() ���� base_dir"}
    # Allow passwordless directory browsing; only require password for deep chunk search
    password_available = bool(_config.get("password", ""))
    if not _config["department"]:
        return {"status": "error", "message": "���ȵ��� configure() ���ò�������"}

    matched, search_error = _do_search_internal(query)
    if not matched:
        if not password_available and not search_error:
            return {
                "status": "empty",
                "query": query,
                "matched_count": 0,
                "message": "No results found. Try providing department password for deep search.",
                "need_password": True,
            }
        return {
            "status": "empty",
            "query": query,
            "matched_count": 0,
            "message": "No matching records found",
            "search_error": search_error,
        }

    # дquery JSON
    pending_dir = _get_pending_dir()
    os.makedirs(pending_dir, exist_ok=True)
    query_id = str(uuid.uuid4())[:8]
    query_path = os.path.join(pending_dir, f"query_{query_id}.json")

    # Separate: lightweight summaries (all) + full texts (top 5 only)
    summaries = []
    for rec in matched:
        summaries.append({
            "id": rec.get("id", ""),
            "source_filename": rec.get("source_filename", ""),
            "department": rec.get("department", ""),
            "summary": rec.get("summary", "")[:200],
            "tags": rec.get("tags", []),
            "key_data": rec.get("key_data", {}),
            "score": rec.get("kr_score", 0),
            "search_method": rec.get("search_method", ""),
        })
    top_full = matched[:5]  # only top 5 get full text

    query_data = {
        "query_id": query_id,
        "query": query,
        "matched_count": len(matched),
        "top_full_count": len(top_full),
        "summaries": summaries,           # all results, lightweight
        "top_full_records": top_full,     # top 5 only, with full_text
        "status": "pending_ai_summary",
    }
    with open(query_path, "w", encoding="utf-8") as f:
        json.dump(query_data, f, ensure_ascii=False)

    return {
        "status": "pending_ai_summary",
        "query_id": query_id,
        "query_path": query_path,
        "query": query,
        "matched_count": len(matched),
        "summaries": summaries,
        "top_full_records": top_full,
        "message": f"������ {len(matched)} ����¼����agent�ȶ�summariesɸѡ2-3������صģ��ٶ���Ӧtop_full_records��full_text���ɱ���",
    }


def quick_search(query: str) -> dict:
    """
    һ���������״�ʹ�����ޱ�������ʱ���� need_password��agentӦ���û����������롣
    �ѱ�������ʱֱ�ӷ���ƥ���¼��

    ����ȫ��ơ�ֻ����ժҪ����Ϣ���ļ�������ǩ��AIժҪ�ȣ�����������ԭ��ȫ�ġ�
    Ա����Ҫ��ϸ����ʱ������ͨ�� employee_search_step1 �� AI�ܽ� �� step2 ���̣�
    ��AI���ڴ��ж�ȡԭ�ĺ����ɴ𰸣�ԭ�Ĳ���¶��Ա����
    """
    _auto_load_config()
    if not _config["base_dir"]:
        return {"need_password": False, "status": "error", "message": "���ȵ��� configure(base_dir=..., department=...) ����"}
    if not _config["password"]:
        # Try directory-first without password - search ALL wiki pages
        try:
            dir_result = employee_search_directory_first(query)
            wiki_records = []
            seen = set()
            
            # Helper: add record if not duplicate
            def _add_wiki_record(path, content, wtype):
                fname = os.path.basename(path).replace(".md", "")
                if fname in seen:
                    return
                seen.add(fname)
                wiki_records.append(_public_record({
                    "id": "wiki_" + fname[:20],
                    "source_filename": fname,
                    "department": _config.get("department", ""),
                    "summary": _extract_field(content, "summary") or content[:200].replace("\n", " "),
                    "client_name": _extract_field(content, "client_name"),
                    "project_name": _extract_field(content, "project_name"),
                    "process": _extract_field(content, "process"),
                    "objective": _extract_field(content, "objective"),
                    "tags": _extract_tags(content),
                    "key_data": _extract_keydata(content),
                    "full_text": content,
                    "search_method": "wiki_" + wtype,
                    "kr_score": 0.90,
                    "importance": "main" if wtype == "summary" else "mention",
                }))
            
            # Prioritize: summaries > concepts > entities > index
            for s in dir_result.get("summary_results", []):
                _add_wiki_record(s["path"], s["content"], "summary")
            for c in dir_result.get("concept_results", []):
                _add_wiki_record(c["path"], c["content"], "concept")
            for e in dir_result.get("entity_results", []):
                _add_wiki_record(e["path"], e["content"], "entity")
            for d in dir_result.get("directory_results", []):
                _add_wiki_record(d["path"], d["content"], "index")
            
            if wiki_records:
                return {
                    "need_password": False,
                    "status": "ok",
                    "query": query,
                    "matched_count": len(wiki_records),
                    "records": wiki_records,
                    "resolution": "directory_only",
                    "hint": "Directory-only results. Provide password for deep chunk search.",
                }
        except Exception:
            pass
        return {
            "need_password": True,
            "department": _config.get("department", ""),
            "message": f"First time querying [{_config.get('department', '')}] department. Provide password for deep search, or browse directory-only results.",
        }
    records, search_error = _do_search_internal(query)
    if not records:
        return {"need_password": False, "status": "empty", "query": query, "matched_count": 0, "records": [], "search_error": search_error}
    # Apply _public_record to all results
    safe_records = [_public_record(r) for r in records]
    return {
        "need_password": False,
        "status": "ok",
        "query": query,
        "matched_count": len(safe_records),
        "records": safe_records,
    }

def _do_search_internal(query: str) -> tuple:
    """
    �ڲ�����������ʹ�� knowledge-rag ���ټ�����������ܣ�ֻ����chunkƬ�Σ���

    �������̣�
      1. kr_search �� ���� chunk Ƭ�Σ�token ���ļ��ͣ�Ա��ֻ����ժҪ�����ݣ�
      2. �� kr �޽���������� �� fallback �� secure_store ���ܼ�����������ݣ�

    ����ģʽ���ظ�ʽͳһ�������� _public_record �淶��
    """
    department = _config.get("department", "")
    password = _config.get("password", "")
    rag_error = None

    # === 目录导航 (Document Brain Layer 1) ===
    wiki_hits = []
    try:
        wiki_dir = _get_wiki_dir()
        if wiki_dir:
            indices = _read_index_files(wiki_dir)
            query_lower = query.lower()
            dept_wiki = os.path.join(wiki_dir, department)
            for idx in indices:
                if query_lower in idx["content"].lower():
                    wiki_hits.append({"type": "index", "path": idx["path"], "content": idx["content"][:800]})
            # Search summaries
            summaries_dir = os.path.join(dept_wiki, "summaries")
            if os.path.isdir(summaries_dir):
                for fname in os.listdir(summaries_dir):
                    fpath = os.path.join(summaries_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as sf:
                            sc = sf.read()
                        if query_lower in sc.lower():
                            wiki_hits.append({"type": "summary", "path": fpath, "content": sc[:800]})
                    except Exception:
                        pass
            # Search concepts
            concepts_dir = os.path.join(dept_wiki, "concepts")
            if os.path.isdir(concepts_dir):
                for fname in os.listdir(concepts_dir):
                    fpath = os.path.join(concepts_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as cf:
                            cc = cf.read()
                        if query_lower in cc.lower():
                            wiki_hits.append({"type": "concept", "path": fpath, "content": cc[:800]})
                    except Exception:
                        pass
            # Search entities
            entities_dir = os.path.join(dept_wiki, "entities")
            if os.path.isdir(entities_dir):
                for fname in os.listdir(entities_dir):
                    fpath = os.path.join(entities_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as ef:
                            ec = ef.read()
                        if query_lower in ec.lower():
                            wiki_hits.append({"type": "entity", "path": fpath, "content": ec[:800]})
                    except Exception:
                        pass
    except Exception:
        pass

    # ���� ģʽ1��knowledge-rag ���������ȣ�����ܿ����� ����������������������������������������������
    try:
        kr = _import_knowledge_rag()
        # ע�Ჿ�����루���ܼ���chunk�ļ���Ҫ��
        if password:
            kr.set_dept_password(department, password)
        variants = [query]
        try:
            scripts_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + os.sep + "scripts"
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import query_rewriter
            variants = query_rewriter.expand_query(query)
        except Exception:
            pass
        combined = {}
        for variant in variants:
            variant_result = kr.kr_search(
                query=variant,
                department=department,
                max_results=20,
                hybrid_alpha=0.5,
                min_score=0.1,
            )
            if variant_result.get("status") == "success":
                for item in variant_result.get("results", []):
                    rid = item.get("record_id", "")
                    if rid and rid not in combined:
                        combined[rid] = item
        kr_result = {"status": "success", "results": list(combined.values())}
        # Merge wiki hits into kr results with structured extraction
        if wiki_hits:
            if kr_result.get('status') != 'success' or not kr_result.get('results'):
                kr_result = {'status': 'success', 'results': []}
            for wh in wiki_hits:
                wh_id = 'wiki_' + os.path.basename(wh['path']).replace('.md', '')[:20]
                wh_content = wh['content']
                kr_result['results'].append({
                    'record_id': wh_id,
                    'filename': os.path.basename(wh['path']),
                    'department': department,
                    'content': wh_content,
                    'summary': _extract_field(wh_content, 'summary') or wh_content[:200],
                    'score': 0.95,
                    'search_method': 'wiki_' + wh['type'],
                    'tags': _extract_tags(wh_content),
                    'key_metrics': _extract_keydata(wh_content),
                    'client_name': _extract_field(wh_content, 'client_name'),
                    'project_name': _extract_field(wh_content, 'project_name'),
                    'process': _extract_field(wh_content, 'process'),
                })

        if kr_result.get("status") == "success" and kr_result.get("results"):
            # ת�� kr ���Ϊ���ݸ�ʽ
            records = []
            for item in kr_result["results"]:
                content = item.get("content", "")
                # �� chunk �����н���Ԫ��Ϣ��kr_add_document д���ͷ����
                # ��ʽ��# �ļ��� / ���ţ�xxx / ժҪ��xxx / ��ǩ��xxx\n...\n=== �ĵ�ȫ�� ===\n...
                record = {
                    "id": item.get("record_id", ""),   # ��ʱ�� source �� id
                    "source_filename": item.get("filename", "δ֪�ļ�"),
                    "department": item.get("department", department),
                    "upload_time": "",
                    "tags": [],
                    "summary": "",
                    "entities": [],
                    "key_data": {},
                    "full_text": content,  # chunkƬ�Σ���ԭ��
                    "search_method": item.get("search_method", "kr"),
                    "kr_score": item.get("score", 0),
                }
                # ���Դ�����ͷ������ժҪ/��ǩ
                lines = content.split("\n")
                for line in lines:
                    if line.startswith("ժҪ��"):
                        record["summary"] = line[3:].strip()
                    elif line.startswith("��ǩ��"):
                        record["tags"] = [t.strip() for t in line[3:].split(",")]
                    elif line.startswith("ʵ�壺"):
                        record["entities"] = [e.strip() for e in line[3:].split(",")]
                records.append(record)
            # === Self-Doubt Round (Document Brain Layer 3) ===
            if records and len(records) < 15:
                try:
                    import re as _sd_re
                    core_terms = _sd_re.findall(r'[A-Za-z0-9.%+~<>=-]+|[\u4e00-\u9fff]{2,}', query)
                    core_terms = [t for t in core_terms if len(t) >= 2][:5]
                    if core_terms:
                        primary_ids = {r.get("id", ""): r for r in records}
                        for term in core_terms:
                            sd = kr.kr_self_doubt_search(term, department=department, min_score=0.1)
                            for rec in sd.get("results", []):
                                rid = rec.get("record_id", "")
                                if rid and rid not in primary_ids:
                                    rec["kr_score"] = rec.get("score", 0)
                                    rec["search_method"] = "self_doubt"
                                    rec["id"] = rid
                                    rec["source_filename"] = rec.get("filename", "")
                                    rec["full_text"] = rec.get("content", "")
                                    rec["importance"] = "mention"
                                    records.append(rec)
                                    primary_ids[rid] = rec
                        for r in records:
                            if "importance" not in r:
                                r["importance"] = "main"
                except Exception:
                    pass

            return records, None
    except Exception as e:

        rag_error = "RAG?????"
    return [], rag_error


def _find_docmeta(query: str) -> list:
    """Search the 7-field docmeta index for structured document matches."""
    _auto_load_config()
    department = _config.get("department", "")
    if not department:
        return []
    try:
        kr = _import_knowledge_rag()
        index_dir = os.path.dirname(kr.__file__)
        if index_dir not in sys.path:
            sys.path.insert(0, index_dir)
        from index_generator import search_doc_meta
        result = search_doc_meta(query, department, max_results=20)
        output = []
        for row in result.get("results", []):
            output.append({
                "id": row.get("doc_id", ""),
                "source_filename": row.get("filename", ""),
                "department": department,
                "upload_time": row.get("upload_time", ""),
                "client_name": row.get("client_name", ""),
                "project_name": row.get("project_name", ""),
                "product_capacity": row.get("product_capacity", ""),
                "quality_summary": row.get("quality_summary", ""),
                "objective": row.get("objective", ""),
                "process": row.get("process", ""),
                "tags": [],
                "summary": "",
                "entities": [],
                "key_data": {},
                "search_method": "docmeta",
                "kr_score": 0.95,
                "importance": "main",
            })
        return output
    except Exception:
        return []


# ── 按 record_id 精确查找（drill 命令专用） ──

def _do_drill_by_id(doc_id: str) -> tuple:
    """
    按 record_id 精确查找文档。与 _do_search_internal 不同，
    这里做的是 ID 精确匹配而非关键词搜索。
    返回 (records, error)
    """
    department = _config.get("department", "")
    password = _config.get("password", "")

    try:
        kr = _import_knowledge_rag()
        if password:
            kr.set_dept_password(department, password)

        # 用 BM25 搜索 doc_id 作为关键词（LanceDB 不支持按 id 直接查）
        kr_result = kr.kr_search(
            query=doc_id,
            department=department,
            max_results=5,
            min_score=0.0,
            mode="bm25",
        )
        if kr_result.get("status") == "success" and kr_result.get("results"):
            records = []
            for item in kr_result["results"]:
                content = item.get("content", "")
                record = {
                    "id": item.get("record_id", ""),
                    "source_filename": item.get("filename", "未知文件"),
                    "department": item.get("department", department),
                    "upload_time": "",
                    "tags": [],
                    "summary": "",
                    "entities": [],
                    "key_data": {},
                    "full_text": content,
                    "search_method": item.get("search_method", "drill"),
                    "kr_score": item.get("score", 0),
                }
                # 解析内容头部元信息
                for line in content.split("\n"):
                    if line.startswith("摘要："):
                        record["summary"] = line[3:].strip()
                    elif line.startswith("标签："):
                        record["tags"] = [t.strip() for t in line[3:].split(",")]
                    elif line.startswith("实体："):
                        record["entities"] = [e.strip() for e in line[3:].split(",")]
                records.append(record)
            return records, None
    except Exception as e:
        return [], str(e)

    return [], "Document not found"

def _public_record(record: dict) -> dict:
    """
    员工安全视图：7字段结构化元数据 + 摘要，绝不暴露原文。
    """
    result = {
        "id": record.get("id", ""),
        "source_filename": record.get("source_filename", ""),
        "department": record.get("department", ""),
        "upload_time": record.get("upload_time", ""),
        "client_name": record.get("client_name", ""),
        "project_name": record.get("project_name", ""),
        "product_capacity": record.get("product_capacity", ""),
        "quality_summary": record.get("quality_summary", ""),
        "objective": record.get("objective", ""),
        "process": record.get("process", ""),
        "tags": record.get("tags", []),
        "summary": (record.get("summary", "") or "")[:300],
        "entities": record.get("entities", []),
        "key_data": record.get("key_data", {}),
        "search_method": record.get("search_method", ""),
        "kr_score": record.get("kr_score", 0),
        "importance": record.get("importance", ""),
    }
    return result

def _extract_field(content: str, field: str, default: str = "") -> str:
    """Extract a field value from wiki markdown content.

    Handles formats:
      **Client**: value
      ### Process\n**value**
      | Client | value |
    """
    import re as _re

    if field == "client_name":
        m = _re.search(r'\*\*(?:甲方|客户|Client|Client Name)\*\*[:：]\s*(.+?)(?:\n|\||\*\*|$)', content)
        if m:
            val = m.group(1).strip()
            val = _re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', val)
            return _re.sub(r'\*\*([^*]+)\*\*', r'\1', val)

    elif field == "project_name":
        m = _re.search(r'\*\*(?:项目名称|项目|Project|Project Name)\*\*[:：]\s*(.+?)(?:\n|\||\*\*|$)', content)
        if m:
            val = m.group(1).strip()
            val = _re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', val)
            return _re.sub(r'\*\*([^*]+)\*\*', r'\1', val)

    elif field == "process":
        m = _re.search(r'\*\*(?:工艺|流程|方法|试验工艺|处理工艺|工艺路线|关键工艺|Process|Method|Methodology)\*\*[:：]\s*(.+?)(?:\n|\*\*|$)', content)
        if m:
            val = m.group(1).strip()
            val = _re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', val)
            return _re.sub(r'\*\*([^*]+)\*\*', r'\1', val)
        m = _re.search(r'###\s*(?:工艺|流程|方法|试验工艺|处理工艺|工艺路线|Process|Method|Methodology)\s*\n+\*\*(.+?)\*\*', content)
        if m:
            return _re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', m.group(1).strip())
        m = _re.search(r'###\s*(?:工艺|流程|方法|试验工艺|处理工艺|工艺路线|Process|Method|Methodology)\s*\n+([^-\n][^\n]+)', content)
        if m:
            return m.group(1).strip()

    elif field == "objective":
        m = _re.search(r'\*\*(?:目标|目的|试验目标|试验目的|Objective|Objectives|Goal|Goals)\*\*[:：]\s*(.+?)(?:\n|\*\*|$)', content)
        if m:
            val = m.group(1).strip()
            return _re.sub(r'\*\*([^*]+)\*\*', r'\1', val)
        m = _re.search(r'###\s*(?:目标|目的|试验目标|试验目的|Objective|Objectives|Goal|Goals)\s*\n+([^-\n][^\n]+)', content)
        if m:
            return m.group(1).strip()

    elif field == "summary":
        m = _re.search(r'\*\*(?:摘要|Summary)\*\*[:：]\s*(.+?)(?:\n|\*\*|$)', content)
        if m:
            return m.group(1).strip()
        for para in _re.split(r'\n{2,}', content):
            para = para.strip()
            if para and not para.startswith('#') and not para.startswith('|') and len(para) > 30:
                return _re.sub(r'\*\*([^*]+)\*\*', r'\1', para[:300])

    return default


def _extract_tags(content: str) -> list:
    """Extract tags from wiki content.

    Supports **Keywords** / **Tags** fields plus generic uppercase abbreviations.
    """
    import re as _re
    result = []

    m = _re.search(r'\*\*(?:关键词|标签|关键工艺|Keywords|Tags)\*\*[:：]\s*(.+?)(?:\n|\*\*|$)', content)
    if m:
        raw = m.group(1).strip()
        result.extend(t.strip() for t in _re.split(r'[,，/、|·]', raw) if t.strip())

    if not result:
        m2 = _re.search(r'(?:关键词|标签|关键工艺|Keywords|Tags)[:：]\s*(.+?)(?:\n|$)', content)
        if m2:
            raw = m2.group(1).strip()
            result.extend(t.strip() for t in _re.split(r'[,，/、|·]', raw) if t.strip())

    if not result:
        m3 = _re.search(r'###\s*(?:工艺|流程|方法|试验工艺|处理工艺|Process|Method|Methodology)\s*\n+\*\*(.+?)\*\*', content)
        if m3:
            raw = m3.group(1).strip()
            result.extend(t.strip() for t in _re.split(r'[,，/、|·]', raw) if t.strip())

    if not result:
        abbrs = _re.findall(r'\b[A-Z][A-Z0-9&/+/-]{1,9}\b', content)
        abbrs = [a for a in abbrs if a not in {"AI", "API", "JSON", "PDF", "HTML", "HTTP"}]
        if abbrs:
            result = list(set(abbrs))

    return result


def _extract_keydata(content: str) -> dict:
    """Extract key metric lines from wiki content in a generic way."""
    import re as _re
    result = {}
    for m in _re.finditer(
        r'([A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff/_\-]{1,30})\s*[:：=]\s*'
        r'([\d.,]+[〜~\-–—]*\s*[\d.,]*\s*(?:[A-Za-z%μµ°/0-9m³²⁻¹/·]+)?)',
        content
    ):
        full = m.group(0)
        val = m.group(2).strip()
        key = full[:full.index(val)].rstrip(":=： ")
        if key and val:
            result[key.strip()] = val
    return result



def _get_wiki_dir() -> str:
    """Resolve wiki directory path from config."""
    base = _config.get("base_dir", "")
    # Try shared/wiki first, then knowledge_rag/wiki
    candidates = [
        os.path.join(base, "shared", "wiki"),
        os.path.join(base, "knowledge_rag", "wiki"),
        os.path.join(base, "..", "shared", "wiki"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None

def _read_index_files(wiki_dir: str) -> list:
    """Read all index_*.md files from wiki directory. Returns list of {path, content}."""
    indices = []
    dept = _config.get("department", "")
    dept_wiki = os.path.join(wiki_dir, dept)
    if not os.path.isdir(dept_wiki):
        return indices
    for fname in os.listdir(dept_wiki):
        if (fname == "index.md" or fname.startswith("index_")) and fname.endswith(".md"):
            fpath = os.path.join(dept_wiki, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                indices.append({"path": fpath, "content": content})
            except Exception:
                pass
    return indices

def employee_search_directory_first(query: str) -> dict:
    """
    Stage 1: Directory First Navigation.
    Reads AI-compiled index.md files, matches relevant documents/sections.
    Returns dict with matched indices, concept pages, summary pages.
    80% of queries should resolve here.
    """
    wiki_dir = _get_wiki_dir()
    if not wiki_dir:
        return {"status": "no_directory", "message": "AI-compiled directory not found. Run boss-upload first.",
                "fallback": "use hybrid search instead"}

    indices = _read_index_files(wiki_dir)
    if not indices:
        return {"status": "no_directory", "message": "No index files found in wiki directory.",
                "fallback": "use hybrid search instead"}

    # Match query against index content
    dept = _config.get("department", "")
    dept_wiki = os.path.join(wiki_dir, dept)

    matched_docs = []
    query_lower = query.lower()

    for idx in indices:
        idx_lower = idx["content"].lower()
        if query_lower in idx_lower or any(kw in idx_lower for kw in query_lower.split()):
            matched_docs.append({
                "path": idx["path"],
                "content": idx["content"][:1000],
                "match_score": "exact" if query_lower in idx_lower else "partial",
            })

    # Also read concept pages for matching
    concept_matches = []
    concepts_dir = os.path.join(dept_wiki, "concepts")
    if os.path.isdir(concepts_dir):
        for fname in os.listdir(concepts_dir):
            fpath = os.path.join(concepts_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    c_content = f.read()
                keywords = query_lower.split()
                if query_lower in c_content.lower() or any(kw in c_content.lower() for kw in keywords if len(kw) >= 2):
                    concept_matches.append({"path": fpath, "content": c_content[:500]})
            except Exception:
                pass

    # Read summary pages for matched docs
    summary_matches = []
    summaries_dir = os.path.join(dept_wiki, "summaries")
    if os.path.isdir(summaries_dir):
        for fname in os.listdir(summaries_dir):
            fpath = os.path.join(summaries_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    s_content = f.read()
                keywords = query_lower.split()
                if query_lower in s_content.lower() or any(kw in s_content.lower() for kw in keywords if len(kw) >= 2):
                    summary_matches.append({"path": fpath, "content": s_content[:800]})
            except Exception:
                pass

    # Read entity pages for matching
    entity_matches = []
    entities_dir = os.path.join(dept_wiki, "entities")
    if os.path.isdir(entities_dir):
        for fname in os.listdir(entities_dir):
            fpath = os.path.join(entities_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    e_content = f.read()
                keywords = query_lower.split()
                if query_lower in e_content.lower() or any(kw in e_content.lower() for kw in keywords if len(kw) >= 2):
                    entity_matches.append({"path": fpath, "content": e_content[:800]})
            except Exception:
                pass

    # Quality-aware sufficient logic
    # "Sufficient" means: we found SPECIFIC document summaries, not just index mentions
    # Index matches alone are NOT sufficient (they're just pointers)
    # Summary/concept/entity matches indicate actual content found
    has_summary_match = len(summary_matches) > 0
    has_concept_match = len(concept_matches) > 0
    has_entity_match = len(entity_matches) > 0
    
    # Calculate match quality score
    quality_score = 0
    quality_score += len(summary_matches) * 3  # summaries are highest quality
    quality_score += len(concept_matches) * 2   # concepts are medium quality
    quality_score += len(entity_matches) * 1    # entities are lower quality
    # Index matches without supporting pages = low quality
    if matched_docs and not (has_summary_match or has_concept_match):
        quality_score = max(quality_score, 1)  # at least 1, but not sufficient
    
    # Sufficient only when we have actual content (summaries/concepts), not just index pointers
    sufficient = quality_score >= 3  # at least 1 summary OR 1 concept+entity OR 3 entities
    
    # Build a summary of what was found
    resolution_hint = ""
    if has_summary_match:
        resolution_hint = f"Found {len(summary_matches)} document summary(s) with matching content"
    elif has_concept_match:
        resolution_hint = f"Found {len(concept_matches)} concept page(s); summaries may have more detail"
    elif matched_docs:
        resolution_hint = f"Found {len(matched_docs)} index mention(s); deep search recommended"
    else:
        resolution_hint = "No relevant directory content found"

    return {
        "status": "success",
        "query": query,
        "matched_indices": len(matched_docs),
        "matched_concepts": len(concept_matches),
        "matched_summaries": len(summary_matches),
        "matched_entities": len(entity_matches),
        "directory_results": matched_docs,
        "concept_results": concept_matches,
        "summary_results": summary_matches,
        "entity_results": entity_matches,
        "sufficient": sufficient,
        "quality_score": quality_score,
        "resolution_hint": resolution_hint,
    }

def employee_search_stage2_hybrid(query: str) -> dict:
    """
    Stage 2: Hybrid Deep Search (BM25 + Vector RRF fusion + optional rerank).
    Only triggered when directory results are insufficient.
    """
    kr = _import_knowledge_rag()
    dept = _config.get("department", "")
    result = kr.kr_hybrid_search(query, department=dept, max_results=10)

    # Optional cross-encoder rerank on top-10 for precision.
    # Enabled explicitly with ARCHON_RERANK=1 to avoid loading a reranker by default.
    if os.environ.get("ARCHON_RERANK", "0") == "1" and result.get("results") and len(result["results"]) > 3:
        try:
            reranked = kr.kr_rerank(query, result["results"], top_n=min(10, len(result["results"])))
            if reranked.get("status") == "success" and reranked.get("results"):
                result["reranked"] = True
                result["results"] = reranked["results"]
        except Exception:
            pass  # Rerank is optional, skip on failure

    return result

def employee_search_stage3_self_doubt(query: str, keywords: list = None) -> dict:
    """
    Stage 3: Self-Doubt Round (BM25 full recall).
    Auto-triggered after Stage 2. Discovers all mentions missed by RRF.
    
    Args:
        query: Original query
        keywords: Supplementary keywords from self-doubt analysis (auto-generated)
    """
    kr = _import_knowledge_rag()
    dept = _config.get("department", "")

    # Generate self-doubt keywords: extract core terms from query
    import re as _re
    search_terms = keywords or []
    if not search_terms:
        # Auto-extract key terms
        raw = _re.findall(r'[A-Za-z0-9.%+~<>=-]+|[\u4e00-\u9fff]+', query)
        search_terms = [t for t in raw if len(t) >= 2]

    all_results = {}
    for term in search_terms[:5]:  # Max 5 keyword rounds
        r = kr.kr_self_doubt_search(term, department=dept, min_score=0.1)
        for rec in r.get("results", []):
            rid = rec.get("record_id", "")
            if rid and rid not in all_results:
                all_results[rid] = rec

    output = list(all_results.values())
    output.sort(key=lambda x: x.get("score", 0), reverse=True)

    return {
        "status": "success",
        "result_count": len(output),
        "search_terms": search_terms,
        "results": output,
        "message": f"Self-doubt round found {len(output)} additional mentions across {len(search_terms)} keyword(s)",
    }


def _wiki_self_doubt(query: str, already_found: set, wiki_dir: str, dept: str) -> dict:
    """Wiki graph-traversal self-doubt round.
    
    Follows cross-references between wiki pages to discover related content
    that the initial search missed. Extracts comparison tables, key findings.
    """
    import re as _re
    
    dept_wiki = os.path.join(wiki_dir, dept)
    if not os.path.isdir(dept_wiki):
        return {"status": "no_wiki", "cross_references": [], "new_findings": []}
    
    # Load all wiki pages
    all_pages = {}
    for subdir in ["summaries", "concepts", "entities"]:
        sd = os.path.join(dept_wiki, subdir)
        if not os.path.isdir(sd):
            continue
        for fname in os.listdir(sd):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(sd, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fp:
                    all_pages[fname] = {"path": fpath, "content": fp.read(), "type": subdir.rstrip("s")}
            except Exception:
                pass
    
    # Also load index.md
    idx_path = os.path.join(dept_wiki, "index.md")
    if os.path.exists(idx_path):
        try:
            with open(idx_path, "r", encoding="utf-8") as fp:
                all_pages["index.md"] = {"path": idx_path, "content": fp.read(), "type": "index"}
        except Exception:
            pass
    
    # Step 1: Extract cross-references from already-found pages
    cross_refs = []
    referenced_files = set()
    
    for fname in list(already_found):
        if fname not in all_pages:
            # Fuzzy match
            for af in all_pages:
                if fname.replace(".md", "") in af or af.replace(".md", "") in fname:
                    fname = af
                    break
            else:
                continue
        
        page = all_pages[fname]
        md_link_pat = _re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
        for m in md_link_pat.finditer(page["content"]):
            link_text = m.group(1)
            link_path = m.group(2)
            target_file = os.path.basename(link_path)
            if not target_file.endswith(".md"):
                continue
            
            if "/summaries/" in link_path:
                target_type = "summary"
            elif "/concepts/" in link_path:
                target_type = "concept"
            elif "/entities/" in link_path:
                target_type = "entity"
            else:
                target_type = "unknown"
            
            if target_file not in already_found and target_file in all_pages:
                referenced_files.add(target_file)
                cross_refs.append({
                    "from": fname, "link_text": link_text,
                    "target": target_file, "target_type": target_type,
                })
    
    # Step 2: Read referenced pages and extract discoveries
    new_findings = []
    key_discoveries = []
    
    for target_file in referenced_files:
        if target_file not in all_pages:
            continue
        
        page = all_pages[target_file]
        pcontent = page["content"]
        refs_from = [r for r in cross_refs if r["target"] == target_file]
        
        # Extract key findings sections
        findings = []
        findings_pat = _re.compile(r"(?:关键发现|重要发现|试验结论|核心结论)[：:]*\s*\n(.*?)(?=\n##|\n#|\Z)", _re.DOTALL)
        for fm in findings_pat.finditer(pcontent):
            finding_text = fm.group(1).strip()
            # Clean up markdown
            finding_text = _re.sub(r"\*\*([^*]+)\*\*", r"\1", finding_text)
            finding_text = finding_text.replace("\n", " ")
            if len(finding_text) > 10:
                findings.append(finding_text[:300])
        
        # Extract comparison tables
        tables = []
        in_table = False
        table_lines = []
        for line in pcontent.split("\n"):
            if line.startswith("|") and "---" not in line:
                if not in_table:
                    in_table = True
                table_lines.append(line)
            else:
                if in_table and len(table_lines) >= 2:
                    tables.append("\n".join(table_lines))
                    table_lines = []
                in_table = False
        if in_table and len(table_lines) >= 2:
            tables.append("\n".join(table_lines))
        
        discovery = {
            "source": target_file.replace(".md", ""),
            "type": page["type"],
            "referenced_by": [r["from"].replace(".md", "") for r in refs_from],
            "reference_links": [r["link_text"] for r in refs_from],
            "preview": pcontent[:300].replace("\n", " "),
        }
        
        if findings:
            discovery["key_findings"] = findings
            key_discoveries.append({"source": target_file.replace(".md", ""), "findings": findings})
        
        if tables:
            discovery["comparison_table"] = tables[0][:500]
        
        new_findings.append(discovery)
    
    # Step 3: Also extract key findings from already-found concept pages
    # (cross-document insights that individual summaries don't have)
    for fname in already_found:
        if fname not in all_pages:
            continue
        page = all_pages[fname]
        if page["type"] not in ("concept", "entity"):
            continue
        pcontent = page["content"]
        
        # Check if this page has key findings not yet captured
        findings_pat = _re.compile(r"(?:关键发现|重要发现|试验结论|核心结论)[：:]*\s*\n(.*?)(?=\n##|\n#|\Z)", _re.DOTALL)
        for fm in findings_pat.finditer(pcontent):
            finding_text = fm.group(1).strip()
            finding_text = _re.sub(r"\*\*([^*]+)\*\*", r"\1", finding_text)
            finding_text = finding_text.replace("\n", " ")
            if len(finding_text) > 10:
                # Check if this insight is already captured
                already_known = False
                for kd in key_discoveries:
                    if finding_text[:50] in kd.get("findings", [""])[0]:
                        already_known = True
                        break
                if not already_known:
                    key_discoveries.append({
                        "source": fname.replace(".md", ""),
                        "type": page["type"],
                        "findings": [finding_text[:300]],
                        "from_initial_match": True,
                    })
    
    return {
        "status": "success",
        "cross_references": cross_refs,
        "new_findings": new_findings,
        "new_count": len(new_findings),
        "pages_scanned": len(all_pages),
        "key_discoveries": key_discoveries,
        "search_terms": list(referenced_files),
    }


def _mark_importance(primary_results: list, secondary_results: list) -> dict:
    """
    Merge two-round results, marking importance:
    - Primary results (RRF) marked as "main"
    - Secondary results (self-doubt) marked as "mention"
    - Cross-referenced results get "main" priority
    """
    primary_ids = {r.get("record_id", ""): r for r in primary_results}
    secondary_ids = {r.get("record_id", ""): r for r in secondary_results}

    merged = []
    seen = set()

    # First pass: primary results �� main
    for r in primary_results:
        rid = r.get("record_id", "")
        if rid in seen:
            continue
        seen.add(rid)
        r_copy = dict(r)
        r_copy["importance"] = "main"
        merged.append(r_copy)

    # Second pass: secondary results �� mention (skip if already in primary)
    for r in secondary_results:
        rid = r.get("record_id", "")
        if rid in seen:
            continue
        seen.add(rid)
        r_copy = dict(r)
        r_copy["importance"] = "mention"
        merged.append(r_copy)

    # Sort: main first, then by score
    merged.sort(key=lambda x: (0 if x.get("importance") == "main" else 1, -(x.get("score", 0))))

    return {
        "status": "success",
        "total": len(merged),
        "main_count": sum(1 for r in merged if r.get("importance") == "main"),
        "mention_count": sum(1 for r in merged if r.get("importance") == "mention"),
        "results": merged,
    }

def employee_search_full_pipeline(query: str, auto_self_doubt: bool = True) -> dict:
    """
    Full 3-stage Document Brain search pipeline.
    
    Stage 1: Directory First (read index.md)
    Stage 2: Hybrid Search (BM25 + Vector RRF) - if needed
    Stage 3: Self-Doubt (BM25 full recall) - if enabled
    
    Returns merged results with importance marking.
    """
    _auto_load_config()
    if not _config.get("department"):
        return {"status": "error", "message": "Department not configured"}

    result = {
        "query": query,
        "department": _config.get("department", ""),
        "stages_executed": [],
        "primary_results": [],
        "secondary_results": [],
        "merged": None,
    }

    # Stage 1: Directory First
    dir_result = employee_search_directory_first(query)
    result["directory"] = dir_result
    result["stages_executed"].append("directory")

    # Always run wiki self-doubt round (even when directory is "sufficient")
    # to catch missed mentions
    wiki_dir = _get_wiki_dir()
    dept = _config.get("department", "")
    if wiki_dir:
        already_found = set()
        for s in dir_result.get("summary_results", []):
            already_found.add(os.path.basename(s["path"]))
        for c in dir_result.get("concept_results", []):
            already_found.add(os.path.basename(c["path"]))
        for e in dir_result.get("entity_results", []):
            already_found.add(os.path.basename(e["path"]))
        for d in dir_result.get("directory_results", []):
            already_found.add(os.path.basename(d["path"]))
        
        sd_result = _wiki_self_doubt(query, already_found, wiki_dir, dept)
        result["self_doubt"] = sd_result
        result["stages_executed"].append("self_doubt_wiki")
        
        if sd_result.get("new_count", 0) > 0:
            result["secondary_results"] = sd_result.get("new_findings", [])
    
    # Always run Stage 2 (Hybrid LanceDB Search) for deep retrieval
    # even when directory is sufficient - it provides complementary results
    try:
        hybrid = employee_search_stage2_hybrid(query)
    except Exception as e:
        hybrid = {"status": "error", "message": str(e), "results": []}
    
    result["primary_results"] = hybrid.get("results", [])
    result["hybrid"] = hybrid
    result["stages_executed"].append("hybrid")

    # Merge directory + hybrid into resolution
    hybrid_count = hybrid.get("result_count", len(hybrid.get("results", [])))
    dir_count = dir_result.get("matched_summaries", 0) + dir_result.get("matched_concepts", 0)
    sd_count = result.get("self_doubt", {}).get("new_count", 0)
    
    if hybrid_count > 0 and dir_count > 0:
        result["resolution"] = "merged"
        result["summary"] = f"Directory: {dir_count} wiki pages + Hybrid: {hybrid_count} LanceDB chunks. Self-doubt: {sd_count} additional mentions."
    elif dir_result.get("sufficient"):
        result["resolution"] = "directory_only"
        result["summary"] = f"Directory navigation ({dir_count} pages). Hybrid returned {hybrid_count} results. Self-doubt: {sd_count} mentions."
    elif hybrid_count > 0:
        result["resolution"] = "hybrid_only"
        result["summary"] = f"Hybrid LanceDB search returned {hybrid_count} chunks. Self-doubt: {sd_count} mentions."
    else:
        result["resolution"] = "no_results"
        result["summary"] = "No results found in directory or LanceDB."

    # Stage 3: Self-Doubt (auto)
    if auto_self_doubt:  # MANDATORY: Self-Doubt always runs, even when hybrid returns 0
        sd = employee_search_stage3_self_doubt(query)
        result["secondary_results"] = sd.get("results", [])
        result["self_doubt"] = sd
        result["stages_executed"].append("self_doubt")

        # Merge and mark importance (cap secondary to avoid massive output)
        secondary_capped = result.get("secondary_results", [])[:20]
        merged = _mark_importance(result.get("primary_results", []), secondary_capped)
        result["merged"] = merged
        result["resolution"] = "full_pipeline"
        result["secondary_results"] = secondary_capped  # Replace with capped version
    else:
        result["stages_executed"].append("self_doubt_skipped")

    return result



# ============================================================
# CLI Entry Point
# ============================================================
if __name__ == "__main__":
    import sys
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="employee-search ? Document Brain ????")
    sub = parser.add_subparsers(dest="command")

    # find ? structured metadata search
    p_find = sub.add_parser("find", help="??????????????")
    p_find.add_argument("query", help="?????")

    # search ? full pipeline search
    p_search = sub.add_parser("search", help="???????")
    p_search.add_argument("query", help="?????")

    # quick_search ? lightweight search
    p_quick = sub.add_parser("quick_search", help="????")
    p_quick.add_argument("query", help="?????")

    # drill ? deep dive into a document
    p_drill = sub.add_parser("drill", help="????")
    p_drill.add_argument("doc_id", help="??ID")

    # configure
    p_conf = sub.add_parser("configure", help="??????")
    p_conf.add_argument("--base-dir", required=True, help="????")
    p_conf.add_argument("--dept", required=True, dest="department", help="????")
    p_conf.add_argument("--password", default=None, help="????????")

    # verify
    p_verify = sub.add_parser("verify", help="????")
    p_verify.add_argument("--password", required=True, help="????")

    # cleanup
    p_clean = sub.add_parser("cleanup", help="????pending??")
    p_clean.add_argument("--max-age", type=int, default=24, help="???????")

    # report
    p_report = sub.add_parser("report", help="????")
    p_report.add_argument("doc_id", help="??ID")
    p_report.add_argument("--format", default="pdf", choices=["pdf", "docx", "txt"], help="????")
    p_report.add_argument("--answer", required=True, help="?????")
    p_report.add_argument("--title", default="数据分析报告", help="报告标题")
    p_report.add_argument("--chart-data", default=None, help="图表数据 JSON")
    p_report.add_argument("--table-data", default=None, help="表格数据 JSON")

    args = parser.parse_args()

    def _print_json(obj):
        print(_json.dumps(obj, ensure_ascii=False, indent=2))

    if args.command == "find":
        _auto_load_config()
        if not _config.get("department"):
            _print_json({"status": "error", "message": "Please configure first: configure --base-dir ... --dept ..."})
            sys.exit(1)
        meta_matches = _find_docmeta(args.query)
        if meta_matches:
            safe = [_public_record(r) for r in meta_matches]
            _print_json({
                "status": "ok",
                "query": args.query,
                "source": "docmeta",
                "matched_count": len(safe),
                "matches": safe,
            })
            sys.exit(0)
        matched, err = _do_search_internal(args.query)
        if not matched:
            # LanceDB empty: search ALL wiki pages (summaries, concepts, entities, index)
            dir_result = employee_search_directory_first(args.query)
            matches = []
            seen = set()
            
            # Prioritize: summaries > concepts > entities > index
            for s in dir_result.get("summary_results", []):
                fname = os.path.basename(s["path"]).replace(".md", "")
                if fname in seen:
                    continue
                seen.add(fname)
                s_content = s["content"]
                matches.append({
                    "source": fname,
                    "type": "summary",
                    "client_name": _extract_field(s_content, "client_name"),
                    "project_name": _extract_field(s_content, "project_name"),
                    "process": _extract_field(s_content, "process"),
                    "objective": _extract_field(s_content, "objective"),
                    "summary": _extract_field(s_content, "summary") or s_content[:300].replace("\n", " "),
                    "tags": _extract_tags(s_content),
                    "key_data": _extract_keydata(s_content),
                    "preview": s_content[:200].replace("\n", " ").replace("#", "").strip(),
                })
            
            for c in dir_result.get("concept_results", []):
                fname = os.path.basename(c["path"]).replace(".md", "")
                if fname in seen:
                    continue
                seen.add(fname)
                c_content = c["content"]
                matches.append({
                    "source": fname,
                    "type": "concept",
                    "client_name": _extract_field(c_content, "client_name"),
                    "project_name": "",
                    "process": _extract_field(c_content, "process"),
                    "summary": c_content[:300].replace("\n", " "),
                    "tags": _extract_tags(c_content),
                    "key_data": _extract_keydata(c_content),
                    "preview": c_content[:200].replace("\n", " ").replace("#", "").strip(),
                })
            
            for e in dir_result.get("entity_results", []):
                fname = os.path.basename(e["path"]).replace(".md", "")
                if fname in seen:
                    continue
                seen.add(fname)
                e_content = e["content"]
                matches.append({
                    "source": fname,
                    "type": "entity",
                    "client_name": _extract_field(e_content, "client_name"),
                    "project_name": "",
                    "process": "",
                    "summary": e_content[:300].replace("\n", " "),
                    "tags": _extract_tags(e_content),
                    "key_data": {},
                    "preview": e_content[:200].replace("\n", " ").replace("#", "").strip(),
                })
            
            # Only add index hits if nothing else found
            if not matches:
                for d in dir_result.get("directory_results", []):
                    fname = os.path.basename(d["path"]).replace(".md", "")
                    if fname in seen:
                        continue
                    seen.add(fname)
                    d_content = d["content"]
                    matches.append({
                        "source": fname,
                        "type": "index",
                        "client_name": "",
                        "project_name": "",
                        "process": "",
                        "summary": d_content[:300].replace("\n", " "),
                        "tags": _extract_tags(d_content),
                        "key_data": _extract_keydata(d_content),
                        "preview": d_content[:200].replace("\n", " ").replace("#", "").strip(),
                    })
            
            if matches:
                _print_json({
                    "status": "ok",
                    "query": args.query,
                    "source": "wiki_pages",
                    "matched_count": len(matches),
                    "resolution_hint": dir_result.get("resolution_hint", ""),
                    "matches": matches,
                })
            else:
                _print_json({
                    "status": "empty",
                    "query": args.query,
                    "matched_count": 0,
                    "matches": [],
                    "hint": "No matches in wiki pages. Try broader keywords or run boss-upload to index documents.",
                })
            sys.exit(0)
        safe = [_public_record(r) for r in matched]
        _print_json({
            "status": "ok",
            "query": args.query,
            "matched_count": len(safe),
            "matches": safe,
        })
    elif args.command == "search":
        _auto_load_config()
        if not _config.get("department"):
            _print_json({"status": "error", "message": "Please configure first: configure --base-dir ... --dept ..."})
            sys.exit(1)
        result = employee_search_full_pipeline(args.query)
        output = {
            "query": args.query,
            "department": result.get("department", ""),
            "stages_executed": result.get("stages_executed", []),
            "resolution": result.get("resolution", "unknown"),
        }
        if result.get("directory"):
            d = result["directory"]
            output["directory"] = {
                "indices": d.get("matched_indices", 0),
                "concepts": d.get("matched_concepts", 0),
                "entities": d.get("matched_entities", 0),
                "summaries": d.get("matched_summaries", 0),
                "sufficient": d.get("sufficient", False),
                "quality_score": d.get("quality_score", 0),
                "hint": d.get("resolution_hint", ""),
            }
            # Include the actual matched content (summaries first)
            dir_matches = []
            for s in d.get("summary_results", []):
                dir_matches.append({
                    "source": os.path.basename(s["path"]).replace(".md", ""),
                    "type": "summary",
                    "preview": s["content"][:300].replace("\n", " "),
                })
            for c in d.get("concept_results", []):
                dir_matches.append({
                    "source": os.path.basename(c["path"]).replace(".md", ""),
                    "type": "concept",
                    "preview": c["content"][:200].replace("\n", " "),
                })
            for e in d.get("entity_results", []):
                dir_matches.append({
                    "source": os.path.basename(e["path"]).replace(".md", ""),
                    "type": "entity",
                    "preview": e["content"][:200].replace("\n", " "),
                })
            output["directory"]["matches"] = dir_matches
        
        # Include self-doubt results
        if result.get("self_doubt"):
            sd = result["self_doubt"]
            output["self_doubt"] = {
                "terms_used": sd.get("search_terms", []),
                "new_findings": sd.get("new_count", 0),
                "pages_scanned": sd.get("pages_scanned", 0),
            }
            if sd.get("new_findings"):
                output["self_doubt"]["matches"] = []
                for f in sd["new_findings"]:
                    match = {
                        "source": f.get("source", ""),
                        "type": f.get("type", ""),
                        "referenced_by": f.get("referenced_by", []),
                        "preview": f.get("preview", ""),
                    }
                    if f.get("key_findings"):
                        match["key_findings"] = f["key_findings"]
                    if f.get("comparison_table"):
                        match["comparison_table"] = f["comparison_table"]
                    output["self_doubt"]["matches"].append(match)
            if sd.get("key_discoveries"):
                output["self_doubt"]["key_discoveries"] = sd["key_discoveries"]
        if result.get("primary_results"):
            primary_clean = []
            for r in result["primary_results"][:10]:  # Cap at 10
                rec = _public_record(r)
                if rec.get("full_text"):
                    rec["full_text"] = rec["full_text"][:500]  # Truncate long texts
                primary_clean.append(rec)
            output["main_results"] = primary_clean
            output["main_results_total"] = len(result["primary_results"])
        if result.get("secondary_results"):
            output["mention_results"] = []
            for r in result["secondary_results"][:10]:  # Cap at 10
                if isinstance(r, dict) and r.get("source"):
                    output["mention_results"].append({
                        "source": r.get("source", ""),
                        "type": r.get("type", ""),
                        "matched_term": r.get("matched_term", ""),
                        "preview": (r.get("content", "") or "")[:200].replace("\n", " "),
                    })
                elif isinstance(r, dict):
                    rec = _public_record(r)
                    if rec.get("full_text"):
                        rec["full_text"] = rec["full_text"][:300]
                    output["mention_results"].append(rec)
            output["mention_results_total"] = len(result["secondary_results"])
        if result.get("merged"):
            m = result["merged"]
            output["merged"] = {
                "total": m.get("total", 0),
                "main_count": m.get("main_count", 0),
                "mention_count": m.get("mention_count", 0),
            }
        try:
            scripts_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + os.sep + "scripts"
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import query_rewriter
            output["citations"] = query_rewriter.build_citations(result.get("primary_results", []))
        except Exception:
            pass
        _print_json(output)
    elif args.command == "quick_search":
        _auto_load_config()
        result = quick_search(args.query)
        _print_json(result)

    elif args.command == "drill":
        _auto_load_config()
        # drill: read conclusion chunk and generate AI summary
        matched, err = _do_drill_by_id(args.doc_id)
        if matched:
            safe = [_public_record(r) for r in matched[:3]]
            _print_json({
                "status": "ok",
                "doc_id": args.doc_id,
                "records": safe,
                "hint": "AI应根据records中的summary、key_data、tags字段生成100-200字总结。原文不可直接输出。",
            })
        else:
            _print_json({"status": "not_found", "doc_id": args.doc_id})

    elif args.command == "configure":
        result = configure(
            base_dir=args.base_dir,
            department=args.department,
            password=args.password,
        )
        _print_json({"status": "ok", "config": {
            "base_dir": _config.get("base_dir"),
            "department": _config.get("department"),
            "has_password": bool(_config.get("password")),
        }})

    elif args.command == "verify":
        _auto_load_config()
        result = verify_and_save(args.password)
        _print_json(result)

    elif args.command == "cleanup":
        _auto_load_config()
        result = cleanup_old_pending(max_age_hours=args.max_age)
        _print_json(result)

    elif args.command == "report":
        _auto_load_config()
        try:
            rpt = _import_report_generator()
            report_kw = {
                "doc_id": args.doc_id,
                "answer": args.answer,
                "title": args.title,
                "fmt": args.format,
            }
            if getattr(args, "chart_data", None):
                report_kw["chart_data"] = args.chart_data
            if getattr(args, "table_data", None):
                report_kw["table_data"] = args.table_data
            report_result = rpt.generate_report(**report_kw)
            _print_json(report_result)
        except Exception as e:
            _print_json({"status": "error", "message": str(e)})

    else:
        parser.print_help()
