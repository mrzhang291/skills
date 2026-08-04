"""֪ʶ�������װ�� �� ���� LanceDB����� ChromaDB������Ƕ�������ݿ⣬�������������100%�������У�����������˫·�������棺BM25 FTS + ����������� + RRF�ںϡ�

�÷�:
    import sys
    sys.path.insert(0, "knowledge-rag/scripts")
    from knowledge_rag_wrapper import kr_add_document, kr_search, kr_hybrid_search

    kr_add_document(content="...", filename="report.pdf", department="eng")
    results = kr_hybrid_search(query="throughput", department="eng")
"""

import json
import os
import sys
import uuid
import re
import hashlib
import math
from collections import Counter
from pathlib import Path

# ---- path config ----

_KNOWLEDGE_RAG_DIR = os.environ.get("KNOWLEDGE_RAG_DIR", "")
if not _KNOWLEDGE_RAG_DIR:
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    _KNOWLEDGE_RAG_DIR = os.path.dirname(_self_dir)

_DEFAULT_DOCUMENTS_DIR = os.path.join(_KNOWLEDGE_RAG_DIR, "documents")
_active_documents_dir = _DEFAULT_DOCUMENTS_DIR

os.environ.setdefault("KNOWLEDGE_RAG_DIR", _KNOWLEDGE_RAG_DIR)
if _KNOWLEDGE_RAG_DIR not in sys.path:
    sys.path.insert(0, _KNOWLEDGE_RAG_DIR)

_LANCEDB_DIR = os.path.join(_KNOWLEDGE_RAG_DIR, "data", "lancedb")

# ---- embedding model (lazy) ----

_embedder = None

def _get_embedding_model() -> str:
    """Resolve the active embedding model from env or profile."""
    try:
        scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import archon_config
        return archon_config.embedding_model()
    except Exception:
        return os.environ.get("ARCHON_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")


def _embedding_dim() -> int:
    """Return the embedding dimension used by the active model."""
    if _embedder is not None:
        try:
            try:
                return _embedder.get_embedding_dimension()
            except AttributeError:
                return _embedder.get_sentence_embedding_dimension()
        except Exception:
            pass
    try:
        scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import archon_config
        return archon_config.embedding_dim()
    except Exception:
        return int(os.environ.get("ARCHON_EMBEDDING_DIM", "512"))


def _get_embedder():
    """Lazy-load the configured Chinese sentence-transformer model."""
    global _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedder = SentenceTransformer(_get_embedding_model())
        except Exception:
            _embedder = None
    return _embedder

def _embed_text(text: str):
    """Embed text to a float32 list. Returns None if embedder is unavailable."""
    emb = _get_embedder()
    if emb is None:
        return None
    try:
        vec = emb.encode(text, normalize_embeddings=True)
        return vec.tolist()
    except Exception:
        return None

# ---- department name sanitization for LanceDB table names ----
_dept_name_cache = {}
_dept_name_reverse = {}

def _sanitize_dept_name(department: str) -> str:
    """Convert department name to a safe table name suffix."""
    if department in _dept_name_cache:
        return _dept_name_cache[department]
    if re.match(r"^[a-zA-Z0-9_-]+$", department):
        safe = department
    else:
        safe = "dept_" + hashlib.md5(department.encode("utf-8")).hexdigest()[:12]
    _dept_name_cache[department] = safe
    _dept_name_reverse[safe] = department
    return safe

def _dept_from_table_name(tn: str) -> str:
    """从表名反向解析部门名。未知suffix返回原始suffix,调用方应校验是否为有效部门名。"""
    suffix = tn[5:]  # 去掉 "docs_" 前缀
    dept = _dept_name_reverse.get(suffix, None)
    if dept is None:
        # 尝试从缓存读回（可能就是已知部门名但没进反向表）
        for k, v in _dept_name_cache.items():
            if v == suffix:
                _dept_name_reverse[suffix] = k
                return k
        # 回退：返回原始suffix
        return suffix
    return dept

