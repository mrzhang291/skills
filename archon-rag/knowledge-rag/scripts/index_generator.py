"""
Document Index Generator - Two-Tier RAG Layer 1
Generates structured section-level index from parsed documents.
Stored in LanceDB as index_<department> table alongside chunks.
"""
import json
import os
import uuid
from datetime import datetime
from datetime import datetime
import re
from typing import List, Dict, Optional

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


def _escape_like(value: str) -> str:
    return str(value).replace("'", "''")

def generate_document_index(
    content: str,
    filename: str,
    department: str,
    chunk_ids: List[str] = None,
) -> Dict:
    """
    Parse document content and extract section structure.
    
    Returns:
        {
            "doc_id": str,
            "filename": str,
            "department": str,
            "sections": [
                {
                    "section_id": str,
                    "title": str,
                    "level": int (1-4),
                    "summary": str (AI-generated, 100 chars),
                    "chunk_ids": [str],
                    "key_metrics": {"throughput": 123, ...}
                }
            ]
        }
    """
    sections = []
    lines = content.split("\n")
    current_section = None
    
    # Pattern: ## 标题 or # 标题 or ### 标题
    heading_pattern = re.compile(r'^(#{1,4})\s+(.+)$')
    
    for i, line in enumerate(lines):
        m = heading_pattern.match(line.strip())
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            
            # Skip TOC-like entries
            if re.match(r'^[\d.]+$', title) or len(title) < 2:
                continue
                
            # Collect content until next heading
            body_lines = []
            for j in range(i+1, min(i+50, len(lines))):
                if heading_pattern.match(lines[j].strip()):
                    break
                if lines[j].strip():
                    body_lines.append(lines[j].strip())
            
            summary = " ".join(body_lines)[:200] if body_lines else title
            
            # Extract key metrics from section body
            metrics = {}
            body_text = " ".join(body_lines)
            for match in re.finditer(r'([A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff/_\-]{1,20})\s*[：:]\s*([\d,.]+\s*(?:[A-Za-z%μµ°/0-9m³²⁻¹/·]+)?)', body_text):
                key = match.group(1)
                val = match.group(2).strip()
                if key not in metrics:
                    metrics[key] = val
            
            sections.append({
                "section_id": str(uuid.uuid4())[:8],
                "title": title,
                "level": level,
                "summary": summary[:200],
                "chunk_ids": chunk_ids or [],
                "key_metrics": metrics,
            })
    
    return {
        "doc_id": str(uuid.uuid4())[:8],
        "filename": filename,
        "department": department,
        "section_count": len(sections),
        "sections": sections,
    }


def store_index(index_data: Dict, department: str) -> Dict:
    """Store document index in LanceDB index_<dept> table."""
    import sys
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    _skill_dir = os.path.dirname(_self_dir)
    if _skill_dir not in sys.path:
        sys.path.insert(0, _skill_dir)
    
    from knowledge_rag_wrapper import _get_db, _sanitize_dept_name
    
    db = _get_db()
    table_name = f"index_{_sanitize_dept_name(department)}"
    
    # Create or open table
    try:
        table = db.open_table(table_name)
    except Exception:
        import lancedb
        import pyarrow as pa
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("doc_id", pa.string()),
            pa.field("filename", pa.string()),
            pa.field("department", pa.string()),
            pa.field("section_id", pa.string()),
            pa.field("section_title", pa.string()),
            pa.field("section_level", pa.int32()),
            pa.field("summary", pa.string()),
            pa.field("chunk_ids", pa.string()),  # JSON array
            pa.field("key_metrics", pa.string()),  # JSON object
        ])
        table = db.create_table(table_name, schema=schema)
    
    # Insert rows
    rows = []
    for sec in index_data["sections"]:
        rows.append({
            "id": str(uuid.uuid4()),
            "doc_id": index_data["doc_id"],
            "filename": index_data["filename"],
            "department": index_data["department"],
            "section_id": sec["section_id"],
            "section_title": sec["title"],
            "section_level": sec["level"],
            "summary": sec["summary"],
            "chunk_ids": json.dumps(sec.get("chunk_ids", []), ensure_ascii=False),
            "key_metrics": json.dumps(sec.get("key_metrics", {}), ensure_ascii=False),
        })
    
    if rows:
        table.add(rows)
    
    return {
        "status": "success",
        "doc_id": index_data["doc_id"],
        "section_count": len(rows),
        "table": table_name,
    }


