"""
??????????pymupdf4llm + pymupdf??
?? IBM Docling???????????
100% ??????????
"""

import os
import re
import sys
from pathlib import Path

_CIPHERTEXT_RUN_RE = re.compile(r"(?:[A-Za-z0-9]|[<>=;|{}\[\]~`^\\/:@#$%&*+_,.\-]){96,}")
_FERNET_TOKEN_RE = re.compile(r"\bgAAAAA[A-Za-z0-9_-]{80,}\b")
_BLOCKED_INPUT_EXTENSIONS = {".enc", ".bin", ".db", ".sqlite", ".lance", ".idx"}
_SUPPORTED_INPUT_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv"}


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
    """Remove encrypted-looking or binary-looking text before it reaches prompts or Wiki."""
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


def _chunks_to_markdown(chunks: list) -> str:
    lines = []
    for ch in chunks:
        heading = str(ch.get("heading", "")).strip()
        content = str(ch.get("content", "")).strip()
        if not heading or not content:
            continue
        level = int(ch.get("level") or 2)
        lines.append(f"{'#' * max(1, min(level, 6))} {heading}")
        lines.append(content)
        lines.append("")
    return "\n".join(lines).strip()


def _parse_pdf(filepath: str) -> dict:
    """? pymupdf4llm ? PDF ? Markdown??? ## ??? chunk"""
    import pymupdf4llm
    md = pymupdf4llm.to_markdown(filepath)
    return _semantic_chunk(md, filepath)


def _parse_docx(filepath: str) -> dict:
    """? python-docx ?? DOCX?????????????????? Heading ?? + outlineLvl ????"""
    try:
        from docx import Document
        from docx.oxml.ns import qn
    except ImportError:
        return _parse_text_fallback(filepath)
    doc = Document(filepath)

    # ?? body ???????????|???? ? XML body ?????
    body = doc.element.body
    para_positions = []   # [(body_pos, para_index)]
    table_positions = []  # [(body_pos, table_index)]

    para_index = 0
    table_index = 0
    for pos, child in enumerate(body):
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "p":
            para_positions.append((pos, para_index))
            para_index += 1
        elif tag == "tbl":
            table_positions.append((pos, table_index))
            table_index += 1

    # ?????
    all_elements = para_positions + table_positions
    all_elements.sort(key=lambda x: x[0])

    # ??????? markdown
    lines_list = []
    for _, (body_pos, idx) in enumerate(all_elements):
        if body_pos in {p[0] for p in para_positions}:
            # ??????
            para_idx = next(p[1] for p in para_positions if p[0] == body_pos)
            if para_idx >= len(doc.paragraphs):
                continue
            para = doc.paragraphs[para_idx]
            text = para.text.strip()
            if not text:
                continue

            # ????????? Heading ????? outlineLvl ????
            heading_level = 0
            if para.style and para.style.name and para.style.name.startswith("Heading"):
                try:
                    heading_level = int(para.style.name.split()[-1])
                except (ValueError, IndexError):
                    heading_level = 1
            else:
                # ?? XML outlineLvl?Word ?????Normal ???????
                try:
                    pPr = para._element.find(qn("w:pPr"))
                    if pPr is not None:
                        ol = pPr.find(qn("w:outlineLvl"))
                        if ol is not None:
                            ol_val = int(ol.get(qn("w:val"), "-1"))
                            if ol_val >= 0:
                                heading_level = ol_val + 1  # outlineLvl 0-based
                except Exception:
                    pass

            if heading_level > 0:
                lines_list.append("#" * min(heading_level, 6) + " " + text)
            else:
                lines_list.append(text)
        else:
            # ??????
            table_idx = next(t[1] for t in table_positions if t[0] == body_pos)
            if table_idx >= len(doc.tables):
                continue
            table = doc.tables[table_idx]
            lines_list.append("")
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                lines_list.append("| " + " | ".join(cells) + " |")
            lines_list.append("")

    md = chr(10).join(lines_list)
    return _semantic_chunk(md, filepath)