# ---- config ----

def configure(knowledge_rag_dir: str = None):
    global _KNOWLEDGE_RAG_DIR, _DEFAULT_DOCUMENTS_DIR, _active_documents_dir, _LANCEDB_DIR, _db
    if knowledge_rag_dir:
        _KNOWLEDGE_RAG_DIR = knowledge_rag_dir
        _DEFAULT_DOCUMENTS_DIR = os.path.join(_KNOWLEDGE_RAG_DIR, "documents")
        _active_documents_dir = _DEFAULT_DOCUMENTS_DIR
        os.environ["KNOWLEDGE_RAG_DIR"] = _KNOWLEDGE_RAG_DIR
        # CRITICAL: also update LanceDB path
        _LANCEDB_DIR = os.path.join(_KNOWLEDGE_RAG_DIR, "data", "lancedb")
        _db = None  # Force reconnection on next use
    return {"knowledge_rag_dir": _KNOWLEDGE_RAG_DIR, "lancedb_dir": _LANCEDB_DIR}

def set_documents_dir(encrypted_dir: str = None):
    global _active_documents_dir
    if encrypted_dir:
        _active_documents_dir = encrypted_dir
    else:
        _active_documents_dir = _DEFAULT_DOCUMENTS_DIR

# ---- LanceDB connection (lazy) ----

_db = None

def _get_db():
    global _db
    if _db is None:
        import lancedb
        os.makedirs(_LANCEDB_DIR, exist_ok=True)
        _db = lancedb.connect(_LANCEDB_DIR)
    _network_mode = os.environ.get("LANCEDB_NETWORK_MODE", "")
    if _network_mode == "ro":
        import warnings
        warnings.filterwarnings("ignore", category=UserWarning)
    return _db


def _table_vector_dim(table) -> int | None:
    """Return the vector column dimension for a LanceDB table, if present."""
    try:
        field = table.schema.field("vector")
        dim = getattr(field.type, "list_size", None)
        return int(dim) if dim else None
    except Exception:
        return None


def _get_table(department: str):
    """Get department table (auto-create with vector column)."""
    db = _get_db()
    table_name = f"docs_{_sanitize_dept_name(department)}"
    try:
        table = db.open_table(table_name)
        # Migration: add vector column to old tables
        existing_schema = table.schema
        col_names = [f.name for f in existing_schema]
        if "vector" not in col_names:
            try:
                import pyarrow as pa
                dim = _embedding_dim()
                table.add_columns({"vector": pa.nulls(dim, type=pa.list_(pa.float32(), dim))})
            except Exception:
                pass  # Migration may not be supported, vector search will skip
        return table
    except Exception:
        if os.environ.get("LANCEDB_NETWORK_MODE") == "ro":
            return None
        import pyarrow as pa
        dim = _embedding_dim()
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
            pa.field("filename", pa.string()),
            pa.field("department", pa.string()),
            pa.field("summary", pa.string()),
            pa.field("tags", pa.string()),
            pa.field("entities", pa.string()),
            pa.field("key_data", pa.string()),
        ])
        return db.create_table(table_name, schema=schema)

# ---- encryption helpers ----

_dept_passwords = {}
_RAG_ENC_SALT = b"kr_rag_salt_v1__"
_CIPHERTEXT_RUN_RE = re.compile(r"(?:[A-Za-z0-9]|[<>=;|{}\[\]~`^\\/:@#$%&*+_,.\-]){96,}")
_FERNET_TOKEN_RE = re.compile(r"\bgAAAAA[A-Za-z0-9_-]{80,}\b")

# ---- FTS index helper ----

_fts_indexed_tables = set()

def _ensure_fts_index(table, table_name: str):
    if table_name in _fts_indexed_tables:
        return True
    if os.environ.get("LANCEDB_NETWORK_MODE") == "ro":
        return False
    try:
        table.create_fts_index("text", replace=False)
        _fts_indexed_tables.add(table_name)
        return True
    except Exception:
        try:
            table.create_fts_index("text", replace=True)
            _fts_indexed_tables.add(table_name)
            return True
        except Exception:
            return False