def search_index(
    query: str,
    department: str,
    max_results: int = 10,
    min_score: float = 0.0,
) -> Dict:
    """
    Search document index (Layer 1 of two-tier RAG).
    Returns matching sections with summaries.
    """
    import sys
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    _skill_dir = os.path.dirname(_self_dir)
    if _skill_dir not in sys.path:
        sys.path.insert(0, _skill_dir)
    
    from knowledge_rag_wrapper import _get_db, _sanitize_dept_name
    
    db = _get_db()
    table_name = f"index_{_sanitize_dept_name(department)}"
    
    try:
        table = db.open_table(table_name)
    except Exception:
        return {"status": "empty", "results": [], "message": "No index found. Upload documents first."}
    
    # BM25 FTS on section_title + summary
    try:
        results = table.search(query).limit(max_results).to_list()
    except Exception:
        # Fallback: LIKE on title
        try:
            results = table.search().where(
                f"section_title LIKE '%{query}%' OR summary LIKE '%{query}%'"
            ).limit(max_results).to_list()
        except Exception:
            results = []
    
    output = []
    for r in results:
        try:
            metrics = json.loads(r.get("key_metrics", "{}"))
        except Exception:
            metrics = {}
        
        output.append({
            "section_id": r.get("section_id", ""),
            "doc_filename": r.get("filename", ""),
            "section_title": r.get("section_title", ""),
            "section_level": r.get("section_level", 0),
            "summary": r.get("summary", ""),
            "key_metrics": metrics,
            "score": r.get("_distance", 1.0),
        })
    
    return {
        "status": "success",
        "result_count": len(output),
        "results": output[:max_results],
    }