def _parse_text_fallback(filepath: str) -> dict:
    """?????"""
    ext = Path(filepath).suffix.lower()
    if ext not in (".txt", ".md", ".csv"):
        return {"status": "error", "message": f"不支持的文本解析类型: {ext or '无扩展名'}"}
    try:
        if ext in (".txt", ".md", ".csv"):
            with open(filepath, "r", encoding="utf-8") as f:
                text = f.read()
    except Exception:
        return {"status": "error", "message": "??????: " + filepath}
    return _semantic_chunk(text, filepath)


def _is_heading_conclusion(text: str) -> bool:
    """???????????"""
    text_lower = text.lower()
    for k in ("结论", "建议", "总结", "小结", "conclusion", "recommendation"):
        if k in text_lower:
            return True
    return False


# CHAPTER FILTER: loaded from config/profiles/*.json
_CHAPTER_BLACKLIST_MARKER = "__BLACKLISTED_CHAPTER__"
_CONCLUSION_CANONICALS = {"conclusion", "recommendations", "结论", "建议"}
_chapter_rules_cache = None


def _load_chapter_rules() -> dict:
    """Load chapter rules from the shared Archon profile config."""
    global _chapter_rules_cache
    if _chapter_rules_cache is not None:
        return _chapter_rules_cache
    try:
        scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import archon_config
        _chapter_rules_cache = archon_config.chapter_rules()
    except Exception:
        _chapter_rules_cache = {}
    return _chapter_rules_cache


def _match_chapter(heading: str) -> str | None:
    """Match a heading against the active include/exclude rules."""
    import re as _re
    h_clean = _re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', heading).lower()
    rules = _load_chapter_rules()
    for item in rules.get("exclude", []):
        for alias in item.get("aliases", []):
            alias_clean = _re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', alias).lower()
            if alias_clean and alias_clean in h_clean:
                return _CHAPTER_BLACKLIST_MARKER
    for item in rules.get("include", []):
        canonical = item.get("canonical", "")
        for alias in item.get("aliases", []):
            alias_clean = _re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', alias).lower()
            if alias_clean and alias_clean in h_clean:
                return canonical
    return None
