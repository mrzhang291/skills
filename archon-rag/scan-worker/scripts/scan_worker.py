"""
多Agent并行扫描Worker：从pending目录抢任务 → AI扫描 → 写structured结果
使用文件重命名实现原子抢任务，支持多个CherryStudio标签页并行处理
"""
import os
import json
import sys
import uuid
import re
from datetime import datetime
from pathlib import Path

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


def _split_into_fragments(text: str, sentences_per_fragment: int = 2) -> list:
    """
    将全文按句子边界切分成2-3句片段，保留全部信息，不加结论。
    返回片段列表。
    """
    if not text or not text.strip():
        return []

    # 按中英文句末标点拆分句子
    sentences = re.split(r'(?<=[。！？!?])\s*', text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return []

    fragments = []
    i = 0
    while i < len(sentences):
        # 取2-3句，优先取2句，剩余不足时取全部
        remaining = len(sentences) - i
        if remaining <= 3:
            chunk = sentences[i:]
        elif remaining == 4:
            # 4句→2+2
            chunk = sentences[i:i+2]
        else:
            chunk = sentences[i:i+sentences_per_fragment]

        fragment = "".join(chunk).strip()
        if fragment:
            fragments.append(fragment)
        i += len(chunk)

    return fragments


def _fragments_to_text(fragments: list) -> str:
    """将片段列表合并为存储文本，用分隔符隔开"""
    return "\n---\n".join(fragments)


def _get_pending_dir(base_dir: str) -> str:
    return os.path.join(base_dir, ".agent_data", "pending")

def _get_structured_dir(base_dir: str) -> str:
    sd = os.path.join(base_dir, ".agent_data", "structured")
    os.makedirs(sd, exist_ok=True)
    return sd



# 全局配置（可通过 configure() 覆盖）
_config = {
    "patterns": None,  # 自定义关键数据提取正则，None 则用 DEFAULT_KEY_DATA_PATTERNS
}
def configure(base_dir: str, patterns: list = None):

    """设置共享存储路径"""
    os.makedirs(_get_pending_dir(base_dir), exist_ok=True)
    if patterns:
        _config["patterns"] = patterns
    return {"base_dir": base_dir, "pending_dir": _get_pending_dir(base_dir)}


def pending_count(base_dir: str) -> int:
    """剩余待处理文件数"""
    d = _get_pending_dir(base_dir)
    if not os.path.exists(d):
        return 0
    return len([f for f in os.listdir(d) if f.startswith("pending_")])


def claim_next_pending(base_dir: str) -> dict | None:
    """
    原子抢任务：将 pending_xxx.json 重命名为 processing_xxx.json
    多个Agent同时调用时，只有一个能成功（文件系统保证rename原子性）
    返回: {"pending_id": "...", "filename": "...", "text": "...", "tables": [...]} 或 None
    """
    d = _get_pending_dir(base_dir)
    if not os.path.exists(d):
        return None

    for f in sorted(os.listdir(d)):
        if not f.startswith("pending_"):
            continue
        pending_path = os.path.join(d, f)
        processing_path = os.path.join(d, f.replace("pending_", "processing_"))
        try:
            os.rename(pending_path, processing_path)
        except OSError:
            continue  # 被别的Agent抢走了

        with open(processing_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        try:
            scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            import task_status
            task_status.record_attempt(base_dir, data.get("pending_id", ""), status="claimed")
        except Exception:
            pass
        return {
            "pending_id": data.get("pending_id", ""),
            "filename": data.get("filename", ""),
            "department": data.get("department", ""),
            "text": _sanitize_visible_text(data.get("text", "")),
            "tables": data.get("tables", []),
        }
    return None


def claim_batch(base_dir: str, max_batch: int = 5) -> list:
    """
    原子批量抢任务：一次抢占最多 max_batch 个 pending 文件。
    多个Agent同时调用时，各自获得不同任务（文件系统 rename 保证原子性）。
    返回: [{"pending_id": "...", "filename": "...", "text": "...", ...}, ...]
          若无可用任务，返回空列表 []
    """
    d = _get_pending_dir(base_dir)
    if not os.path.exists(d):
        return []

    tasks = []
    for f in sorted(os.listdir(d)):
        if not f.startswith("pending_"):
            continue
        if len(tasks) >= max_batch:
            break

        pending_path = os.path.join(d, f)
        processing_path = os.path.join(d, f.replace("pending_", "processing_"))
        try:
            os.rename(pending_path, processing_path)
        except OSError:
            continue  # 被别的Agent抢走了

        with open(processing_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        tasks.append({
            "pending_id": data.get("pending_id", ""),
            "filename": data.get("filename", ""),
            "department": data.get("department", ""),
            "text": _sanitize_visible_text(data.get("text", "")),
            "tables": data.get("tables", []),
        })

    return tasks


def complete_scan(base_dir: str, pending_id: str,
                  tags: list, summary: str, entities: list,
                  key_data: dict, filename: str = "",
                  department: str = "", full_text: str = "",
                  tables: list = None,
                  client_name: str = "", project_name: str = "",
                  product_capacity: str = "", quality_summary: str = "",
                  objective: str = "", process: str = "") -> str:
    """
    Agent完成AI扫描后调用：写入structured结果，删除processing文件
    如果agent未传full_text/tables，自动从processing文件兜底读取，防止数据丢失
    如果有 Docling chunks（新流程），优先使用 chunks 结构
    返回structured文件路径
    """
    d = _get_pending_dir(base_dir)

    # 自动兜底：从processing文件读取
    processing_path = os.path.join(d, f"processing_{pending_id}.json")
    proc_data = {}
    if os.path.exists(processing_path):
        with open(processing_path, "r", encoding="utf-8") as f:
            proc_data = json.load(f)

    if not full_text:
        full_text = proc_data.get("text", "")
    full_text = _sanitize_visible_text(full_text)
    if not tables:
        tables = proc_data.get("tables", [])
    if not filename:
        filename = proc_data.get("filename", "")
    if not department:
        department = proc_data.get("department", "")

    # 优先使用 Docling chunks（新流程），否则降级用旧文本切分
    docling_chunks = proc_data.get("_docling_chunks", [])
    docling_conclusion = proc_data.get("_docling_conclusion", "")
    docling_key_data = proc_data.get("_docling_key_data", {})
    safe_tags = [_sanitize_visible_text(str(t)) for t in (tags or []) if _sanitize_visible_text(str(t))]
    safe_summary = _sanitize_visible_text(summary)
    safe_entities = [_sanitize_visible_text(str(e)) for e in (entities or []) if _sanitize_visible_text(str(e))]
    safe_key_data = {
        _sanitize_visible_text(str(k)): _sanitize_visible_text(str(v))
        for k, v in (key_data or docling_key_data or {}).items()
    }
    safe_docling_chunks = []
    for ch in docling_chunks:
        content = _sanitize_visible_text(ch.get("content", ""))
        if not content:
            continue
        safe_ch = dict(ch)
        safe_ch["content"] = content
        safe_ch["tables"] = [
            line for line in (_sanitize_visible_text(t) for t in (ch.get("tables", []) or []))
            if line
        ]
        safe_docling_chunks.append(safe_ch)

    if safe_docling_chunks:
        # 新流程：只写元数据到 structured，原文保持在 processing 中（安全考虑）
        structured = {
            "filename": filename,
            "department": department,
            "tags": safe_tags,
            "summary": safe_summary,
            "entities": safe_entities,
            "key_data": safe_key_data,
            "client_name": _sanitize_visible_text(client_name),
            "project_name": _sanitize_visible_text(project_name),
            "product_capacity": _sanitize_visible_text(product_capacity),
            "quality_summary": _sanitize_visible_text(quality_summary),
            "objective": _sanitize_visible_text(objective),
            "process": _sanitize_visible_text(process),
            "_docling_chunks": safe_docling_chunks,
            "_docling_conclusion": _sanitize_visible_text(docling_conclusion),
            "_docling_markdown": _sanitize_visible_text(proc_data.get("_docling_markdown", "")),
            # full_text/tables 保留在 processing 文件中，由 boss_upload_step2 加密读取
        }
    else:
        # 旧流程降级：只写元数据到 structured
        fragments = _split_into_fragments(full_text, sentences_per_fragment=2)
        fragmented_text = _fragments_to_text(fragments)
        structured = {
            "filename": filename,
            "department": department,
            "tags": safe_tags,
            "summary": safe_summary,
            "entities": safe_entities,
            "key_data": safe_key_data,
            "client_name": _sanitize_visible_text(client_name),
            "project_name": _sanitize_visible_text(project_name),
            "product_capacity": _sanitize_visible_text(product_capacity),
            "quality_summary": _sanitize_visible_text(quality_summary),
            "objective": _sanitize_visible_text(objective),
            "process": _sanitize_visible_text(process),
            "fragments": fragments,
            # full_text/tables 保留在 processing 文件中
        }

    validation = {"valid": True, "errors": []}
    try:
        scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import archon_validation
        validation = archon_validation.validate_scan_payload(structured)
        structured["_scan_validation"] = validation
        if not validation["valid"]:
            structured["_scan_status"] = "needs_review"
            review_dir = os.path.join(base_dir, ".agent_data", "review")
            os.makedirs(review_dir, exist_ok=True)
            with open(os.path.join(review_dir, f"review_{pending_id}.json"), "w", encoding="utf-8") as f:
                json.dump({"pending_id": pending_id, "structured": structured, "validation": validation}, f, ensure_ascii=False, indent=2)
            if os.environ.get("ARCHON_STRICT_SCAN") == "1":
                raise ValueError(f"scan validation failed: {validation['errors']}")
    except ImportError:
        pass
    try:
        scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import task_status
        task_status.update_task_status(
            base_dir,
            pending_id,
            "scanned" if validation.get("valid", True) else "needs_review",
            filename=filename,
            department=department,
            errors=validation.get("errors", []),
        )
    except Exception:
        pass

    result_path = os.path.join(_get_structured_dir(base_dir), f"structured_{pending_id}.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(structured, f, ensure_ascii=False)

    # processing 文件保留（含 full_text/tables），由 boss_upload_step2 加密后删除

    return result_path


# ==================== 防幻觉扫描提示词 ====================

def _section_hint() -> str:
    """Read the active profile's allowed/excluded section hint."""
    try:
        scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import archon_config
        return archon_config.allowed_sections_hint()
    except Exception:
        return ""


SCAN_PROMPT = """请严格按以下规则提取文档结构化信息：

【严禁推断规则】
- 只提取原文中明确写出的信息，绝不推断、不补充、不猜测
- 代号/英文缩写原样保留，不翻译
- 原文没有明确写出的信息，标记为"原文未提及"
- 时间、数值等必须原文原样引用，不要自己计算或换算
- 表格数据提取不完整时标记置信度为"低"
- 只从允许章节提取：{section_rule}

文件名：{filename}
部门：{department}
文档内容（已按章节切块，表格完整保留在对应章节内）：
{text}

请返回以下JSON（每条信息必须含source_quote原文引用和confidence置信度）：
{{
  "tags": ["从原文中提取的关键标签"],
  "summary": "仅基于原文明确信息的摘要，不推断。保留代号/缩写。",
  "facts": [
    {{
      "fact": "提取的事实",
      "source_quote": "原文中对应的句子或段落（逐字引用）",
      "confidence": "高/中/低",
      "note": "补充说明"
    }}
  ],
  "entities": ["原文明确出现的实体/代号/缩写"],
  "client_name": "甲方/客户名称；原文未提及时填原文未提及",
  "project_name": "项目名称；原文未提及时填原文未提及",
  "product_capacity": "产品类型和产能/规模；原文未提及时填原文未提及",
  "quality_summary": "关键质量/规格指标摘要；优先原样保留数值和单位",
  "objective": "主要目标/目的；原文未提及时填原文未提及",
  "process": "方法/流程/工艺路线；原文未提及时填原文未提及",
  "key_data": {{
    // 关键数值：数值字段名: "原文数值"
  }},
  "missing_info": ["用户可能想知道但原文未明确提及的信息"]
}}

只返回JSON，不返回其他内容。"""


def get_scan_prompt(text: str, filename: str, department: str = "") -> str:
    return SCAN_PROMPT.format(
        text=_sanitize_visible_text(text),
        filename=filename,
        department=department,
        section_rule=_section_hint(),
    )


def get_prompt_for_pending(pending_data: dict) -> str:
    """
    根据 pending_data 自动选择提示词：
    - 有 Docling chunks（新流程）→ 用 chunked 提示词，省 token
    - 无 chunks（旧流程）→ 用全文提示词，向后兼容
    """
    chunks = pending_data.get("_docling_chunks", [])
    filename = pending_data.get("filename", "")
    department = pending_data.get("department", "")

    if chunks:
        # 新流程：heading-chunks 发给 AI（省 token）
        return get_chunked_scan_prompt(chunks, filename, department)
    else:
        # 旧流程：全文发给 AI
        text = pending_data.get("text", "")
        return get_scan_prompt(text, filename, department)


# ==================== 优化：Docling chunk 提示词（不发全文，只发章节） ====================

SCAN_CHUNK_PROMPT = """【任务】从以下文档章节内容中提取结构化信息。

【严禁推断规则】
- 只提取原文中明确写出的信息，绝不推断、不补充、不猜测
- 代号/英文缩写原样保留，不翻译
- 表格数据完整提取，原样引用数值
- 只处理已传入的允许章节：{section_rule}

文件名：{filename}
部门：{department}

文档章节（已按##标题切块，每块内表格完整保留）：
{chunked_text}

请返回JSON：
{{
  "tags": ["关键标签"],
  "summary": "基于章节内容的摘要",
  "facts": [
    {{
      "fact": "提取的事实",
      "source_quote": "原文引用",
      "confidence": "高/中/低"
    }}
  ],
  "entities": ["实体/代号列表"],
  "client_name": "甲方/客户名称；原文未提及时填原文未提及",
  "project_name": "项目名称；原文未提及时填原文未提及",
  "product_capacity": "产品类型和产能/规模；原文未提及时填原文未提及",
  "quality_summary": "关键质量/规格指标摘要；优先原样保留数值和单位",
  "objective": "主要目标/目的；原文未提及时填原文未提及",
  "process": "方法/流程/工艺路线；原文未提及时填原文未提及",
  "key_data": {{
    // 关键数值：数值字段名: "原文数值"
  }},
  "missing_info": ["原文未提及的信息"]
}}

只返回JSON。"""


BATCH_SCAN_PROMPT = """【任务】批量扫描多份文档，提取各自的结构化信息。

【规则】
- 每份文档独立提取，严格只引用原文信息，不推断
- 代号/缩写原样保留，数值原样引用
- 表格数据完整保留在对应章节
- 只处理允许章节：{section_rule}

{docs_text}

请返回JSON数组（每份文档一个对象）：
[
  {{
    "filename": "文档A文件名",
    "tags": [...],
    "summary": "...",
    "facts": [...],
    "entities": [...],
    "client_name": "...",
    "project_name": "...",
    "product_capacity": "...",
    "quality_summary": "...",
    "objective": "...",
    "process": "...",
    "key_data": {{...}},
    "missing_info": [...]
  }},
  {{
    "filename": "文档B文件名",
    ...
  }}
]

只返回JSON数组。"""


def get_chunked_scan_prompt(chunks: list, filename: str, department: str = "") -> str:
    """
    将 Docling heading-chunks 组装成精简提示词，发给 AI。
    每 chunk = "## 标题\\n正文"，表格完整保留在 chunk 内。
    发给 AI 的量远小于全文（14 chunks vs 5万字）。
    """
    chunked_lines = []
    for c in chunks:
        heading = c.get("heading", "").strip()
        content = _sanitize_visible_text(c.get("content", "")).strip()
        table_text = _sanitize_visible_text("\n".join(c.get("tables", []) or [])).strip()
        if not content and not table_text:
            continue
        if table_text and table_text not in content:
            content = (content + "\n" + table_text).strip()
        chunked_lines.append(f"## {heading}\n{content}")

    chunked_text = "\n\n".join(chunked_lines)
    return SCAN_CHUNK_PROMPT.format(
        chunked_text=chunked_text,
        filename=filename,
        department=department,
        section_rule=_section_hint(),
    )


def get_batch_scan_prompt(docs: list) -> str:
    """
    批量扫描提示词：多份文档一次 AI 调用。
    docs = [{"filename": "...", "chunks": [...], "department": "..."}]
    每份文档的 chunks 是 Docling heading-chunks 列表。
    """
    docs_text_parts = []
    for doc in docs:
        fname = doc.get("filename", "")
        dept = doc.get("department", "")
        chunks = doc.get("chunks", [])
        chunk_lines = []
        for c in chunks:
            heading = c.get("heading", "").strip()
            content = _sanitize_visible_text(c.get("content", "")).strip()
            table_text = _sanitize_visible_text("\n".join(c.get("tables", []) or [])).strip()
            if not content and not table_text:
                continue
            if table_text and table_text not in content:
                content = (content + "\n" + table_text).strip()
            chunk_lines.append(f"## {heading}\n{content}")
        docs_text_parts.append(
            f"【文档】{fname}\n【部门】{dept}\n\n" + "\n\n".join(chunk_lines)
        )

    docs_text = "\n\n==========\n\n".join(docs_text_parts)
    return BATCH_SCAN_PROMPT.format(docs_text=docs_text, section_rule=_section_hint())


# ==================== 优化：Docling 本地预处理（零 AI 消耗） ====================

def local_preprocess(filepath: str) -> dict:
    """
    用 Docling 本地解析文档，不花任何 AI token。
    返回 heading-chunks + 表格数据 + 结论，供 scan 提示词使用。

    流程：
      1. dl_parse_document() → 获取所有 heading-chunks（已按##切好）
      2. dl_extract_conclusion() → 提取结论章节
      3. 从 chunks 提取 key_data（数值、指标等，关键数据不上 AI）

    返回：
      {
        "status": "success",
        "chunks": [{"heading": "...", "content": "...", "level": 2}, ...],
        "conclusion": "结论全文",
        "key_data": {"throughput_in": "120 t/h", "yield": "86.3%"},
        "filename": "...",
      }
    """
    import sys as _sys
    import re as _re

    # ???? docling skill??? pymupdf4llm??? IBM Docling?
    try:
        if __file__:
            skills_base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        else:
            skills_base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        docling_path = os.path.join(skills_base, "docling", "scripts")
        if docling_path not in _sys.path:
            _sys.path.insert(0, docling_path)
        import docling_wrapper
        _HAS_DOCLING = True
    except ImportError:
        _HAS_DOCLING = False

    if not _HAS_DOCLING:
        return {"status": "error", "message": "docling skill ????????? pymupdf4llm"}

    try:
        result = docling_wrapper.dl_parse_document(filepath)
        if result.get("status") != "success":
            return {"status": "error", "message": result.get("message", "????")}

        chunks = result.get("chunks", [])
        conclusion = result.get("conclusion", "")

        # ???? key_data???????????? AI?
        key_data = _extract_key_data_locally(chunks)

        return {
            "status": "success",
            "chunks": chunks,
            "conclusion": conclusion,
            "key_data": key_data,
            "filename": Path(filepath).name,
            "total_chars": len(result.get("markdown", "")),
            "n_chunks": len(chunks),
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}




# ── 默认关键数据提取正则（污水处理场景），可通过 configure() 覆盖 ──
# ?? ??????????markdown????????????????

def _parse_table_to_metrics(table_lines: list) -> dict:
    """从 Markdown 表格中提取关键指标 → {参数名: 数值, ...}
    只提取公认的环保/化工参数，跳过纯数值行和时间序列数据"""
    import re
    if len(table_lines) < 2:
        return {}

    # 已知参数白名单（不区分大小写匹配）
    KNOWN_PARAMS = {
        # 英文参数（归一化后小写，去标点）
        "cod", "codcr", "toc", "bod", "bod5", "tn", "tp", "nh3n", "nh3",
        "ss", "ph", "cond", "do", "tss", "vss", "mlss", "mlvss",
        "so42", "cl", "no3n", "no2n", "po43",
        # 中文参数
        "碱度", "总碱度", "硬度", "总硬度", "色度", "浊度", "电导率", "水量",
        # 去除率
        "cod去除率", "toc去除率", "tn去除率", "tp去除率", "nh3n去除率",
        "去除率", "codr", "tocr", "tnr", "tpr",
        # 工艺参数
        "oc", "臭氧投加量", "臭氧浓度", "pac投加量", "聚铁投加量",
        "进水cod", "出水cod", "进水toc", "出水toc",
        "cod浓度", "toc浓度",
        # 比值
        "ct",
    }

    # 跳过行（第一列为纯数值/日期/空/单位）
    SKIP_FIRST = {"项目", "指标", "参数", "单位", "序号", "编号", "污染因子",
                  "污染物", "检测项目", "分析项目", "试验项目", "取样日期", "分析日期"}

    metrics = {}
    header_cells = [c.strip() for c in table_lines[0].split("|") if c.strip()]
    if len(header_cells) < 2:
        return {}

    # 检测表头方向：如果第一列是"项目/指标"类，则为纵向表（参数在行，值在列）
    is_vertical = any(h in SKIP_FIRST for h in header_cells[:2])

    for row_line in table_lines[1:]:
        cells = [c.strip() for c in row_line.split("|") if c.strip()]
        if len(cells) < 2:
            continue
        first_cell = cells[0]

        # 跳过空行
        if not first_cell or len(first_cell) > 30:
            continue
        # 跳过纯数字（含千分位逗号）和日期
        if re.match(r"^[\d,]+(?:\.\d+)?$", first_cell):
            continue
        if re.match(r"^\d{4}[\./-]\d{1,2}", first_cell):
            continue
        if first_cell in SKIP_FIRST:
            continue
        # 跳过常见无意义标签
        if len(first_cell) <= 2 and first_cell not in KNOWN_PARAMS:
            continue

        if is_vertical:
            # 纵向表：first_cell 是参数名，后续列为对应值
            param_lower = re.sub(r"[^a-z0-9\u4e00-\u9fff-]", "", first_cell).lower()
            if param_lower not in KNOWN_PARAMS:
                continue
            for j in range(1, len(cells)):
                val = cells[j]
                if not val or val in ("/", "-", "—", "未检出", "分析标准"):
                    continue
                if not re.search(r"\d", val):
                    continue
                col_name = header_cells[j] if j < len(header_cells) else ""
                key = f"{first_cell}" if not col_name or col_name in SKIP_FIRST else f"{first_cell}({col_name})"
                if key not in metrics:
                    metrics[key] = val
        else:
            # 横向表：表头是参数名，first_cell 是样本标识
            for j in range(1, len(cells)):
                val = cells[j]
                if not val or val in ("/", "-", "—", "未检出", "分析标准"):
                    continue
                if not re.search(r"\d", val):
                    continue
                col_name = header_cells[j] if j < len(header_cells) else ""
                param_lower = re.sub(r"[^a-z0-9\u4e00-\u9fff-]", "", col_name).lower()
                if param_lower not in KNOWN_PARAMS:
                    continue
                key = col_name if not first_cell or first_cell in SKIP_FIRST else f"{first_cell}({col_name})"
                if key not in metrics:
                    metrics[key] = val

    return metrics


def _extract_key_data_from_tables(chunks: list) -> dict:
    """?chunks?tables????????????"""
    all_metrics = {}
    
    for chunk in chunks:
        tables = chunk.get("tables", [])
        if not tables:
            continue
        
        # ??????????????
        table_groups = []
        current_table = []
        for line in tables:
            if line.strip().startswith("|"):
                current_table.append(line)
            else:
                if current_table:
                    table_groups.append(current_table)
                    current_table = []
        if current_table:
            table_groups.append(current_table)
        
        # ?????????
        heading = chunk.get("heading", "")
        for tbl in table_groups:
            tbl_metrics = _parse_table_to_metrics(tbl)
            # ?????????????
            if heading:
                tbl_metrics = {f"{heading}-{k}" if heading not in k else k: v for k, v in tbl_metrics.items()}
            all_metrics.update(tbl_metrics)
    
    return all_metrics


def _extract_key_data_locally(chunks: list) -> dict:
    """
    ??????????????markdown??????
    ??????????????????????
    ??: {"throughput_in": "30,161", "yield": "86.3%", ...}
    """
    # ???tables????
    metrics = _extract_key_data_from_tables(chunks)
    
    # ???????tables??????content?????
    if not metrics:
        import re
        for chunk in chunks:
            text = chunk.get("content", "")
            # ?? "??? ????" ??
            for m in re.finditer(r'([A-Za-z一-鿿][A-Za-z0-9一-鿿/_\-]{1,20})\s*[:]?\s*(\d[\d,\.]+\s*(?:mg/L|%|℃|d|g/L|μS/cm|m3/d|gCOD/L·d)?)', text):
                key = m.group(1).strip()
                val = m.group(2).strip()
                if key not in metrics:
                    metrics[key] = val
    
    return metrics