def drill_section(
    section_id: str,
    department: str,
) -> Dict:
    """
    Drill into a specific section (Layer 2 of two-tier RAG).
    Retrieves full chunk content for the section.
    """
    import sys
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    _skill_dir = os.path.dirname(_self_dir)
    if _skill_dir not in sys.path:
        sys.path.insert(0, _skill_dir)
    
    from knowledge_rag_wrapper import _get_db, _sanitize_dept_name
    
    db = _get_db()
    index_table_name = f"index_{_sanitize_dept_name(department)}"
    
    try:
        index_table = db.open_table(index_table_name)
        # Get the section record
        section_rows = index_table.search().where(
            f"section_id = '{section_id}'"
        ).limit(1).to_list()
        
        if not section_rows:
            return {"status": "error", "message": f"Section {section_id} not found"}
        
        section = section_rows[0]
        chunk_ids = json.loads(section.get("chunk_ids", "[]"))
        
        # Fetch chunks
        chunks_table_name = f"docs_{_sanitize_dept_name(department)}"
        chunks_table = db.open_table(chunks_table_name)
        
        chunk_contents = []
        if chunk_ids:
            for cid in chunk_ids:
                try:
                    rows = chunks_table.search().where(f"id = '{cid}'").limit(1).to_list()
                    if rows:
                        chunk_contents.append({
                            "chunk_id": cid,
                            "text": rows[0].get("text", ""),
                            "filename": rows[0].get("filename", ""),
                        })
                except Exception:
                    pass
        
        return {
            "status": "success",
            "section_id": section_id,
            "section_title": section.get("section_title", ""),
            "summary": section.get("summary", ""),
            "chunk_count": len(chunk_contents),
            "chunks": chunk_contents,
            "key_metrics": json.loads(section.get("key_metrics", "{}")),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════
# Document Metadata Index (7-field - Architecture Layer 1)
# ═══════════════════════════════════════════════════════════════

def store_doc_meta(doc_meta: dict, department: str) -> dict:
    """
    Store 7-field document metadata in LanceDB index_<dept> table.
    
    doc_meta fields:
      - client_name:       甲方企业名称
      - project_name:      项目名称
      - product_capacity:  产品类型+产能
      - quality_summary: 关键质量/规格指标
      - objective:        主要目标
      - process:          方法/流程
      - conclusion_chunk_id: 结论chunk指针 (AI-only)
      - filename:          源文件名
    """
    import sys
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    _skill_dir = os.path.dirname(_self_dir)
    if _skill_dir not in sys.path:
        sys.path.insert(0, _skill_dir)
    
    from knowledge_rag_wrapper import _get_db, _sanitize_dept_name
    
    db = _get_db()
    table_name = f"docmeta_{_sanitize_dept_name(department)}"
    
    try:
        table = db.open_table(table_name)
    except Exception:
        import lancedb
        import pyarrow as pa
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("doc_id", pa.string()),
            pa.field("filename", pa.string()),
            pa.field("department", pa.string()),
            pa.field("client_name", pa.string()),
            pa.field("project_name", pa.string()),
            pa.field("product_capacity", pa.string()),
            pa.field("quality_summary", pa.string()),
            pa.field("objective", pa.string()),
            pa.field("process", pa.string()),
            pa.field("conclusion_chunk_id", pa.string()),
            pa.field("upload_time", pa.string()),
        ])
        table = db.create_table(table_name, schema=schema)
    
    row = {
        "id": str(uuid.uuid4()),
        "doc_id": doc_meta.get("doc_id", str(uuid.uuid4())[:8]),
        "filename": _sanitize_visible_text(doc_meta.get("filename", "")),
        "department": department,
        "client_name": _sanitize_visible_text(doc_meta.get("client_name", "")),
        "project_name": _sanitize_visible_text(doc_meta.get("project_name", "")),
        "product_capacity": _sanitize_visible_text(doc_meta.get("product_capacity", "")),
        "quality_summary": _sanitize_visible_text(doc_meta.get("quality_summary", "")),
        "objective": _sanitize_visible_text(doc_meta.get("objective", "")),
        "process": _sanitize_visible_text(doc_meta.get("process", "")),
        "conclusion_chunk_id": doc_meta.get("conclusion_chunk_id", ""),
        "upload_time": doc_meta.get("upload_time", datetime.now().isoformat()),
    }
    
    table.add([row])
    
    # Ensure FTS index for search
    fts_status = "ok"
    try:
        table.create_fts_index(["client_name", "project_name", "product_capacity",
                                "quality_summary", "objective", "process"],
                               replace=False)
    except Exception as e:
        try:
            table.create_fts_index(["client_name", "project_name", "product_capacity",
                                    "quality_summary", "objective", "process"],
                                   replace=True)
            fts_status = "replaced"
        except Exception as e2:
            fts_status = f"fts_failed: {e2}"
    
    return {
        "status": "success",
        "doc_id": row["doc_id"],
        "table": table_name,
        "fts_status": fts_status,
    }