def _semantic_chunk(md: str, filepath: str) -> dict:
    """
    Semantic chunking: 按标题边界 + 段落空行 + min/max 限制分块。
    替代纯标题切分，保留上下文完整性。
    规则：最小200字符/块，最大2000字符/块，结论章节不切分。
    新增章节白名单/黑名单过滤：按 profile 只摘录允许章节。
    """
    import re as _re

    md = _sanitize_visible_text(md)
    if not md:
        return {"status": "error", "message": "文档正文为空或疑似密文/二进制内容"}
    
    chunks = []
    conclusion = ""
    conclusion_parts = []
    inside_conclusion = False
    
    # Step 1: Split by heading boundaries first
    heading_pattern = _re.compile(r'^(#{1,6})\s+(.+)$')
    bold_pattern = _re.compile(r'^\*\*(.+?)\*\*\s*$')
    num_pattern = _re.compile(r'^(\d+(?:\.\d+)*)\s+(.{2,})')
    
    current = {"heading": "", "level": 0, "lines": [], "tables": []}
    prev_matched_chapter = None  # canonical name of the chapter content belongs to
    inside_blacklisted = False   # track when we enter a blacklisted parent section

    for line in md.split('\n'):
        hm = heading_pattern.match(line)
        bm = bold_pattern.match(line)
        nm = num_pattern.match(line)
        is_heading = bool(hm or bm or nm)

        if is_heading:
            if hm:
                heading_text = hm.group(2).strip()
                level = len(hm.group(1))
            elif bm:
                heading_text = bm.group(1).strip()
                level = 2
            else:
                heading_text = nm.group(0).strip()
                depth = nm.group(1).count(".") + 1
                level = min(depth + 1, 6)

            # Match chapter against whitelist/blacklist
            matched_ch = _match_chapter(heading_text)
            is_blacklisted = matched_ch == _CHAPTER_BLACKLIST_MARKER

            # Track blacklisted parent: once inside, sub-headings should NOT re-enter whitelist
            if is_blacklisted and level <= 2:
                inside_blacklisted = True
            elif matched_ch is not None and level <= 2:
                inside_blacklisted = False  # new top-level whitelist chapter exits blacklist

            # Check if entering conclusion
            if is_blacklisted:
                inside_conclusion = False
            elif matched_ch in _CONCLUSION_CANONICALS and level <= 2:
                inside_conclusion = True
            elif inside_conclusion and level > 2:
                pass  # Sub-headings inside conclusion stay
            else:
                inside_conclusion = False

            # Chapter filter: decide if we should collect this section
            should_collect = True
            if is_blacklisted:
                should_collect = False
            elif matched_ch is None:
                if inside_blacklisted:
                    # Sub-heading inside a blacklisted section — keep blocked
                    should_collect = False
                elif prev_matched_chapter is not None or inside_conclusion:
                    # Sub-heading under matched chapter or inside conclusion
                    should_collect = True
                else:
                    # Top-level heading not in whitelist — skip
                    should_collect = False
            else:
                # Whitelist match — only allow if NOT inside a blacklisted section
                if inside_blacklisted:
                    should_collect = False
                else:
                    should_collect = True
            
            # Save previous chunk (if it had content)
            content_text = '\n'.join(current["lines"]).strip()
            table_text = '\n'.join(current["tables"]).strip()
            chunk_content = content_text or table_text
            if chunk_content and current["heading"]:
                if prev_matched_chapter is not None or inside_conclusion:
                    chunks.append({
                        "heading": current["heading"],
                        "level": current["level"],
                        "content": chunk_content,
                        "tables": list(current["tables"]),
                    })
                    if prev_matched_chapter in _CONCLUSION_CANONICALS:
                        conclusion_parts.append(chunk_content)
            
            # Start new chunk
            current = {"heading": heading_text, "level": level, "lines": [], "tables": []}
            if is_blacklisted:
                prev_matched_chapter = None
            elif inside_blacklisted:
                # Sub-heading inside blacklisted parent — stay blocked
                prev_matched_chapter = None
            elif inside_conclusion:
                prev_matched_chapter = "conclusion"
            elif should_collect and matched_ch is not None:
                prev_matched_chapter = matched_ch
            elif should_collect and prev_matched_chapter is not None:
                pass  # Sub-heading under matched chapter, keep prev
            else:
                prev_matched_chapter = None
        else:
            # Only collect content if we are in a whitelisted chapter
            if prev_matched_chapter is not None:
                stripped = line.strip()
                if stripped.startswith("|") and "|" in stripped[1:]:
                    current["tables"].append(stripped)
                else:
                    current["lines"].append(line)
    
    # Save last chunk
    content_text = '\n'.join(current["lines"]).strip()
    table_text = '\n'.join(current["tables"]).strip()
    chunk_content = content_text or table_text
    if chunk_content and current["heading"]:
        if prev_matched_chapter is not None or inside_conclusion:
            chunks.append({
                "heading": current["heading"],
                "level": current["level"],
                "content": chunk_content,
                "tables": list(current["tables"]),
            })
            if prev_matched_chapter in _CONCLUSION_CANONICALS:
                conclusion_parts.append(chunk_content)
    
    # Step 2: Preserve customer-approved chapter boundaries; only split overlong chunks.
    MIN_CHARS = 1
    MAX_CHARS = 2000
    
    refined_chunks = []
    buffer = None
    
    for ch in chunks:
        ch_len = len(ch["content"])
        h_low = ch["heading"].lower()
        is_conclusion_chunk = any(k in h_low for k in ("结论", "建议", "总结", "小结", "conclusion", "recommendation", "summary"))
        
        if is_conclusion_chunk:
            if buffer is not None and len(buffer["content"]) > 0:
                buffer["heading"] = " / ".join(buffer.get("_merged_headings", [buffer["heading"]]))
                refined_chunks.append(buffer)
                buffer = None
            refined_chunks.append(ch)
            continue
        
        if ch_len < MIN_CHARS and buffer is None:
            buffer = dict(ch)
            buffer["content"] = ch["content"]
            buffer["_merged_headings"] = [ch["heading"]]
            continue
        
        if buffer is not None:
            merged_len = len(buffer["content"]) + ch_len
            if merged_len <= MAX_CHARS:
                buffer["content"] += "\n\n" + ch["content"]
                buffer["_merged_headings"].append(ch["heading"])
                if merged_len >= MIN_CHARS:
                    buffer["heading"] = " / ".join(buffer["_merged_headings"])
                    refined_chunks.append(buffer)
                    buffer = None
                continue
            else:
                if len(buffer["content"]) >= MIN_CHARS:
                    buffer["heading"] = " / ".join(buffer["_merged_headings"])
                    refined_chunks.append(buffer)
                buffer = None
        
        if ch_len > MAX_CHARS:
            paragraphs = ch["content"].split('\n\n')
            sub = {"heading": ch["heading"], "level": ch["level"], "lines": [], "tables": ch["tables"]}
            sub_len = 0
            for para in paragraphs:
                para_len = len(para)
                if sub_len + para_len > MAX_CHARS and sub_len >= MIN_CHARS:
                    sub["content"] = '\n\n'.join(sub["lines"])
                    refined_chunks.append(dict(sub))
                    sub = {"heading": ch["heading"] + " (续)", "level": ch["level"], "lines": [], "tables": []}
                    sub_len = 0
                sub["lines"].append(para)
                sub_len += para_len
            sub["content"] = '\n\n'.join(sub["lines"])
            if sub["content"].strip():
                refined_chunks.append(sub)
        else:
            refined_chunks.append(ch)
    
    if buffer is not None and len(buffer["content"]) > 0:
        buffer["heading"] = " / ".join(buffer.get("_merged_headings", [buffer["heading"]]))
        refined_chunks.append(buffer)

    safe_chunks = []
    for ch in refined_chunks:
        content = _sanitize_visible_text(ch.get("content", ""))
        if not content:
            continue
        safe = dict(ch)
        safe["content"] = content
        safe["tables"] = [
            line for line in (_sanitize_visible_text(t) for t in (ch.get("tables", []) or []))
            if line
        ]
        safe.pop("_merged_headings", None)
        safe_chunks.append(safe)
    refined_chunks = safe_chunks
    
    filename = Path(filepath).name
    headings = [{"text": ch["heading"], "level": ch["level"]} for ch in refined_chunks]
    conclusion = "\n\n".join(
        ch.get("content", "").strip()
        for ch in refined_chunks
        if any(k in ch.get("heading", "").lower() for k in ("结论", "建议", "总结", "小结", "conclusion", "recommendation", "summary"))
    ).strip()
    
    filtered_markdown = _chunks_to_markdown(refined_chunks)

    return {
        "status": "success",
        "markdown": filtered_markdown,
        "chunks": refined_chunks,
        "conclusion": conclusion,
        "all_headings": [h["text"] for h in headings],
        "metadata": {
            "filename": filename,
            "total_chars": len(filtered_markdown),
            "source_total_chars": len(md),
            "n_chunks": len(refined_chunks),
            "has_conclusion": bool(conclusion),
            "chunking": "semantic",
        },
    }