def set_dept_password(department: str, password: str):
    _dept_passwords[department] = password

def _get_dept_password(department: str) -> str:
    return _dept_passwords.get(department, "")

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

def _derive_dept_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.backends import default_backend
    from base64 import urlsafe_b64encode
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600000, backend=default_backend())
    return urlsafe_b64encode(kdf.derive(password.encode("utf-8")))

def _encrypt_chunk(content: str, department: str) -> bytes:
    from cryptography.fernet import Fernet
    pwd = _get_dept_password(department)
    if not pwd:
        return content.encode("utf-8")
    key = _derive_dept_key(pwd, _RAG_ENC_SALT)
    f = Fernet(key)
    return _RAG_ENC_SALT + f.encrypt(content.encode("utf-8"))

def _decrypt_chunk(encrypted: bytes, department: str) -> str:
    from cryptography.fernet import Fernet
    if not encrypted:
        return ""
    is_marked_encrypted = encrypted.startswith(_RAG_ENC_SALT)
    pwd = _get_dept_password(department)
    if not is_marked_encrypted:
        try:
            return _sanitize_visible_text(encrypted.decode("utf-8"))
        except Exception:
            return ""
    if not pwd:
        return ""
    salt = encrypted[:16]
    ciphertext = encrypted[16:]
    try:
        key = _derive_dept_key(pwd, salt)
        return _sanitize_visible_text(Fernet(key).decrypt(ciphertext).decode("utf-8"))
    except Exception:
        return ""

# ---- RRF (Reciprocal Rank Fusion) ----

def _rrf_fuse(bm25_results, vector_results, k=60):
    """RRF fusion of two ranked lists. Returns merged list sorted by RRF score descending."""
    scores = {}
    for rank, (rid, _score) in enumerate(bm25_results):
        scores[rid] = scores.get(rid, 0) + 1.0 / (k + rank + 1)
    for rank, (rid, _score) in enumerate(vector_results):
        scores[rid] = scores.get(rid, 0) + 1.0 / (k + rank + 1)
    sorted_items = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_items

# ---- public API ----

def kr_add_document(
    content: str,
    filename: str,
    department: str,
    summary: str = "",
    tags: list = None,
    entities: list = None,
    key_data: dict = None,
) -> dict:
    """Index document into LanceDB (department-isolated). Now also embeds and stores vector."""
    content = _sanitize_visible_text(content)
    if not content:
        return {"status": "skipped", "message": "content empty or suspicious", "filename": filename}
    summary = _sanitize_visible_text(summary)
    table = _get_table(department)
    doc_id = str(uuid.uuid4())
    vector = _embed_text(content)
    dim = _embedding_dim()
    if not vector or len(vector) != dim:
        # Embedding unavailable or wrong dimension - use zero vector
        vector = [0.0] * dim
    row = {
        "id": doc_id,
        "text": content,
        "vector": vector,
        "filename": filename,
        "department": department,
        "summary": summary or "",
        "tags": json.dumps(tags or [], ensure_ascii=False),
        "entities": json.dumps(entities or [], ensure_ascii=False),
        "key_data": json.dumps(key_data or {}, ensure_ascii=False),
    }
    table.add([row])
    return {
        "status": "success",
        "doc_id": doc_id,
        "department": department,
        "filename": filename,
    }

def _score_record(r, rtext, method):
    score = 0.0
    sid = r.get("id", "")
    try:
        if method == "fts":
            score = float(r.get("_score", 0.0))
            score = 1.0 / (1.0 + score)  # Normalize BM25 distance to 0-1
            score = min(score, 1.0)
        elif method == "like":
            score = 0.3
    except Exception:
        score = 0.1
    return score