def search_doc_meta(query: str, department: str, max_results: int = 20) -> dict:
    """
    Search 7-field document metadata (Layer 1: structured index).
    
    Searches across all 6 visible fields simultaneously.
    Returns metadata list — employees see everything except conclusion_chunk_id.
    """
    import sys
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    _skill_dir = os.path.dirname(_self_dir)
    if _skill_dir not in sys.path:
        sys.path.insert(0, _skill_dir)
    
    from knowledge_rag_wrapper import _get_db, _sanitize_dept_name
    
    db = _get_db()
    table_name = f"docmeta_{_sanitize_dept_name(department)}"
    
    try:
        table = db.open_table(table_name)
    except Exception:
        return {"status": "empty", "results": [], "message": "No document index found. Upload documents first."}
    
    results = []
    seen = set()

    def _add_rows(rows):
        for r in rows:
            formatted = _format_doc_meta(r)
            key = formatted.get("doc_id") or formatted.get("filename")
            if key in seen:
                continue
            seen.add(key)
            results.append(formatted)
    
    # Try BM25 FTS first
    try:
        fts_results = table.search(query, query_type="fts").limit(max_results).to_list()
        _add_rows(fts_results)
    except Exception:
        pass
    
    # Supplement with tokenized LIKE so company-name + intent queries recall all projects.
    try:
        raw_terms = [query] + re.findall(r"[A-Za-z0-9.%+~<>=-]+|[\u4e00-\u9fff]{2,}", query)
        terms = []
        for t in raw_terms:
            t = t.strip()
            if t and t not in terms:
                terms.append(t)
        fields = [
            "client_name", "project_name", "product_capacity",
            "quality_summary", "objective", "process", "filename",
        ]
        for term in terms[:8]:
            safe_term = _escape_like(term)
            like_condition = " OR ".join([f"{field} LIKE '%{safe_term}%'" for field in fields])
            like_results = table.search().where(like_condition).limit(max_results).to_list()
            _add_rows(like_results)
            if len(results) >= max_results:
                break
    except Exception:
        pass

    # Last fallback: old exact whole-query LIKE on all searchable fields.
    if not results:
        try:
            safe_query = _escape_like(query)
            like_condition = " OR ".join([
                f"client_name LIKE '%{safe_query}%'",
                f"project_name LIKE '%{safe_query}%'",
                f"product_capacity LIKE '%{safe_query}%'",
                f"quality_summary LIKE '%{safe_query}%'",
                f"objective LIKE '%{safe_query}%'",
                f"process LIKE '%{safe_query}%'",
            ])
            like_results = table.search().where(like_condition).limit(max_results).to_list()
            _add_rows(like_results)
        except Exception:
            pass
    
    return {
        "status": "success",
        "result_count": len(results[:max_results]),
        "results": results[:max_results],
    }


def _format_doc_meta(row: dict) -> dict:
    """Format a doc_meta row for employee display (hides conclusion_chunk_id)."""
    return {
        "doc_id": row.get("doc_id", ""),
        "filename": _sanitize_visible_text(row.get("filename", "")),
        "client_name": _sanitize_visible_text(row.get("client_name", "")),
        "project_name": _sanitize_visible_text(row.get("project_name", "")),
        "product_capacity": _sanitize_visible_text(row.get("product_capacity", "")),
        "quality_summary": _sanitize_visible_text(row.get("quality_summary", "")),
        "objective": _sanitize_visible_text(row.get("objective", "")),
        "process": _sanitize_visible_text(row.get("process", "")),
        "upload_time": row.get("upload_time", ""),
    }


def drill_conclusion(doc_id: str, department: str) -> dict:
    """
    Drill into conclusion chunk (AI-only, not exposed to employee).
    
    Returns the conclusion_chunk_id and content for AI to read and summarize.
    Employee never sees this raw output — AI writes a summary from it.
    """
    import sys
    _self_dir = os.path.dirname(os.path.abspath(__file__))
    _skill_dir = os.path.dirname(_self_dir)
    if _skill_dir not in sys.path:
        sys.path.insert(0, _skill_dir)
    
    from knowledge_rag_wrapper import _get_db, _sanitize_dept_name
    
    db = _get_db()
    meta_table_name = f"docmeta_{_sanitize_dept_name(department)}"
    
    try:
        meta_table = db.open_table(meta_table_name)
        rows = meta_table.search().where(f"doc_id = '{doc_id}'").limit(1).to_list()
        
        if not rows:
            return {"status": "error", "message": f"Document {doc_id} not found"}
        
        meta = rows[0]
        conclusion_chunk_id = meta.get("conclusion_chunk_id", "")
        
        if not conclusion_chunk_id:
            return {"status": "error", "message": "No conclusion chunk available for this document"}
        
        # Fetch the conclusion chunk from docs table
        chunks_table_name = f"docs_{_sanitize_dept_name(department)}"
        chunks_table = db.open_table(chunks_table_name)
        chunk_rows = chunks_table.search().where(f"id = '{conclusion_chunk_id}'").limit(1).to_list()
        
        if not chunk_rows:
            return {"status": "error", "message": f"Conclusion chunk {conclusion_chunk_id} not found"}
        
        return {
            "status": "success",
            "doc_id": doc_id,
            "filename": meta.get("filename", ""),
            "conclusion_text": chunk_rows[0].get("text", ""),
            "message": "AI: read conclusion_text, write a 100-200 word summary, never expose raw text to employee",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    print("index_generator.py compiled OK")