def _md_to_chunks(md: str, filepath: str) -> dict:
    """按 ## 标题 + **粗体** + outlineLvl编号 切分 chunks，结论chunk内子标题不切分"""
    headings = []
    chunks = []
    conclusion = ""
    current = {"text": "", "level": 0, "content": "", "tables": []}
    inside_conclusion = False

    for line in md.split("\n"):
        hm = re.match(r"^(#{1,6})\s+(.*)", line)
        bm = re.match(r"^\*\*(.+?)\*\*\s*$", line)
        nm = re.match(r"^(\d+(?:\.\d+)*)\s+(.{2,})", line)
        is_heading = bool(hm or bm or nm)

        if is_heading:
            if hm:
                heading_text = hm.group(2).strip()
            elif bm:
                heading_text = bm.group(1).strip()
            else:
                heading_text = nm.group(0).strip()

            if _is_heading_conclusion(heading_text):
                inside_conclusion = True
            elif inside_conclusion and not hm:
                current["content"] += "\n" + line
                continue
            else:
                inside_conclusion = False

            if current["text"]:
                content_text = current["content"].strip()
                if content_text:
                    chunks.append({
                        "heading": current["text"],
                        "level": current["level"],
                        "content": content_text,
                        "tables": current.get("tables", []),
                    })
                    h_lower = current["text"].lower()
                    if any(k in h_lower for k in ("\u7ed3\u8bba", "\u5efa\u8bae", "\u603b\u7ed3", "\u5c0f\u7ed3", "conclusion")):
                        conclusion = content_text

            if hm:
                level = len(hm.group(1))
                text = hm.group(2).strip()
            elif bm:
                level = 2
                text = bm.group(1).strip()
            else:
                depth = nm.group(1).count(".") + 1
                level = min(depth + 1, 6)
                text = nm.group(0).strip()

            current = {"text": text, "level": level, "content": "", "tables": []}
            headings.append({"text": text, "level": level})
        else:
            stripped = line.strip()
            if stripped.startswith("|") and "|" in stripped[1:]:
                current["tables"] = current.get("tables", [])
                current["tables"].append(stripped)
            else:
                current["content"] += "\n" + line

    if current["text"]:
        content_text = current["content"].strip()
        if content_text:
            chunks.append({
                "heading": current["text"],
                "level": current["level"],
                "content": content_text,
                "tables": current.get("tables", []),
            })
            h_lower = current["text"].lower()
            if any(k in h_lower for k in ("\u7ed3\u8bba", "\u5efa\u8bae", "\u603b\u7ed3", "\u5c0f\u7ed3", "conclusion")):
                conclusion = content_text

    filename = Path(filepath).name
    return {
        "status": "success",
        "markdown": md,
        "chunks": chunks,
        "conclusion": conclusion,
        "all_headings": [h["text"] for h in headings],
        "metadata": {"filename": filename, "total_chars": len(md), "n_chunks": len(chunks), "has_conclusion": bool(conclusion)},
    }