def _dedup_and_sort(all_results, max_results):
    output = []
    for rid, (r, score, method) in sorted(all_results.items(), key=lambda x: x[1][1], reverse=True):
        try:
            tags = json.loads(r.get("tags", "[]"))
        except Exception:
            tags = []
        try:
            key_data = json.loads(r.get("key_data", "{}"))
        except Exception:
            key_data = {}
        chunk_text = _sanitize_visible_text(r.get("text", "") or "")
        if not chunk_text:
            continue
        summary = _sanitize_visible_text(r.get("summary", "") or chunk_text[:200].replace("#", "").strip())[:200]
        output.append({
            "record_id": rid,
            "filename": _sanitize_visible_text(r.get("filename", "")),
            "department": r.get("department", ""),
            "summary": summary,
            "content": chunk_text,
            "tags": tags,
            "key_metrics": key_data,
            "score": score,
            "search_method": method,
        })
    output.sort(key=lambda x: x["score"], reverse=True)
    return output[:max_results]

def kr_search(
    query: str,
    department: str = None,
    filename: str = None,
    max_results: int = 5,
    mode: str = "hybrid",
    min_score: float = 0.0,
    **kwargs,
) -> dict:
    """
    Multi-mode search: bm25 | vector | hybrid | bm25_full

    - bm25: BM25 FTS only
    - vector: Semantic vector search only
    - hybrid: BM25 + Vector RRF fusion (default)
    - bm25_full: BM25 full recall, no limit (self-doubt round)
    """
    if mode == "hybrid":
        return kr_hybrid_search(query, department=department, filename=filename, max_results=max_results)
    if mode == "vector":
        return kr_vector_search(query, department=department, filename=filename, max_results=max_results)
    if mode == "bm25_full":
        return kr_self_doubt_search(query, department=department, filename=filename, min_score=min_score)
    return _kr_bm25_search(query, department=department, filename=filename, max_results=max_results, min_score=min_score)


def _tokenize_text(text: str) -> list:
    """Tokenize Chinese/English text with jieba, falling back to regex."""
    text = str(text or "")
    try:
        import jieba
        tokens = [t.strip() for t in jieba.lcut(text) if t.strip()]
    except Exception:
        tokens = re.findall(r"[A-Za-z0-9.%+~<>=-]+|[\u4e00-\u9fff]+", text)
    skip = set("，。！？；：、（）《》【】\t\n ")
    return [t for t in tokens if t not in skip]


