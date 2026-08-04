"""文档解析模块：PDF/DOCX/PPTX/XLSX + Docling 结构化。"""
import sys, os, json, re, shutil, io
from pathlib import Path
from PIL import Image

# ── 模块级配置（默认值，由 boss_upload.configure() 覆盖）──
_config = {
    "base_dir": None,
    "originals_dir": None,
    "encrypted_dir": None,
    "boss_password": "",
    "departments": {},
    "dept_patterns": {},
}

# ── OCR 状态追踪 ──
_ocr_available = None

def _split_into_fragments(text: str, sentences_per_fragment: int = 2) -> list:
    """将全文按句子边界切分成2-3句片段，保留全部信息，不加结论。"""
    if not text or not text.strip():
        return []
    sentences = re.split(r'(?<=[。！？!?])\s*', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return []
    fragments = []
    i = 0
    while i < len(sentences):
        remaining = len(sentences) - i
        if remaining <= 3:
            chunk = sentences[i:]
        elif remaining == 4:
            chunk = sentences[i:i+2]
        else:
            chunk = sentences[i:i+sentences_per_fragment]
        fragment = "".join(chunk).strip()
        if fragment:
            fragments.append(fragment)
        i += len(chunk)
    return fragments

def _import_docling():
    """??docling skill?wrapper???pymupdf4llm???IBM Docling?"""
    import sys
    if __file__:
        skills_base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    else:
        skills_base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    docling_path = os.path.join(skills_base, "docling", "scripts")
    if docling_path not in sys.path:
        sys.path.insert(0, docling_path)
    import docling_wrapper
    return docling_wrapper
def extract_pdf(filepath: str) -> dict:
    import pdfplumber
    text_parts = []
    tables_data = []

    with pdfplumber.open(filepath) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            text_parts.append(f"\n--- 第{page_idx + 1}页 ---\n")

            # 策略1：默认文本提取
            page_text = page.extract_text(
                x_tolerance=3,
                y_tolerance=3,
                keep_blank_chars=False,
            )
            # 策略2：如果默认提取为空或过短，用宽松参数重试
            if not page_text or len(page_text) < 20:
                page_text = page.extract_text(
                    x_tolerance=5,
                    y_tolerance=5,
                )

            if page_text:
                text_parts.append(page_text)

            # 提取表格（多策略）
            tables = page.extract_tables()
            if not tables:
                tables = page.extract_tables({
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text",
                    "snap_tolerance": 5,
                    "intersection_x_tolerance": 5,
                })

            for table in tables:
                if table and any(any(cell for cell in row) for row in table):
                    cleaned = _clean_table(table)
                    tables_data.append(cleaned)
                    text_parts.append(_table_to_text(cleaned))

            # OCR：提取页面嵌入图片中的文字（工艺流程图等）
            ocr_text = _ocr_page_images(page, page_idx)
            if ocr_text:
                text_parts.append(ocr_text)

    return {"text": "\n".join(text_parts), "tables": tables_data}


def _clean_table(table: list) -> list:
    """填充合并单元格产生的None值：同行向右填充，同列向下填充"""
    if not table:
        return table
    max_cols = max(len(row) for row in table)
    # 补齐不等长行
    for row in table:
        while len(row) < max_cols:
            row.append("")

    # 第一遍：同行内，None继承左侧值（横向合并）
    for row in table:
        for i in range(1, len(row)):
            if row[i] is None or str(row[i]).strip() == "":
                row[i] = row[i - 1]

    # 第二遍：不同行间，None继承上方值（纵向合并）
    for col in range(max_cols):
        for row_idx in range(1, len(table)):
            val = table[row_idx][col]
            if val is None or str(val).strip() == "":
                table[row_idx][col] = table[row_idx - 1][col]

    return table


def _table_to_text(table: list) -> str:
    """将表格转为可读文本（保留到full_text中便于检索）"""
    lines = ["[表格]"]
    for row in table:
        cells = [str(c).strip() if c else "" for c in row]
        lines.append(" | ".join(cells))
    lines.append("")
    return "\n".join(lines)


def _ocr_page_images(page, page_idx: int) -> str:
    """提取PDF页面中的嵌入图片并OCR，用于捕获流程图等图片中的文字"""
    try:
        from PIL import Image
        import io
    except ImportError:
        return ""

    ocr_texts = []
    try:
        for img_info in page.images:
            try:
                img_bytes = img_info["stream"].get_data()
                img = Image.open(io.BytesIO(img_bytes))
                img_text = _ocr_image(img)
                if img_text:
                    ocr_texts.append(f"[第{page_idx + 1}页图片文字]\n{img_text}")
            except Exception:
                continue
    except Exception:
        pass

    return "\n".join(ocr_texts) if ocr_texts else ""


def _ocr_image(img) -> str:
    """对单张图片执行OCR，自动检测tesseract可用性"""
    try:
        import pytesseract
        # 预处理：放大 + 灰度化 提高识别率
        img = img.convert("L")
        w, h = img.size
        if w < 1000:
            img = img.resize((w * 2, h * 2), Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC)
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return text.strip()
    except ImportError:
        return ""
    except Exception:
        return ""


def extract_docx(filepath: str) -> dict:
    """
    提取DOCX：段落和表格按文档顺序交替处理，
    表格嵌在引用它的段落后面（如"表2-3 设计出水水质"后面直接跟表格数据）。
    """
    from docx import Document
    from lxml import etree
    doc = Document(filepath)
    text_parts = []
    tables_data = []
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = doc.element.body

    def get_para_text(p_elem):
        return "".join(t.text or "" for t in p_elem.iter("{%s}t" % W)).strip()

    def get_table_text(tbl_elem):
        rows = tbl_elem.findall(".//{%s}tr" % W)
        lines = ["[表格]"]
        for row in rows:
            cells = [c.text or "" for c in row.iter("{%s}t" % W)]
            lines.append(" | ".join(cells))
        return "\n".join(lines)

    # 按XML中的文档顺序交替遍历段落和表格
    child_elements = list(body)
    child_tags = [c.tag.split("}")[1] if "}" in c.tag else c.tag for c in child_elements]

    table_idx = 0  # doc.tables 的下标计数器
    i = 0
    while i < len(child_elements):
        tag = child_tags[i]
        elem = child_elements[i]

        if tag == "p":
            para_text = get_para_text(elem)
            if para_text:
                text_parts.append(para_text)
                # 检查下一个元素是否是表格（表格紧跟在段落后面）
                if i + 1 < len(child_elements) and child_tags[i + 1] == "tbl":
                    tbl_elem = child_elements[i + 1]
                    tbl_data = []
                    for row in tbl_elem.findall(".//{%s}tr" % W):
                        row_data = [c.text or "" for c in row.iter("{%s}t" % W)]
                        tbl_data.append(row_data)
                        tables_data.append(row_data)
                    text_parts.append(get_table_text(tbl_elem))
                    table_idx += 1
                    i += 1  # 跳过表格，已处理

        elif tag == "tbl":
            # 表格不在任何段落后面（兜底：追加到末尾）
            tbl_data = []
            for row in elem.findall(".//{%s}tr" % W):
                row_data = [c.text or "" for c in row.iter("{%s}t" % W)]
                tbl_data.append(row_data)
                tables_data.append(row_data)
            text_parts.append(get_table_text(elem))
            table_idx += 1

        i += 1

    return {"text": "\n".join(text_parts), "tables": tables_data}


def extract_file(filepath: str) -> dict:
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".pdf":
        return extract_pdf(filepath)
    elif ext in (".docx", ".doc"):
        return extract_docx(filepath)
    else:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            return {"text": f.read(), "tables": []}