def dl_parse_document(filepath: str, chunk_size: int = 500, chunk_overlap: int = 100) -> dict:
    """??????????????? PDF/DOCX/TXT/MD ????"""
    ext = Path(filepath).suffix.lower()
    if ext in _BLOCKED_INPUT_EXTENSIONS:
        return {"status": "error", "message": f"拒绝解析疑似加密/索引文件: {Path(filepath).name}"}
    if ext not in _SUPPORTED_INPUT_EXTENSIONS:
        return {"status": "error", "message": f"不支持的文件类型: {ext or '无扩展名'}"}
    try:
        if ext == ".pdf":
            return _parse_pdf(filepath)
        elif ext == ".docx":
            return _parse_docx(filepath)
        else:
            return _parse_text_fallback(filepath)
    except Exception as e:
        return {"status": "error", "message": str(e)}


def dl_extract_conclusion(filepath: str) -> dict:
    """????????"""
    result = dl_parse_document(filepath)
    if result.get("status") != "success":
        return {"status": "error", "message": result.get("message", "\u89e3\u6790\u5931\u8d25")}
    conclusion = result.get("conclusion", "")
    if not conclusion:
        return {"status": "no_conclusion", "message": "\u672a\u68c0\u6d4b\u5230\u7ed3\u8bba\u7ae0\u8282"}
    section_title = ""
    for chunk in result.get("chunks", []):
        if chunk.get("content", "").strip() == conclusion.strip():
            section_title = chunk.get("heading", "")
            break
    subsections = []
    current_sub = {"title": section_title or "\u7ed3\u8bba", "content": ""}
    for line in conclusion.split("\n"):
        sm = re.match(r"^#{3,6}\s+(.*)", line)
        if sm:
            if current_sub["content"].strip():
                subsections.append(current_sub)
            current_sub = {"title": sm.group(1).strip(), "content": ""}
        else:
            current_sub["content"] += line + "\n"
    if current_sub["content"].strip():
        subsections.append(current_sub)
    return {"status": "success", "conclusion": conclusion, "section_title": section_title or "\u7ed3\u8bba", "subsections": subsections}


def dl_get_document_summary(filepath: str) -> dict:
    """??????"""
    result = dl_parse_document(filepath)
    if result.get("status") != "success":
        return {"status": "error", "message": result.get("message", "")}
    section_summaries = []
    for chunk in result.get("chunks", []):
        text = chunk.get("content", "").strip()
        summary = text[:80].replace("\n", " ") + ("..." if len(text) > 80 else "")
        section_summaries.append({"heading": chunk.get("heading", ""), "summary": summary})
    return {"status": "success", "section_summaries": section_summaries, "conclusion": result.get("conclusion", "")[:500]}



# ============ Document Brain: AI Directory Compilation ============