def _local_bm25_search(table, query: str, filename_cond: str = None,
                       max_results: int = 50, min_score: float = 0.0):
    """Pure-Python BM25 over LanceDB rows for robust Chinese keyword search."""
    try:
        frame = table.to_pandas()
    except Exception:
        return None
    docs = []
    for _, row in frame.iterrows():
        record = dict(row)
        if filename_cond:
            pattern = filename_cond.replace("%", ".*")
            if not re.search(pattern, str(record.get("filename", "")), re.IGNORECASE):
                continue
        docs.append(record)

    if not docs:
        return []

    tokenized_docs = [_tokenize_text(doc.get("text", "")) for doc in docs]
    avgdl = max(sum(len(tokens) for tokens in tokenized_docs) / len(docs), 1.0)
    doc_freq = Counter(t for tokens in tokenized_docs for t in set(tokens))
    query_terms = _tokenize_text(query)
    if not query_terms:
        query_terms = [query]

    scored = []
    for doc, tokens in zip(docs, tokenized_docs):
        tf = Counter(tokens)
        dl = max(len(tokens), 1)
        score = 0.0
        for term in query_terms:
            term_tf = tf.get(term, 0)
            if term_tf == 0:
                continue
            n = doc_freq.get(term, 0)
            idf = math.log(1.0 + (len(docs) - n + 0.5) / (n + 0.5))
            score += idf * (term_tf * 2.5) / (term_tf + 1.5 * (0.25 + 0.75 * dl / avgdl))
        if score > 0 and score >= min_score:
            scored.append((doc, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:max_results]


def _kr_bm25_search(query, department=None, filename=None, max_results=5, min_score=0.0):
    """Chinese-aware BM25 search with FTS/LIKE fallback."""
    if department:
        try:
            table = _get_table(department)
        except Exception:
            return {"status": "error", "message": f"Department [{department}] index not found"}
        if table is None:
            return {"status": "success", "result_count": 0, "results": [],
                    "message": f"Department [{department}] index not available (network read-only mode)"}
        tables = [(department, table)]
    else:
        tables = []
        db = _get_db()
        for tn in db.table_names():
            if tn.startswith("docs_"):
                dept = _dept_from_table_name(tn)
                try:
                    tables.append((dept, db.open_table(tn)))
                except Exception:
                    pass
        if not tables:
            return {"status": "success", "result_count": 0, "results": []}

    filename_cond = None
    if filename:
        if "*" in filename:
            filename_cond = filename.replace("*", "%")
        else:
            filename_cond = f"%{filename}%"

    all_results = {}
    keywords = _tokenize_text(query)
    if not keywords:
        keywords = [query]

    for dept, table in tables:
        local_results = _local_bm25_search(
            table,
            query,
            filename_cond=filename_cond,
            max_results=max_results * 3,
            min_score=min_score,
        )
        if local_results is not None:
            for record, score in local_results:
                rid = record.get("id", "")
                if rid and rid not in all_results:
                    all_results[rid] = (record, min(score, 10.0), "bm25_local")
            continue

        table_name = f"docs_{_sanitize_dept_name(dept)}"
        has_fts = _ensure_fts_index(table, table_name)

        fts_results = []
        if has_fts:
            try:
                fts_raw = table.search(query, query_type="fts").limit(max_results * 3).to_list()
                fts_results = fts_raw
            except Exception:
                pass

        like_results = []
        kw = keywords[0]
        try:
            like_results = table.search().where(f"text LIKE '%{kw}%'").limit(max_results * 2).to_list()
        except Exception:
            pass

        for r in fts_results:
            rid = r.get("id", "")
            if rid and rid not in all_results:
                rtext = r.get("text", "") or ""
                if filename_cond:
                    fname = r.get("filename", "")
                    if filename:
                        pattern = filename.replace("*", ".*")
                        if not re.search(pattern, fname):
                            continue
                score = _score_record(r, rtext, "fts")
                if score >= min_score:
                    all_results[rid] = (r, score, "fts")

        for r in like_results:
            rid = r.get("id", "")
            if rid and rid not in all_results:
                rtext = r.get("text", "") or ""
                score = _score_record(r, rtext, "like")
                if score >= min_score:
                    all_results[rid] = (r, score, "like")

    output = _dedup_and_sort(all_results, max_results)
    return {
        "status": "success",
        "result_count": len(output),
        "results": output,
    }

def kr_vector_search(
    query: str,
    department: str = None,
    filename: str = None,
    max_results: int = 10,
    min_score: float = 0.0,
) -> dict:
    """Semantic vector search using the configured sentence-transformer model."""
    query_vec = _embed_text(query)
    if query_vec is None:
        return {"status": "error", "message": "Embedding model not available (install sentence-transformers)"}

    if department:
        try:
            table = _get_table(department)
        except Exception:
            return {"status": "error", "message": f"Department [{department}] index not found"}
        if table is None:
            return {"status": "success", "result_count": 0, "results": [],
                    "message": "Department index not available (network read-only mode)"}
        tables = [(department, table)]
    else:
        tables = []
        db = _get_db()
        for tn in db.table_names():
            if tn.startswith("docs_"):
                dept = _dept_from_table_name(tn)
                try:
                    tables.append((dept, db.open_table(tn)))
                except Exception:
                    pass
        if not tables:
            return {"status": "success", "result_count": 0, "results": []}

    all_results = []
    import numpy as np
    for dept, table in tables:
        table_dim = _table_vector_dim(table)
        if table_dim and table_dim != _embedding_dim():
            return {
                "status": "error",
                "message": f"Embedding dimension mismatch: index={table_dim}, model={_embedding_dim()}. Run kr_reindex(full_rebuild=True) after changing ARCHON_EMBEDDING_MODEL.",
                "result_count": 0,
                "results": [],
            }
        try:
            # Ensure table has vector column; skip if schema is old
            vec_results = table.search(query_vec, vector_column_name="vector").limit(max_results * 2).to_list()
        except Exception:
            continue

        for r in vec_results:
            rtext = r.get("text", "") or ""
            if filename:
                fname = r.get("filename", "")
                pattern = filename.replace("*", ".*")
                if not re.search(pattern, fname):
                    continue
            dist = float(r.get("_distance", 1.0))
            score = max(0.0, 1.0 - dist)
            if score >= min_score:
                all_results.append((r, score, "vector", dept))

    all_results.sort(key=lambda x: x[1], reverse=True)
    output = []
    for r, score, method, dept in all_results[:max_results]:
        try:
            tags = json.loads(r.get("tags", "[]"))
        except Exception:
            tags = []
        try:
            key_data = json.loads(r.get("key_data", "{}"))
        except Exception:
            key_data = {}
        chunk_text = _sanitize_visible_text(r.get("text", "") or "")
        if not chunk_text:
            continue
        summary = _sanitize_visible_text(r.get("summary", "") or chunk_text[:200].replace("#", "").strip())[:200]
        output.append({
            "record_id": r.get("id", ""),
            "filename": _sanitize_visible_text(r.get("filename", "")),
            "department": dept,
            "summary": summary,
            "content": chunk_text,
            "tags": tags,
            "key_metrics": key_data,
            "score": round(score, 4),
            "search_method": method,
        })

    return {
        "status": "success",
        "result_count": len(output),
        "results": output,
    }

def kr_hybrid_search(
    query: str,
    department: str = None,
    filename: str = None,
    max_results: int = 10,
) -> dict:
    """
    BM25 + Vector RRF (k=60) hybrid search.
    Fuses two ranked lists via Reciprocal Rank Fusion, returns merged Top-N.
    """
    # Run both searches in parallel
    bm25_out = _kr_bm25_search(query, department=department, filename=filename, max_results=max_results * 2)
    vec_out = kr_vector_search(query, department=department, filename=filename, max_results=max_results * 2)

    bm25_ranked = [(r["record_id"], r["score"]) for r in bm25_out.get("results", []) if r.get("record_id")]
    vec_ranked = [(r["record_id"], r["score"]) for r in vec_out.get("results", []) if r.get("record_id")]

    rrf_scores = _rrf_fuse(bm25_ranked, vec_ranked, k=60)

    # Build lookup for record details
    lookup = {}
    for r in bm25_out.get("results", []) + vec_out.get("results", []):
        rid = r.get("record_id", "")
        if rid and rid not in lookup:
            lookup[rid] = r

    output = []
    for rid, rrf_score in rrf_scores[:max_results]:
        if rid in lookup:
            rec = dict(lookup[rid])
            rec["score"] = round(rrf_score, 4)
            rec["search_method"] = "hybrid_rrf"
            output.append(rec)

    return {
        "status": "success",
        "result_count": len(output),
        "results": output,
        "fusion": "RRF(k=60)",
    }

def kr_self_doubt_search(
    query: str,
    department: str = None,
    filename: str = None,
    min_score: float = 0.1,
) -> dict:
    """
    BM25 full recall mode (self-doubt round).
    Returns ALL chunks with score >= min_score, no limit.
    Used to catch all mentions of a keyword across the entire corpus.
    """
    return _kr_bm25_search(
        query, department=department, filename=filename,
        max_results=1000, min_score=min_score
    )

def kr_rerank(query: str, candidates: list, top_n: int = 10) -> dict:
    """
    Cross-encoder rerank for Top-N precision (optional).
    Requires sentence-transformers with cross-encoder support.
    Returns reranked candidates.
    """
    if not candidates:
        return {"status": "success", "result_count": 0, "results": []}

    try:
        from sentence_transformers import CrossEncoder
        model = CrossEncoder(os.environ.get("ARCHON_RERANK_MODEL", "BAAI/bge-reranker-base"))
        pairs = [(query, c.get("content", c.get("text", ""))[:1000]) for c in candidates]
        scores = model.predict(pairs)
        scored = list(zip(candidates, scores))
        scored.sort(key=lambda x: x[1], reverse=True)
        output = []
        for c, s in scored[:top_n]:
            c["rerank_score"] = float(s)
            c["score"] = float(s)
            output.append(c)
        return {
            "status": "success",
            "result_count": len(output),
            "results": output,
            "reranker": os.environ.get("ARCHON_RERANK_MODEL", "BAAI/bge-reranker-base"),
        }
    except Exception as e:
        return {"status": "fallback", "message": f"Cross-encoder not available: {e}", "results": candidates}

def kr_get_index_stats() -> dict:
    db = _get_db()
    tables = {}
    for tn in db.table_names():
        if tn.startswith("docs_"):
            dept = _dept_from_table_name(tn)
            try:
                t = db.open_table(tn)
                tables[dept] = t.count_rows()
            except Exception:
                tables[dept] = 0
    return {"status": "success", "tables": tables, "total_docs": sum(tables.values())}

def kr_decrypt_chunk(record_id: str, department: str, password: str) -> dict:
    try:
        table = _get_table(department)
        if table is None:
            return {"status": "error", "message": "Department index not available (network read-only mode)"}
        results = table.search().where(f"id = '{record_id}'").limit(1).to_list()
        if not results:
            return {"status": "error", "message": "Record not found"}
        record = results[0]
    except Exception:
        return {"status": "error", "message": "Search failed"}

    encrypted_dir = os.environ.get("ENCRYPTED_DIR", "")
    if not encrypted_dir:
        return {"status": "error", "message": "ENCRYPTED_DIR not configured"}

    store_path = os.path.join(encrypted_dir, department, f"store_{department}.enc")
    if not os.path.exists(store_path):
        return {"status": "error", "message": "Encrypted store not found"}

    try:
        import sys as _sys
        secure_path = Path(__file__).resolve().parents[2] / "secure-storage" / "scripts"
        if str(secure_path) not in _sys.path:
            _sys.path.insert(0, str(secure_path))
        from secure_store import pw_decrypt_store
        records = pw_decrypt_store(
            password,
            department=department,
            encrypted_dir=encrypted_dir,
        )
        found = next((r for r in records if r.get("id") == record_id), None)
        content = _sanitize_visible_text((found or {}).get("full_text", ""))
        if content:
            return {
                "status": "success",
                "record_id": record_id,
                "filename": (found or record).get("filename", ""),
                "summary": (found or record).get("summary", ""),
                "content": content[:2000],
                "full_length": len(content),
            }
        return {"status": "error", "message": "Decrypt returned empty"}
    except Exception as e:
        return {"status": "error", "message": f"Decrypt failed: {e}"}

def kr_reindex(force: bool = False, full_rebuild: bool = False) -> dict:
    if full_rebuild:
        db = _get_db()
        for tn in db.table_names():
            if tn.startswith("docs_"):
                try:
                    db.drop_table(tn)
                except Exception:
                    pass
        return {"status": "success", "message": "All indexes cleared"}
    return {"status": "success", "message": "No rebuild needed (LanceDB auto-maintains indexes)"}

def _get_orchestrator():
    import sys as _sys
    mod = _sys.modules[__name__]
    mod.add_document_from_content = lambda content, path, dept: kr_add_document(
        content=content, filename=Path(path).name, department=dept
    )
    return mod