def dl_prepare_for_wiki(markdown_content: str, filename: str, department: str) -> dict:
    """
    Document Brain Layer 1 — 数据准备函数。
    解析文档结构，返回 AI 编写 Wiki 页面所需的原材料。
    
    AI 应该使用这些数据来编写：
      - index.md（全局目录：列出部门所有文档 + 链接到摘要页）
      - summaries/{doc}.md（每文档的章节摘要 + 关键指标）
      - concepts/{概念}.md（跨文档概念页，如"UASB工艺"含所有相关文档引用）
      - entities/{实体}.md（跨文档实体页，如"YY水务"含所有项目）
    
    Args:
        markdown_content: Full document markdown from dl_parse_document()
        filename: Original document filename
        department: Department name
    
    Returns:
        Structured data for AI wiki compilation
    """
    import re as _re

    prepared = _semantic_chunk(markdown_content, filename)
    if prepared.get("status") == "success":
        markdown_content = prepared.get("markdown", "")
    else:
        markdown_content = ""
    
    doc_slug = _re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fff-]', '_', filename.rsplit('.', 1)[0])[:60]
    
    # Parse section structure
    heading_pattern = _re.compile(r'^(#{1,6})\s+(.+)$', _re.MULTILINE)
    sections = []
    current = {"title": "文档开头", "level": 0, "content_lines": [], "tables": []}
    
    for line in markdown_content.split('\n'):
        hm = heading_pattern.match(line)
        if hm:
            if current["content_lines"] or current["tables"]:
                sections.append(current)
            current = {
                "title": hm.group(2).strip(),
                "level": len(hm.group(1)),
                "content_lines": [],
                "tables": [],
            }
        else:
            stripped = line.strip()
            if stripped.startswith("|") and "|" in stripped[1:]:
                current["tables"].append(stripped)
            elif stripped:
                current["content_lines"].append(stripped)
    if current["content_lines"] or current["tables"]:
        sections.append(current)
    
    # Build chapter summaries
    chapter_summaries = []
    for sec in sections:
        if sec["level"] <= 2 and sec["title"]:
            body_preview = ' '.join(sec["content_lines"][:3])[:200]
            chapter_summaries.append({
                "heading": sec["title"],
                "level": sec["level"],
                "preview": body_preview,
                "has_table": len(sec["tables"]) > 0,
            })
    
    # Build full body text for AI to read
    body = []
    for sec in sections:
        if sec["title"]:
            body.append(f"{'#' * sec['level']} {sec['title']}")
        body.extend(sec["content_lines"])
        body.extend(sec["tables"])
        body.append("")
    full_body = '\n'.join(body)
    
    return {
        "status": "success",
        "department": department,
        "filename": filename,
        "doc_slug": doc_slug,
        "markdown": markdown_content,
        "full_body": full_body,
        "sections": sections,
        "chapter_summaries": chapter_summaries,
        "section_count": len(sections),
        "total_chars": len(markdown_content),
        # 供 AI 编写 wiki 的提示
        "wiki_instructions": {
            "index_md": "编写全局 index.md：列出部门所有文档 + 每文档1-2句概要 + 关键指标，链接到 summaries/{slug}.md",
            "summary_page": f"编写 summaries/{doc_slug}.md：本文档的完整目录 + 每章1-2句摘要 + 关键数据表格",
            "concept_pages": "跨文档编写 concepts/{概念名}.md：提取核心技术概念（如工艺流程/指标/方法），列出所有相关文档及该概念在各文档中的体现",
            "entity_pages": "跨文档编写 entities/{实体名}.md：提取公司/项目/产品实体，列出所有关联文档及关系",
        },
    }


def dl_compile_directory(markdown_content: str, filename: str, department: str,
                         wiki_dir: str = None) -> dict:
    """
    Document Brain Layer 1 — 数据准备函数。
    返回 AI 编写 Wiki 所需的全部结构化数据。不写文件——AI 负责写。

    返回数据供 AI 编写四层 Wiki：
      L1: index.md — 全局目录
      L2: summaries/{doc}.md — 单文档章节目录+摘要
      L3: concepts/{概念}.md — 跨文档概念对比
      L4: entities/{实体}.md — 跨文档实体关联
    """
    return dl_prepare_for_wiki(markdown_content, filename, department)

