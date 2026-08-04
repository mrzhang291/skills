import sys
"""
老板端上传模块：文件提取 → pending JSON → AI扫描 → 加密入库 → 归档原文件
依赖 secure-storage 技能（加密存储）+ knowledge-rag（索引检索）+ docling（结构化解析）
"""
import os
import json
import uuid
import re
import csv
import shutil
from datetime import datetime
from pathlib import Path

_CIPHERTEXT_RUN_RE = re.compile(r"(?:[A-Za-z0-9]|[<>=;|{}\[\]~`^\\/:@#$%&*+_,.\-]){96,}")
_FERNET_TOKEN_RE = re.compile(r"\bgAAAAA[A-Za-z0-9_-]{80,}\b")
_BLOCKED_UPLOAD_EXTENSIONS = {".enc", ".bin", ".db", ".sqlite", ".lance", ".idx"}
_SUPPORTED_UPLOAD_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv"}


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


def _get_metrics_csv_path(department: str) -> str:
    """获取部门汇总CSV路径：base_dir/.agent_data/metrics/{部门}_key_metrics.csv"""
    base_dir = _config.get("base_dir", "")
    if not base_dir:
        return None
    metrics_dir = os.path.join(base_dir, ".agent_data", "metrics")
    os.makedirs(metrics_dir, exist_ok=True)
    return os.path.join(metrics_dir, f"{department}_key_metrics.csv")


def _append_key_metrics_to_csv(department: str, filename: str,
                                 key_data: dict, summary: str = "") -> dict:
    """
    将关键数据追加到部门汇总CSV（用于跨文档对比分析）。
    CSV结构：department, filename, upload_time, [key_data各字段...], summary
    多文档追加时自动合并列头，新字段追加到最右列。
    """
    csv_path = _get_metrics_csv_path(department)
    if not csv_path:
        return {"status": "error", "message": "base_dir 未配置"}

    fixed_fields = ["department", "filename", "upload_time", "summary"]
    file_exists = os.path.exists(csv_path)

    if file_exists:
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                existing_header = next(reader, [])
                existing_rows = list(reader)
            fieldnames = existing_header if existing_header else fixed_fields.copy()
        except Exception:
            fieldnames = fixed_fields.copy()
            existing_rows = []
    else:
        fieldnames = fixed_fields.copy()
        existing_rows = []

    new_cols_added = False
    if key_data:
        for k in key_data.keys():
            if k not in fieldnames:
                fieldnames.append(k)
                new_cols_added = True

    row = {f: "" for f in fieldnames}
    row["department"] = department
    row["filename"] = filename
    row["upload_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row["summary"] = summary
    if key_data:
        row.update(key_data)

    try:
        if new_cols_added:
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for old_row in existing_rows:
                    padded = {f: old_row[i] if i < len(old_row) else "" for i, f in enumerate(fieldnames)}
                    writer.writerow(padded)
                writer.writerow(row)
        else:
            with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writerow(row)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    json_path = csv_path.replace(".csv", ".json")
    try:
        existing = []
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as jf:
                existing = json.load(jf)
        existing.append(row)
        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump(existing, jf, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return {"status": "success", "csv_path": csv_path}

def _normalize_project_name(filename: str) -> str:
    """Extract a stable project key from a filename for conflict comparison."""
    import re
    name = re.sub(r'\.[^.]+$', '', filename)
    name = re.sub(r"[\[\(].*?[\]\)]", "", name)
    name = re.sub(r"[\s_\-]+", "", name).strip()
    name = re.sub(r"(报告|总结|方案|测试|试验|分析|说明|文档|资料|项目)$", "", name)
    return name[:30] or filename[:30]


def _metric_value_to_str(v) -> str:
    """将指标值转为可比较的归一化字符串。
    处理：千分位逗号去除、全角百分号归一化、数值+单位分离后数值标准化。
    例如 "8,752mg/L" → "8752.0mg/l"，"42％" → "42.0%"，"42.0%" → "42.0%"
    """
    if v is None or v == "" or v == "原文未提及":
        return None
    if isinstance(v, str):
        v = v.strip()
    s = str(v).strip()
    # 提取数值部分（含千分位逗号）+ 可选单位
    m = re.match(r"^([\d,]+(?:\.\d+)?)\s*(.*)$", s)
    if m:
        num_str = m.group(1).replace(",", "")  # 去千分位逗号
        unit = m.group(2).strip()
        # 归一化百分号
        if unit in ("%", "％"):
            unit = "%"
        elif unit:
            unit = unit.lower()  # mg/L → mg/l
        try:
            num = float(num_str)
            if unit:
                return f"{num}{unit}"
            return str(num)
        except ValueError:
            return s
    # 纯数字字符串
    try:
        return str(float(s))
    except (ValueError, TypeError):
        return s


def check_conflicts(department: str, filename: str, key_data: dict) -> dict:
    """
    上传时冲突检测：检查是否存在同一项目但关键指标不同的已有记录。
    返回冲突列表，由boss决定保留策略。
    """
    if not key_data:
        return {"status": "no_conflicts", "conflicts": [], "message": "无关键指标数据，跳过冲突检测"}

    existing = list_department_metrics(department)
    if existing.get("status") != "success" or not existing.get("records"):
        return {"status": "no_conflicts", "conflicts": [], "message": "部门无历史记录，无冲突"}

    upload_project = _normalize_project_name(filename)
    conflicts = []

    for record in existing["records"]:
        record_filename = record.get("filename", "")
        record_project = _normalize_project_name(record_filename)

        # 判断是否为同一项目（名称相似度判断）
        if upload_project == record_project or upload_project in record_project or record_project in upload_project:
            # 逐个对比关键指标
            for k, new_val in key_data.items():
                old_val = record.get(k, "")
                new_val_str = _metric_value_to_str(new_val)
                old_val_str = _metric_value_to_str(old_val)

                # 两方都有有效值时才比较
                if new_val_str and old_val_str and new_val_str != old_val_str:
                    try:
                        # 数值差异超过5%视为冲突
                        new_num = float(new_val_str)
                        old_num = float(old_val_str)
                        if old_num != 0 and abs(new_num - old_num) / abs(old_num) > 0.05:
                            conflicts.append({
                                "metric": k,
                                "old_value": old_val,
                                "new_value": new_val,
                                "old_file": record_filename,
                                "new_file": filename,
                                "project": upload_project,
                            })
                    except (ValueError, TypeError, ZeroDivisionError):
                        # 非数值型，直接字符串比较
                        if new_val_str != old_val_str:
                            conflicts.append({
                                "metric": k,
                                "old_value": old_val,
                                "new_value": new_val,
                                "old_file": record_filename,
                                "new_file": filename,
                                "project": upload_project,
                            })

    if not conflicts:
        return {"status": "no_conflicts", "conflicts": [], "message": "无冲突"}

    return {
        "status": "conflicts_found",
        "conflicts": conflicts,
        "project": upload_project,
        "conflict_count": len(conflicts),
        "message": f"检测到 {len(conflicts)} 个冲突指标，涉及项目 [{upload_project}]，请选择处理方式：保留旧记录(keep_old)/覆盖(overwrite)/同时保留并标记(keep_both)",
    }


def resolve_conflict(department: str, filename: str, resolution: str) -> dict:
    """
    冲突解决：boss选择保留策略后更新CSV。
    resolution: "keep_old" | "overwrite" | "keep_both"
    """
    if resolution not in ("keep_old", "overwrite", "keep_both"):
        return {"status": "error", "message": f"无效的resolution: {resolution}"}

    csv_path = _get_metrics_csv_path(department)
    if not csv_path or not os.path.exists(csv_path):
        return {"status": "error", "message": "CSV文件不存在"}

    # 读取所有记录
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        records = list(reader)

    # 找目标记录（最新一条，即刚才上传的）
    target_record = None
    target_idx = None
    for i, r in enumerate(records):
        if r.get("filename") == filename:
            target_record = r
            target_idx = i
            break

    if target_record is None:
        return {"status": "error", "message": f"CSV中未找到记录: {filename}"}

    if resolution == "keep_old":
        # 删除新记录
        records.pop(target_idx)
        action = "已删除新记录，保留原有数据"

    elif resolution == "keep_both":
        # 给新记录打标记
        records.pop(target_idx)
        new_filename = f"{filename} [冲突标记]"
        target_record["filename"] = new_filename
        # 添加冲突标记列（如果CSV中没有这个列）
        if "conflict_flag" not in fieldnames:
            fieldnames = list(fieldnames) + ["conflict_flag"]
        target_record["conflict_flag"] = "有冲突"
        records.append(target_record)
        action = "已保留新记录并标记冲突"

    elif resolution == "overwrite":
        # 覆盖：直接保留新记录（已经在CSV中），添加覆盖标记
        if "overwrite_flag" not in fieldnames:
            fieldnames = list(fieldnames) + ["overwrite_flag"]
        records[target_idx]["overwrite_flag"] = "已覆盖旧数据"
        action = "已覆盖旧数据"

    # 写回CSV
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)

    return {"status": "success", "resolution": resolution, "action": action}


def list_department_metrics(department: str) -> dict:
    """列出部门所有文档的关键指标（用于跨文档分析）"""
    csv_path = _get_metrics_csv_path(department)
    if not csv_path or not os.path.exists(csv_path):
        return {"status": "empty", "records": [], "message": f"部门 [{department}] 暂无汇总数据"}

    records = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(dict(row))
        return {"status": "success", "count": len(records), "records": records, "csv_path": csv_path}
    except Exception as e:
        return {"status": "error", "message": str(e)}


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

# 全局配置（由 configure() 设置）
_config = {
    "base_dir": None,           # 工作目录（临时文件、pending）
    "originals_dir": None,      # 原文件归档目录（按部门子文件夹）
    "encrypted_dir": None,      # 加密文件目录（按部门子文件夹）
    "boss_password": "",           # ??? configure(boss_password=...) ?????
    "departments": {},          # 部门→密码映射 {"general":"pwd1"}
    "dept_patterns": {},          # 部门→自定义patterns列表，空则用DEFAULT_KEY_DATA_PATTERNS
}


# ?? Config persistence ??

def _get_config_path() -> str:
    """Get path to persisted config file."""
    base = _config.get("base_dir", "")
    if not base:
        return ""
    agent_dir = os.path.join(base, ".agent_data")
    os.makedirs(agent_dir, exist_ok=True)
    return os.path.join(agent_dir, ".docbrain_config.json")


def _save_config():
    """Persist current _config to disk (excluding secrets)."""
    cfg_path = _get_config_path()
    if not cfg_path:
        return
    import json as _json
    save_data = {
        "base_dir": _config.get("base_dir", ""),
        "originals_dir": _config.get("originals_dir", ""),
        "encrypted_dir": _config.get("encrypted_dir", ""),
        "departments": dict(_config.get("departments", {})),
        "dept_patterns": dict(_config.get("dept_patterns", {})),
        "knowledge_rag_dir": os.environ.get("KNOWLEDGE_RAG_DIR", ""),
        "encrypted_dir_env": os.environ.get("ENCRYPTED_DIR", ""),
    }
    with open(cfg_path, "w", encoding="utf-8") as f:
        _json.dump(save_data, f, ensure_ascii=False, indent=2)


def _auto_load_config():
    """Auto-load persisted config. Searches: CWD chain, common locations."""
    import json as _json
    # Already configured?
    if _config.get("base_dir"):
        return True

    candidates = []
    # 1. CWD and parent chain
    cwd = os.getcwd()
    while cwd and len(cwd) > 3:
        cfg = os.path.join(cwd, ".agent_data", ".docbrain_config.json")
        if os.path.exists(cfg):
            candidates.append(cfg)
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent

    # 2. Common drives / mount points (multi-level scan for Samba mounts)
    common_roots = []
    if os.name == "nt":
        common_roots.extend(["D:\\", "E:\\", "C:\\"])
    else:
        common_roots.extend(["/mnt", "/media", os.path.expanduser("~")])

    for root in common_roots:
        try:
            if not os.path.isdir(root):
                continue
            # BFS walk up to 4 levels deep to find .agent_data
            to_scan = [(root, 0)]
            scanned = set()
            while to_scan:
                current, depth = to_scan.pop(0)
                if current in scanned or depth > 4:
                    continue
                scanned.add(current)
                try:
                    if os.path.isdir(os.path.join(current, ".agent_data")):
                        candidates.append(current)
                        continue  # found, don't go deeper into this branch
                    if depth < 4:
                        for entry in os.listdir(current):
                            full = os.path.join(current, entry)
                            if os.path.isdir(full):
                                to_scan.append((full, depth + 1))
                except (PermissionError, OSError):
                    pass
        except Exception:
            pass

    for cfg_path in candidates:
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                saved = _json.load(f)
            _config["base_dir"] = saved.get("base_dir", "")
            _config["originals_dir"] = saved.get("originals_dir", "")
            _config["encrypted_dir"] = saved.get("encrypted_dir", "")
            _config["departments"] = saved.get("departments", {})
            _config["dept_patterns"] = saved.get("dept_patterns", {})
            # Restore env vars
            kr_dir = saved.get("knowledge_rag_dir", "")
            enc_dir = saved.get("encrypted_dir_env", "")
            if kr_dir:
                os.environ["KNOWLEDGE_RAG_DIR"] = kr_dir
            if enc_dir:
                os.environ["ENCRYPTED_DIR"] = enc_dir
            return True
        except Exception:
            pass
    return False


def _get_structured_dir() -> str:
    """Get structured results directory."""
    return os.path.join(_config["base_dir"], ".agent_data", "structured")





def configure(base_dir: str, originals_dir: str = None, encrypted_dir: str = None,
              boss_password: str = None, departments: dict = None, key_data_patterns: dict = None):

    """????????? Document Brain ????"""
    import os as _os_env
    _config["base_dir"] = base_dir

    # Auto-derive paths if not explicitly set
    if not originals_dir:
        originals_dir = _os_env.path.join(base_dir, "private", "originals")
    if not encrypted_dir:
        encrypted_dir = _os_env.path.join(base_dir, "shared", "encrypted")
    knowledge_rag_dir = _os_env.path.join(base_dir, "shared", "knowledge_rag")
    wiki_dir = _os_env.path.join(base_dir, "shared", "wiki")

    # Set env vars BEFORE knowledge-rag module is imported (controls LanceDB path)
    _os_env.environ["KNOWLEDGE_RAG_DIR"] = knowledge_rag_dir
    _os_env.environ["ENCRYPTED_DIR"] = encrypted_dir

    _config["originals_dir"] = originals_dir
    _config["encrypted_dir"] = encrypted_dir
    if boss_password:
        _config["boss_password"] = boss_password
    if departments:
        _config["departments"] = departments

    # Create full directory tree
    agent_dirs = [
        _os_env.path.join(base_dir, ".agent_data", "pending"),
        _os_env.path.join(base_dir, ".agent_data", "structured"),
        _os_env.path.join(base_dir, ".agent_data", "uploads"),
        _os_env.path.join(base_dir, ".agent_data", "metrics"),
    ]
    for d in agent_dirs:
        os.makedirs(d, exist_ok=True)

    # Main data directories
    os.makedirs(originals_dir, exist_ok=True)
    os.makedirs(encrypted_dir, exist_ok=True)
    os.makedirs(knowledge_rag_dir, exist_ok=True)
    os.makedirs(_os_env.path.join(knowledge_rag_dir, "data"), exist_ok=True)
    os.makedirs(wiki_dir, exist_ok=True)

    # Department-specific directories (originals + encrypted + wiki)
    if departments:
        for dept in departments:
            os.makedirs(_os_env.path.join(originals_dir, dept), exist_ok=True)
            os.makedirs(_os_env.path.join(encrypted_dir, dept), exist_ok=True)
            dept_wiki = _os_env.path.join(wiki_dir, dept)
            os.makedirs(dept_wiki, exist_ok=True)
            os.makedirs(_os_env.path.join(dept_wiki, "summaries"), exist_ok=True)
            os.makedirs(_os_env.path.join(dept_wiki, "concepts"), exist_ok=True)
            os.makedirs(_os_env.path.join(dept_wiki, "entities"), exist_ok=True)

    # Persist config to disk for cross-session survival
    _save_config()
    return _config


def validate_structure() -> dict:
    """?? Document Brain ??????????????????"""
    import os as _os
    base_dir = _config.get("base_dir", "")
    if not base_dir:
        return {"status": "error", "message": "base_dir not configured. Call configure() first."}

    required = [
        _os.path.join(base_dir, "private", "originals"),
        _os.path.join(base_dir, "shared", "encrypted"),
        _os.path.join(base_dir, "shared", "knowledge_rag", "data"),
        _os.path.join(base_dir, "shared", "wiki"),
        _os.path.join(base_dir, ".agent_data", "pending"),
        _os.path.join(base_dir, ".agent_data", "structured"),
    ]

    departments = _config.get("departments", {})
    for dept in departments:
        required.extend([
            _os.path.join(base_dir, "private", "originals", dept),
            _os.path.join(base_dir, "shared", "encrypted", dept),
            _os.path.join(base_dir, "shared", "wiki", dept),
            _os.path.join(base_dir, "shared", "wiki", dept, "summaries"),
            _os.path.join(base_dir, "shared", "wiki", dept, "concepts"),
            _os.path.join(base_dir, "shared", "wiki", dept, "entities"),
        ])

    missing = [d for d in required if not _os.path.isdir(d)]
    existing = [d for d in required if _os.path.isdir(d)]

    return {
        "status": "ok" if not missing else "incomplete",
        "base_dir": base_dir,
        "departments": list(departments.keys()),
        "total_required": len(required),
        "existing": len(existing),
        "missing": len(missing),
        "missing_dirs": missing,
        "existing_dirs": existing,
    }

def boss_list_departments(password: str) -> dict:
    """老板查看所有部门及密码"""
    _check_password(password)
    return {"departments": dict(_config["departments"])}


def boss_add_department(password: str, name: str, dept_password: str,
                        patterns: list = None) -> dict:
    """
    动态添加/修改部门及密码。
    name: 部门名称，如"研发部"
    dept_password: 该部门的加密密码，员工搜索时使用
    patterns: 可选，该部门自定义的关键数据提取正则列表，
              格式 [(regex, key_name), ...]，不传则使用 DEFAULT_KEY_DATA_PATTERNS
    """
    _check_password(password)
    if not name or not dept_password:
        raise ValueError("部门名称和密码不能为空")
    _config["departments"][name] = dept_password
    if patterns:
        _config["dept_patterns"][name] = patterns
    # 确保存储目录存在
    secure_store = _import_secure_store()
    storage_dir = secure_store._get_storage_dir(
        base_dir=_config["base_dir"], encrypted_dir=_config.get("encrypted_dir"),
        department=name)
    store_file = secure_store._store_file_path(storage_dir)
    if not os.path.exists(store_file):
        # 预初始化空store文件
        secure_store.pw_append({"id": "init", "source_filename": "placeholder"},
                               dept_password, base_dir=_config["base_dir"],
                               department=name,
                               encrypted_dir=_config.get("encrypted_dir"))
    return {"status": "ok", "department": name, "patterns": "custom" if patterns else "default",
            "message": f"部门 [{name}] 已配置"}


def boss_remove_department(password: str, name: str) -> dict:
    """删除部门配置（不删除已加密的数据文件）"""
    _check_password(password)
    if name not in _config["departments"]:
        raise ValueError(f"部门不存在: {name}")
    del _config["departments"][name]
    return {"status": "ok", "department": name, "message": f"部门 [{name}] 已移除（加密文件保留在磁盘）"}


def _check_password(password: str):
    if not _config.get("boss_password"):
        raise RuntimeError("???????????? configure(boss_password=...)")
    if password != _config["boss_password"]:
        raise PermissionError("密码错误，无权限上传")


def _get_dept_password(department: str) -> str:
    """获取指定部门的加密密码"""
    pwd = _config["departments"].get(department)
    if not pwd:
        raise ValueError(f"未知部门: {department}，可用: {list_departments()}")
    return pwd


def _get_pending_dir():
    return os.path.join(_config["base_dir"], ".agent_data", "pending")


def _get_uploads_dir():
    return os.path.join(_config["base_dir"], ".agent_data", "uploads")


def _file_sha256(filepath: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _hash_manifest_path(department: str) -> str:
    base = _config.get("base_dir", "")
    if not base:
        return ""
    d = os.path.join(base, ".agent_data", "hashes")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{department}.json")


def _load_hash_manifest(department: str) -> dict:
    path = _hash_manifest_path(department)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _record_hash_manifest(department: str, content_hash: str, filename: str) -> None:
    path = _hash_manifest_path(department)
    if not path:
        return
    manifest = _load_hash_manifest(department)
    manifest[content_hash] = filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def _check_content_duplicate(filepath: str, department: str) -> dict | None:
    try:
        content_hash = _file_sha256(filepath)
        manifest = _load_hash_manifest(department)
        existing = manifest.get(content_hash)
        if existing:
            return {"content_hash": content_hash, "existing_filename": existing}
        return {"content_hash": content_hash, "existing_filename": None}
    except Exception:
        return None


def _import_secure_store():
    """动态导入secure-storage技能模块"""
    import sys
    # scripts → boss-upload → Skills (3 levels up)
    skills_base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    secure_path = os.path.join(skills_base, "secure-storage", "scripts")
    if secure_path not in sys.path:
        sys.path.insert(0, secure_path)
    import secure_store
    return secure_store


def _index_docling_chunks(original_path: str, filename: str, department: str,
                           summary: str, tags: list, entities: list,
                           key_data: dict, chunks: list = None) -> dict:
    """
    将 Docling heading-chunk 逐个写入加密的 .md 文件，
    让 HybridChunker 识别 ## 标题边界（不再字符切断）。

    每个 chunk → 加密 .md 文件 → 独立索引 → 检索时可独立召回。
    磁盘上的 .md 文件是密文，只有知识库进程能解密读取。
    """
    if chunks is None:
        # ── 未传入 chunks 时才重新解析（兼容直接调用，正常流程由 step1 缓存传入）──
        dl = _import_docling()
        parse_result = dl.dl_parse_document(original_path)
        if parse_result.get("status") != "success":
            return {"status": "error", "message": parse_result.get("error", "解析失败"), "chunks_indexed": 0}
        chunks = parse_result["chunks"]
    if not chunks:
        return {"status": "error", "message": "无有效章节", "chunks_indexed": 0}

    kr = _import_knowledge_rag()
    encrypted_dir = _config.get("encrypted_dir")
    if encrypted_dir:
        kr.set_documents_dir(encrypted_dir)

    # 获取部门密码（用于加密/解密 chunk 文件）
    dept_password = _get_dept_password(department)
    if not dept_password:
        return {"status": "error", "message": f"??[{department}]?????", "chunks_indexed": 0}
    if encrypted_dir:
        dept_dir = Path(encrypted_dir) / "rag_docs" / department
    else:
        dept_dir = Path(_config["base_dir"]) / "rag_docs" / department
    dept_dir.mkdir(parents=True, exist_ok=True)

    safe_stem = "".join(c if c.isalnum() or c in ("_", "-", ".") else "_" for c in filename)
    safe_stem = Path(safe_stem).stem

    orch = kr._get_orchestrator()

    chunks_indexed = 0
    chunk_ids = []
    conclusion_chunk_id = ""
    for i, chunk in enumerate(chunks):
        heading = chunk.get("heading", "").strip()
        content = _sanitize_visible_text(chunk.get("content", "")).strip()
        if not content:
            continue

        lines = [
            f"# {filename}",
            f"部门：{department}",
            f"摘要：{_sanitize_visible_text(summary)}",
        ]
        if tags:
            lines.append(f"标签：{', '.join(tags)}")
        if entities:
            lines.append(f"实体：{', '.join(entities)}")
        if key_data:
            safe_key_data = {
                _sanitize_visible_text(str(k)): _sanitize_visible_text(str(v))
                for k, v in key_data.items()
            }
            lines.append(f"关键数据：{json.dumps(safe_key_data, ensure_ascii=False)}")
        lines.append("")
        lines.append(f"## {heading}")
        lines.append(content)

        md_content = "\n".join(lines)

        chunk_uuid = uuid.uuid4().hex[:6]
        md_filename = f"{safe_stem}_chunk{i+1:02d}_{chunk_uuid}.md"
        relative_path = f"{department}/{md_filename}"

        # ① 写入 LanceDB 索引（全文存储，无需磁盘副本）
        actual_file_path = dept_dir / md_filename
        try:
            indexed = orch.add_document_from_content(md_content, relative_path, department)
            chunk_id = indexed.get("doc_id", "") if isinstance(indexed, dict) else ""
            if chunk_id:
                chunk_ids.append(chunk_id)
            chunks_indexed += 1
            heading_lower = heading.lower()
            if not conclusion_chunk_id and any(
                k in heading_lower for k in ("conclusion", "recommendation", "summary", "结论", "建议", "总结", "小结")
            ):
                conclusion_chunk_id = chunk_id
        except Exception:
            continue

        # ② 安全清理：不保留明文/密文磁盘副本，LanceDB 已持久化
        # 业务需要时通过 store_部门.enc + kr_decrypt_chunk 获取全文

    return {
        "status": "success",
        "chunks_indexed": chunks_indexed,
        "encrypted": True,
        "chunk_ids": chunk_ids,
        "conclusion_chunk_id": conclusion_chunk_id,
    }


def _import_knowledge_rag():
    """????knowledge-rag????????LanceDB????????"""
    import sys
    if __file__:
        skills_base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    else:
        skills_base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    kr_path = os.path.join(skills_base, "knowledge-rag", "scripts")
    if kr_path not in sys.path:
        sys.path.insert(0, kr_path)

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


# 默认关键数据提取正则；通用 profile 由 scan-worker 本地提取承担。
DEFAULT_KEY_DATA_PATTERNS = []
def _extract_key_data_from_chunks(chunks: list, department: str = "") -> dict:
    """? Docling chunks ????????????????? AI ???"""
    # ???? scan_worker ????????
    try:
        from scan_worker import _extract_key_data_locally
        return _extract_key_data_locally(chunks)
    except ImportError:
        pass

    # ?????????
    import re
    metrics = {}
    for chunk in chunks:
        tables = chunk.get("tables", [])
        if not tables:
            continue
        table_groups = []
        current = []
        for line in tables:
            if line.strip().startswith("|"):
                current.append(line)
            else:
                if current:
                    table_groups.append(current)
                    current = []
        if current:
            table_groups.append(current)
        
        heading = chunk.get("heading", "")
        for tbl in table_groups:
            if len(tbl) < 2:
                continue
            header_cells = [c.strip() for c in tbl[0].split("|") if c.strip()]
            for row_line in tbl[1:]:
                cells = [c.strip() for c in row_line.split("|") if c.strip()]
                if len(cells) < 2:
                    continue
                metric_base = cells[0]
                if not metric_base or len(metric_base) > 30:
                    continue
                if re.match(r'^\d{4}[\./-]|\d+$', metric_base):
                    continue
                for j in range(1, min(len(cells), len(header_cells))):
                    val = cells[j]
                    if not val or val in ("/", "-", "—", "未检出"):
                        continue
                    if not re.search(r'\d', val):
                        continue
                    col = header_cells[j] if j < len(header_cells) else f"col{j}"
                    key = f"{metric_base}-{col}" if len(header_cells) > 2 else metric_base
                    if heading and heading not in key:
                        key = f"{heading}-{key}"
                    if key not in metrics:
                        metrics[key] = val
    return metrics

def _import_docling():
    """????docling????"""
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
        cells = [_sanitize_visible_text(str(c).strip()) if c else "" for c in row]
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
    if ext in _BLOCKED_UPLOAD_EXTENSIONS:
        raise ValueError(f"拒绝上传疑似加密/索引文件: {os.path.basename(filepath)}")
    if ext not in _SUPPORTED_UPLOAD_EXTENSIONS:
        raise ValueError(f"不支持的文件类型: {ext or '无扩展名'}，请上传 PDF/DOCX/TXT/MD/CSV")
    if ext == ".pdf":
        extracted = extract_pdf(filepath)
        if len(extracted.get("text", "").strip()) < 300 and os.environ.get("MINERU_API_TOKEN"):
            try:
                skills_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                ocr_path = os.path.join(skills_root, "ocr")
                if ocr_path not in sys.path:
                    sys.path.insert(0, ocr_path)
                import mineru_ocr
                ocr_result = mineru_ocr.ocr_pdf_if_needed(filepath, min_text_chars=300)
                if ocr_result.get("status") == "success":
                    extracted["text"] = ocr_result["markdown"] + "\n" + extracted.get("text", "")
                    extracted["ocr"] = "mineru"
            except Exception:
                pass
    elif ext == ".docx":
        extracted = extract_docx(filepath)
    else:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            extracted = {"text": f.read(), "tables": []}
    extracted["text"] = _sanitize_visible_text(extracted.get("text", ""))
    return extracted


def boss_upload_step1(file_path: str, password: str = "", department: str = None) -> dict:
    """
    ???????????? ? ?pending JSON ? ??AI??
    department: ??????"???"/"???"
    password: boss???configure??boss_password?????
    """
    if _config.get("boss_password", ""):
        if password != _config["boss_password"]:
            raise PermissionError("??????????")
    _auto_load_config()
    if not _config["base_dir"]:
        raise RuntimeError("请先调用 configure() 配置 base_dir")
    if not department:
        raise ValueError("请指定部门，可用: " + str(list_departments()))
    if department not in _config["departments"]:
        raise ValueError(f"未知部门: {department}，可用: {list_departments()}")

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    filename = os.path.basename(file_path)
    dup = _check_content_duplicate(file_path, department)
    if dup and dup.get("existing_filename"):
        return {
            "status": "skipped",
            "message": f"内容重复，已存在同名内容: {dup['existing_filename']}，跳过上传",
            "filename": filename,
            "department": department,
            "content_hash": dup.get("content_hash", ""),
        }
    content_hash = (dup or {}).get("content_hash", "")
    extracted = extract_file(file_path)

    uploads_dir = _get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)
    tmp_path = os.path.join(uploads_dir, filename)
    shutil.copy2(file_path, tmp_path)

    pending_dir = _get_pending_dir()
    os.makedirs(pending_dir, exist_ok=True)
    pending_id = str(uuid.uuid4())[:8]
    try:
        scripts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import task_status
        task_status.update_task_status(_config["base_dir"], pending_id, "uploaded", filename=filename, department=department, content_hash=content_hash)
    except Exception:
        pass

    # 归档原文件到原文件目录（按部门分类：原文件目录/部门/文件名）
    originals_dir = _config.get("originals_dir", "")
    archive_target = None
    if originals_dir and os.path.isdir(originals_dir):
        dept_archive = os.path.join(originals_dir, department)
        os.makedirs(dept_archive, exist_ok=True)
        archive_target = os.path.join(dept_archive, filename)
        if os.path.exists(archive_target):
            return {
                "status": "skipped",
                "message": f"文件已存在: {archive_target}，跳过上传",
                "archive_path": archive_target,
                "filename": filename,
                "department": department,
            }
        shutil.copy2(file_path, archive_target)

    # 写meta文件（供step2使用，scan_worker不会动它）
    meta_path = os.path.join(pending_dir, f"meta_{pending_id}.json")
    meta = {
        "pending_id": pending_id,
        "filename": filename,
        "department": department,
        "original_path": file_path,
        "tmp_path": tmp_path,
        "archive_path": archive_target,
        "content_hash": content_hash,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    # 写pending JSON（供scan_worker抢任务）
    # 新流程：同时用 Docling 提取 heading-chunks，scan-worker 用 chunks 发 AI（省 token）
    pending_chunks = []
    pending_conclusion = ""
    pending_key_data = {}
    try:
        dl = _import_docling()
        parse = dl.dl_parse_document(file_path)
        if parse.get("status") == "success":
            pending_chunks = parse.get("chunks", [])
            pending_conclusion = parse.get("conclusion", "")
            pending_key_data = _extract_key_data_from_chunks(pending_chunks, department=department)
    except Exception:
        pass  # Docling 不可用时降级用旧流程

    pending_data = {
        "pending_id": pending_id,
        "filename": filename,
        "department": department,
        "text": extracted.get("text", ""),
        "tables": extracted.get("tables", []),
        "tmp_path": tmp_path,
        "original_path": file_path,
        "content_hash": content_hash,
        "status": "pending_ai_scan",
        # 新增：Docling chunks（scan-worker 优先用这个发 AI，省 token）
        "_docling_chunks": pending_chunks,
        "_docling_conclusion": pending_conclusion,
        "_docling_key_data": pending_key_data,
        "_docling_markdown": parse.get("markdown", ""),
    }
    pending_path = os.path.join(pending_dir, f"pending_{pending_id}.json")
    with open(pending_path, "w", encoding="utf-8") as f:
        json.dump(pending_data, f, ensure_ascii=False)

    return {
        "status": "pending_ai_scan",
        "pending_id": pending_id,
        "department": department,
        "filename": filename,
        "archive_path": archive_target,
        "message": f"文件已提取（{department}），原文件{'已归档' if archive_target else '未归档'}，等待AI扫描",
    }


def _get_lock_path(pending_id: str) -> str:
    """锁文件路径"""
    return os.path.join(_get_pending_dir(), f"step2_lock_{pending_id}")


def _acquire_lock(pending_id: str, max_age_seconds: int = 3600) -> bool:
    """
    尝试获取事务锁。
    如果锁文件已存在且未超时，返回 False（另一进程正在执行）。
    如果锁文件存在但已超时，视为崩溃遗留，删除后创建新锁。
    """
    lock_path = _get_lock_path(pending_id)
    if os.path.exists(lock_path):
        try:
            mtime = os.path.getmtime(lock_path)
            import time
            if time.time() - mtime < max_age_seconds:
                return False  # 锁有效，不抢占
            os.remove(lock_path)  # 已超时，当作崩溃遗留清理
        except Exception:
            try:
                os.remove(lock_path)
            except Exception:
                pass
    try:
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(f"{pending_id}|{datetime.now().isoformat()}")
        return True
    except Exception:
        return False


def _release_lock(pending_id: str):
    """释放事务锁（无论成功失败都调用）"""
    lock_path = _get_lock_path(pending_id)
    if os.path.exists(lock_path):
        try:
            os.remove(lock_path)
        except Exception:
            pass


def boss_upload_step2(pending_id: str) -> dict:
    """
    上传流程第二步（agent完成AI扫描后调用）：
    读取structured结果 → 加密存储 → 索引 → 清理临时文件
    原文件归档在step1已完成，此处只销毁临时副本

    事务性保证：
    - 有锁文件则不重复执行（防止并发）
    - 锁文件超24小时未释放视为崩溃遗留，下次调用自动清理重试
    """
    pending_dir = _get_pending_dir()
    structured_dir_b = os.path.join(_config['base_dir'], '.agent_data', 'structured')
    structured_path = os.path.join(structured_dir_b, f"structured_{pending_id}.json")

    # ── 前置检查：锁 + structured文件 ────────────────────────────────────────
    if not os.path.exists(structured_path):
        return {"status": "error", "message": f"AI扫描结果未找到: {structured_path}"}

    # 尝试获取锁（锁存在且未超时则拒绝执行，防止并发）
    if not _acquire_lock(pending_id):
        return {
            "status": "error",
            "message": f"该任务正在另一进程中执行，或上次执行中途崩溃未释放锁。请稍后重试。",
            "pending_id": pending_id,
        }

    # ── 主流程（包裹在try中，finally确保锁释放） ──────────────────────────────
    try:
        with open(structured_path, "r", encoding="utf-8") as f:
            structured = json.load(f)

        meta_path = os.path.join(pending_dir, f"meta_{pending_id}.json")
        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

        try:
            scripts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import archon_validation
            gate = archon_validation.validate_finalize_payload(structured)
            if not gate["valid"] and os.environ.get("ARCHON_STRICT_FINALIZE", "1") != "0":
                dead_dir = os.path.join(_config["base_dir"], ".agent_data", "dead_letter")
                os.makedirs(dead_dir, exist_ok=True)
                with open(os.path.join(dead_dir, f"dead_{pending_id}.json"), "w", encoding="utf-8") as f:
                    json.dump({"pending_id": pending_id, "structured": structured, "validation": gate}, f, ensure_ascii=False, indent=2)
                return {
                    "status": "rejected",
                    "pending_id": pending_id,
                    "message": f"finalize validation failed: {gate['errors']}",
                    "validation": gate,
                }
        except ImportError:
            pass

        try:
            scripts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import task_status
            task_status.update_task_status(_config["base_dir"], pending_id, "finalizing", filename=structured.get("filename", ""), department=structured.get("department", ""))
        except Exception:
            pass

        secure_store = _import_secure_store()

        # 从meta文件读取部门（meta不会被scan_worker删除）
        department = structured.get("department", "")
        if not department and os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
                department = meta.get("department", "")
        if not department:
            department = "未分类"

        dept_password = _get_dept_password(department)
        filename = structured.get("filename", "unknown")
        kr_result = {}
        conflict_info = None

        # ── 幂等检查：是否已加密入库（防重复执行） ─────────────────────────────
        try:
            existing = secure_store.pw_decrypt_store(
                dept_password,
                base_dir=_config["base_dir"],
                department=department,
                encrypted_dir=_config.get("encrypted_dir"),
            )
            today_prefix = datetime.now().strftime("%Y-%m-%d")
            already_stored = any(
                r.get("source_filename") == filename and
                str(r.get("upload_time", "")).startswith(today_prefix)
                for r in existing
            )
            if already_stored:
                # 已经入库，跳过加密存储，但继续完成索引和结论提取
                kr = _import_knowledge_rag()
                kr.set_documents_dir(_config.get("encrypted_dir"))
                kr_status = "skipped（已索引）"
                kr_chunks = 0
                dl_status = "skipped"
                dl_conclusion_chunks = 0
                csv_status = "skipped（已入库）"
                id_from_store = next((r.get("id", "") for r in existing
                                     if r.get("source_filename") == filename), "")
                record_id = id_from_store or str(uuid.uuid4())
            else:
                already_stored = False
                raise Exception("not stored")  # 触发下面的存储逻辑
        except Exception:
            already_stored = False
            # ── 复用 scan_worker 已切好的片段，不再重复切分 ──
            docling_chunks = structured.get("_docling_chunks", [])
            pre_fragments = structured.get("fragments", [])
            pre_fragmented_text = structured.get("full_text", "")

            if docling_chunks:
                # 新流程：从 Docling chunks 重建片段，每个 chunk 的 heading+content 就是一个片段
                fragments = []
                for ch in docling_chunks:
                    h = ch.get("heading", "").strip()
                    c = _sanitize_visible_text(ch.get("content", "")).strip()
                    part = f"{h}: {c}" if h else c
                    if part:
                        fragments.append(part)
                full_text = "\n---\n".join(fragments)
            elif pre_fragments:
                # 旧流程：直接复用 scan_worker 已切好的片段
                fragments = [_sanitize_visible_text(p) for p in pre_fragments if _sanitize_visible_text(p)]
                full_text = _sanitize_visible_text(pre_fragmented_text) if pre_fragmented_text else "\n---\n".join(fragments)
            else:
                # 兜底：structured 里没有任何片段信息，才自己做切分
                fragments = _split_into_fragments(_sanitize_visible_text(structured.get("full_text", "")))
                full_text = "\n---\n".join(fragments)

            # ── 兜底：如果 structured 没有 full_text（scan_worker 新行为），从 processing 文件读取 ──
            processing_path = os.path.join(pending_dir, f"processing_{pending_id}.json")
            if (not full_text or full_text.isspace()) and os.path.exists(processing_path):
                with open(processing_path, "r", encoding="utf-8") as pf:
                    proc = json.load(pf)
                full_text = _sanitize_visible_text(proc.get("text", ""))
                tables_from_proc = proc.get("tables", [])
                if tables_from_proc and not structured.get("tables"):
                    structured["tables"] = tables_from_proc
                if not fragments:
                    fragments = _split_into_fragments(full_text)
                    full_text = "\n---\n".join(fragments)

            safe_key_data = {
                _sanitize_visible_text(str(k)): _sanitize_visible_text(str(v))
                for k, v in (structured.get("key_data", {}) or {}).items()
            }
            record = {
                "id": str(uuid.uuid4()),
                "source_filename": filename,
                "department": department,
                "upload_time": datetime.now().isoformat(),
                "content_hash": meta.get("content_hash", ""),
                "tags": [_sanitize_visible_text(str(t)) for t in structured.get("tags", [])],
                "summary": _sanitize_visible_text(structured.get("summary", "")),
                "entities": [_sanitize_visible_text(str(e)) for e in structured.get("entities", [])],
                "key_data": safe_key_data,
                "client_name": _sanitize_visible_text(structured.get("client_name", "")),
                "project_name": _sanitize_visible_text(structured.get("project_name", "")),
                "product_capacity": _sanitize_visible_text(structured.get("product_capacity", "")),
                "quality_summary": _sanitize_visible_text(structured.get("quality_summary", "")),
                "objective": _sanitize_visible_text(structured.get("objective", "")),
                "process": _sanitize_visible_text(structured.get("process", "")),
                "full_text": full_text,
                "fragments": fragments,
                "tables": structured.get("tables", []),
            }
            record_id = record["id"]
            secure_store.pw_add_record(record, dept_password, base_dir=_config["base_dir"],
                                       department=department,
                                       encrypted_dir=_config.get("encrypted_dir"))

            # ── 关键指标写入部门汇总CSV（供跨文档对比） ─────────────────────────
            csv_status = "no_key_data"
            if structured.get("key_data"):
                try:
                    csv_result = _append_key_metrics_to_csv(
                        department=department,
                        filename=filename,
                        key_data=structured.get("key_data", {}),
                        summary=structured.get("summary", ""),
                    )
                    csv_status = csv_result.get("status", "ok")
                except Exception as e:
                    csv_status = f"error: {e}"
            # ── 冲突检测 ─────────────────────────────────────────────────────────
            conflict_result = {"status": "no_check", "conflicts": []}
            if structured.get("key_data"):
                try:
                    conflict_result = check_conflicts(
                        department=department,
                        filename=filename,
                        key_data=structured.get("key_data", {}),
                    )
                except Exception as e:
                    conflict_result = {"status": f"error: {e}", "conflicts": []}

            # ── 冲突信息整理 ────────────────────────────────────────────────────
            conflict_info = None
            if conflict_result.get("status") == "conflicts_found":
                conflict_info = {
                    "status": "conflicts_found",
                    "conflicts": conflict_result["conflicts"],
                    "project": conflict_result.get("project", ""),
                    "csv_written": True,
                    "message": conflict_result["message"],
                }

            # ── 索引到知识库（按 Docling 标题边界，不字符切断） ──────────────────
            kr_status = "unknown"
            kr_chunks = 0
            original_path = None
            kr_result = {}
            try:
                if os.path.exists(meta_path):
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        original_path = meta.get("original_path", "")

                                # LanceDB indexing: chunks from structured JSON (no original file needed)
                docling_chunks = structured.get("_docling_chunks", [])
                if docling_chunks:
                    kr_result = _index_docling_chunks(
                        original_path=original_path or "",
                        filename=filename,
                        department=department,
                        summary=structured.get("summary", ""),
                        tags=structured.get("tags", []),
                        chunks=docling_chunks,
                        entities=structured.get("entities", []),
                        key_data=structured.get("key_data", {}),
                    )
                    kr_status = kr_result.get("status", "unknown")
                    kr_chunks = kr_result.get("chunks_indexed", 0)
                else:
                    # No docling chunks: build indexable content from available structured fields
                    try:
                        content_parts = []
                        if structured.get("summary"):
                            content_parts.append(f"Summary: {structured['summary']}")
                        if structured.get("client_name"):
                            content_parts.append(f"Client: {structured['client_name']}")
                        if structured.get("project_name"):
                            content_parts.append(f"Project: {structured['project_name']}")
                        if structured.get("process"):
                            content_parts.append(f"Process: {structured['process']}")
                        if structured.get("objective"):
                            content_parts.append(f"Objective: {structured['objective']}")
                        if structured.get("quality_summary"):
                            content_parts.append(f"Quality Summary: {structured['quality_summary']}")
                        if structured.get("tags"):
                            content_parts.append(f"Tags: {', '.join(structured['tags'])}")
                        if structured.get("key_metrics"):
                            content_parts.append(f"Key Metrics: {json.dumps(structured['key_metrics'], ensure_ascii=False)}")
                        if structured.get("key_data"):
                            content_parts.append(f"Key Data: {json.dumps(structured['key_data'], ensure_ascii=False)}")
                        
                        index_content = " | ".join(content_parts) if content_parts else f"Document: {filename}"
                        
                        # Index into LanceDB
                        kr.kr_add_document(
                            content=index_content,
                            filename=filename,
                            department=department,
                            summary=structured.get("summary", ""),
                            tags=structured.get("tags", []),
                            entities=structured.get("entities", []),
                            key_data=structured.get("key_data", structured.get("key_metrics", {})),
                        )
                        kr_status = "indexed_from_fields"
                        kr_chunks = 1
                    except Exception:
                        kr_status = "no_chunks"
                        kr_chunks = 0
            except Exception:
                kr_status = "index_error"
                kr_chunks = 0
            dl_status = "in_chunks"
            dl_conclusion_chunks = 0

        # ?? ?????? ──────────────────────────────────────────────────────
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            tmp_path = meta.get("tmp_path", "")
            if tmp_path and os.path.exists(tmp_path):
                secure_store.delete_original_file(tmp_path)

        # ── 写入7字段文档元数据索引（架构设计：结构化检索层） ────────────────
        try:
            sys.path.insert(0, os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "knowledge-rag", "scripts"))
            from index_generator import store_doc_meta

            # 从structured中提取7字段（AI扫描可能已包含，否则从现有字段构造）
            doc_meta = {
                "doc_id": record_id,
                "filename": filename,
                "client_name": _sanitize_visible_text(structured.get("client_name", "")),
                "project_name": _sanitize_visible_text(structured.get("project_name", "")),
                "product_capacity": _sanitize_visible_text(structured.get("product_capacity", "")),
                "quality_summary": _sanitize_visible_text(structured.get("quality_summary", "")),
                "objective": _sanitize_visible_text(structured.get("objective", "")),
                "process": _sanitize_visible_text(structured.get("process", "")),
                "upload_time": datetime.now().isoformat(),
            }
            # 从 key_data 自动补充质量/规格字段
            if not doc_meta["quality_summary"] and structured.get("key_data"):
                kd = structured["key_data"]
                quality_parts = []
                for key, val in list(kd.items())[:5]:
                    if val:
                        quality_parts.append(f"{key} {val}")
                if quality_parts:
                    doc_meta["quality_summary"] = ", ".join(quality_parts)

            # 从 tags 补充方法/流程字段（AI 未提取时的兜底）
            if not doc_meta["process"]:
                tags = structured.get("tags", [])
                if tags:
                    doc_meta["process"] = ", ".join(tags[:5])

            # 找结论chunk id
            conclusion_id = kr_result.get("conclusion_chunk_id", "")
            if not conclusion_id:
                chunk_ids = kr_result.get("chunk_ids", [])
                if chunk_ids:
                    conclusion_id = chunk_ids[0]
            doc_meta["conclusion_chunk_id"] = conclusion_id

            meta_result = store_doc_meta(doc_meta, department)
            doc_meta_status = meta_result.get("status", "unknown")
        except Exception as e:
            doc_meta_status = f"error: {e}"

        # ── 内容哈希归档 + 自动增量 Wiki ─────────────────────────────────────
        if meta.get("content_hash"):
            try:
                _record_hash_manifest(department, meta["content_hash"], filename)
            except Exception:
                pass
        wiki_status = "skipped"
        try:
            scripts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import wiki_compiler
            wiki_status = wiki_compiler.compile_auto_wiki(_config["base_dir"], department, structured, record_id).get("status", "error")
        except Exception:
            wiki_status = "error"
        try:
            scripts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import task_status
            task_status.update_task_status(
                _config["base_dir"],
                pending_id,
                "success" if not already_stored else "skipped",
                filename=filename,
                department=department,
                file_id=record_id,
                wiki_status=wiki_status,
            )
        except Exception:
            pass

        # ── 清理pending/processing/meta文件（保留structured供wiki读取） ────────
        for prefix in ["pending", "processing", "meta"]:
            p = os.path.join(pending_dir, f"{prefix}_{pending_id}.json")
            if os.path.exists(p):
                os.remove(p)

        return {
            "status": "success",
            "file_id": record_id,
            "filename": filename,
            "department": department,
            "tags": structured.get("tags", []),
            "knowledge_rag": {"status": kr_status, "chunks_indexed": kr_chunks},
            "docling": {"status": dl_status, "conclusion_chunks": dl_conclusion_chunks},
            "metrics_csv": {"status": csv_status},
            "conflicts": conflict_info,
            "doc_meta_index": {"status": doc_meta_status},
            "wiki_auto": {"status": wiki_status},
            "already_indexed": already_stored,
            "message": f"上传完成（{'跳过（已入库）' if already_stored else '新建'}），已索引到知识库（{kr_chunks} chunks），结论（{dl_status}），关键数据（{csv_status}）"
                    + (f"\n?? 检测到冲突指标 {len(conflict_info.get('conflicts', []))} 个，请调用 resolve_conflict(department=\"{department}\", filename=\"{filename}\", resolution=\"keep_old|overwrite|keep_both\")"
                       if conflict_info else ""),
        }

    except Exception as e:
        return {
            "status": "error",
            "message": f"step2执行出错: {str(e)}",
            "pending_id": pending_id,
        }

    finally:
        # 无论成功还是失败，必须释放锁
        _release_lock(pending_id)


def boss_upload_auto_finalize(password: str = "") -> dict:
    """
    ??????????????????????????
    ?????structured_{id}.json ?? + pending_{id}.json ??? + processing_{id}.json ???
    ??? scan-worker ??????????????

    ??: {"status": "all_done"|"partial"|"nothing", "processed": n, "skipped": [...], "errors": [...]}
    """
    if _config.get("boss_password", ""):
        if password != _config["boss_password"]:
            raise PermissionError("??????????")
    _auto_load_config()
    pending_dir = _get_pending_dir()
    if not os.path.exists(pending_dir):
        return {"status": "nothing", "processed": 0, "message": "pending目录不存在"}

    # ── 第一步：清理崩溃遗留的锁文件（超过24小时的） ────────────────────────
    stale_locks = []
    try:
        import time
        for f in os.listdir(pending_dir):
            if f.startswith("step2_lock_"):
                lock_path = os.path.join(pending_dir, f)
                try:
                    if time.time() - os.path.getmtime(lock_path) > 86400:
                        os.remove(lock_path)
                        stale_locks.append(f)
                except Exception:
                    pass
    except Exception:
        pass

    secure_store = _import_secure_store()
    processed = []
    skipped = []
    errors = []

    for f in sorted(os.listdir(_get_structured_dir())):
        if not f.startswith("structured_") or not f.endswith(".json"):
            continue
        pending_id = f[len("structured_"):-len(".json")]

        try:
            scripts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            import task_status
            if task_status.get_task_status(_config["base_dir"], pending_id).get("status") in ("success", "skipped"):
                skipped.append({"pending_id": pending_id, "reason": "already_finalized"})
                continue
        except Exception:
            pass

        # 检查是否还有未完成的 pending/processing
        pending_path = os.path.join(pending_dir, f"pending_{pending_id}.json")
        processing_path = os.path.join(pending_dir, f"processing_{pending_id}.json")
        if os.path.exists(pending_path):
            skipped.append({"pending_id": pending_id, "reason": "扫描未完成（pending仍存在）"})
            continue

        # 执行加密入库
        try:
            result = boss_upload_step2(pending_id)
            if result.get("status") == "success":
                processed.append({"pending_id": pending_id, "filename": result.get("filename"),
                                  "file_id": result.get("file_id")})
            else:
                errors.append({"pending_id": pending_id, "error": result.get("message", "未知错误")})
        except Exception as e:
            errors.append({"pending_id": pending_id, "error": str(e)})

    total = len(processed) + len(skipped) + len(errors)
    if total == 0:
        status = "nothing"
    elif len(errors) > 0:
        status = "partial"
    elif len(skipped) > 0:
        status = "partial"
    else:
        status = "all_done"

    return {
        "status": status,
        "processed": len(processed),
        "skipped": len(skipped),
        "errors": len(errors),
        "stale_locks_cleaned": len(stale_locks),
        "processed_list": processed,
        "skipped_list": skipped,
        "errors_list": errors,
        "message": f"处理完成: {len(processed)}/{total}（跳过{len(skipped)}，失败{len(errors)}，清理崩溃锁{len(stale_locks)}个）",
    }



def boss_compare_documents(department: str, metric_filter: str = None) -> dict:
    """Compare metric values across department documents.

    Args:
        department: Department name.
        metric_filter: Optional substring filter for metric names.

    Returns:
        Comparison rows with values per document and numeric diffs where
        both values can be parsed as numbers.
    """
    import re
    
    csv_path = _get_metrics_csv_path(department)
    json_path = csv_path.replace(".csv", ".json") if csv_path else None
    
    if not json_path or not os.path.exists(json_path):
        return {"status": "error", "message": f"?? [{department}] ????????????"}
    
    with open(json_path, "r", encoding="utf-8") as f:
        records = json.load(f)
    
    if len(records) < 2:
        return {
            "status": "ok",
            "department": department,
            "document_count": len(records),
            "message": f"?? {len(records)} ???????? 2 ?????",
            "documents": [r.get("filename", "") for r in records],
        }
    
    # ??????
    meta_cols = {"department", "filename", "upload_time", "summary"}
    
    # ????????
    doc_metrics = {}
    for rec in records:
        fname = rec.get("filename", "unknown")
        doc_metrics[fname] = {k: v for k, v in rec.items() if k not in meta_cols}
    
    doc_names = list(doc_metrics.keys())
    
    # ???????
    all_metric_names = set()
    for metrics in doc_metrics.values():
        all_metric_names.update(metrics.keys())
    
    # ??
    if metric_filter:
        all_metric_names = {m for m in all_metric_names if metric_filter.lower() in m.lower()}
    
    # ??????? ? ??? ? ????????????
    # ?? "3.2.1 ????-COD???(...)" ? "??????-COD???" ? core ?? "COD???"
    import re as _re
    
    def _metric_core(name: str) -> str:
        """Normalize a metric name to its core for cross-document matching."""
        base = _re.sub(r'\([^)]*\)$', '', name)
        if '-' in base:
            base = base.rsplit('-', 1)[-1]
        return base.strip()
    
    # ??????
    core_index = {}  # core_name -> { doc_name -> (full_metric, value) }
    for dn in doc_names:
        for metric, value in doc_metrics[dn].items():
            core = _metric_core(metric)
            if core not in core_index:
                core_index[core] = {}
            if dn not in core_index[core]:
                core_index[core][dn] = (metric, value)
    
    # ??
    comparisons = []
    only_in = {dn: [] for dn in doc_names}
    seen_cores = set()
    
    for metric in sorted(all_metric_names):
        core = _metric_core(metric)
        if core in seen_cores:
            continue
        seen_cores.add(core)
        
        values = {}
        for dn in doc_names:
            if dn in core_index.get(core, {}):
                full_metric, value = core_index[core][dn]
                values[dn] = value
            else:
                only_in[dn].append(metric)
        
        if len(values) >= 2:
            # ??????
            diff_text = ""
            try:
                nums = {}
                for dn, v in values.items():
                    clean = re.sub(r'[,\s]', '', str(v))
                    m = re.match(r'([\d.]+)', clean)
                    if m:
                        nums[dn] = float(m.group(1))
                if len(nums) >= 2:
                    dn_list = list(nums.keys())
                    v1, v2 = nums[dn_list[0]], nums[dn_list[1]]
                    if v1 != 0:
                        pct = (v2 - v1) / v1 * 100
                        direction = "?" if pct > 0 else "?"
                        diff_text = f"{dn_list[1]} ? {dn_list[0]} {direction} {abs(pct):.1f}%"
            except Exception:
                pass
            
            comparisons.append({
                "metric": metric,
                "values": values,
                "diff": diff_text if diff_text else None,
            })
    
    # ?????????????
    shared_only_in = {dn: [m for m in metrics if m in all_metric_names] 
                      for dn, metrics in only_in.items()}
    shared_only_in = {dn: ms for dn, ms in shared_only_in.items() if ms}
    
    return {
        "status": "success",
        "department": department,
        "documents": doc_names,
        "total_metrics": len(all_metric_names),
        "compared_metrics": len(comparisons),
        "comparisons": comparisons[:50],  # ???? 50 ?
        "only_in_document": shared_only_in,
    }


def boss_search_metric(department: str, keyword: str) -> dict:
    """Search metric columns in the department metrics store."""
    csv_path = _get_metrics_csv_path(department)
    json_path = csv_path.replace(".csv", ".json") if csv_path else None
    
    if not json_path or not os.path.exists(json_path):
        return {"status": "error", "message": f"?? [{department}] ?????"}
    
    with open(json_path, "r", encoding="utf-8") as f:
        records = json.load(f)
    
    results = []
    for rec in records:
        fname = rec.get("filename", "")
        meta_cols = {"department", "filename", "upload_time", "summary"}
        for k, v in rec.items():
            if k in meta_cols:
                continue
            if keyword.lower() in k.lower():
                results.append({
                    "filename": fname,
                    "metric": k,
                    "value": v,
                })
    
    return {
        "status": "success",
        "keyword": keyword,
        "department": department,
        "match_count": len(results),
        "results": results[:30],
    }

def boss_get_pending_status() -> dict:
    """
    查看扫描进度：pending（等待扫描）、processing（扫描中）、structured（已完成）
    """
    pending_dir = _get_pending_dir()
    pending = 0
    processing = 0
    done = 0
    if os.path.isdir(pending_dir):
        for f in os.listdir(pending_dir):
            if f.startswith("pending_") and f.endswith(".json"):
                pending += 1
            elif f.startswith("processing_") and f.endswith(".json"):
                processing += 1
    structured_dir = _get_structured_dir()
    if os.path.isdir(structured_dir):
        done = len([
            f for f in os.listdir(structured_dir)
            if f.startswith("structured_") and f.endswith(".json")
        ])

    return {
        "pending": pending,
        "processing": processing,
        "done": done,
        "total": pending + processing + done,
        "message": f"等待扫描: {pending} | 扫描中: {processing} | 已完成: {done}",
    }


def list_pending_scans() -> list:
    """列出所有等待AI扫描的pending文件"""
    pending_dir = _get_pending_dir()
    if not os.path.exists(pending_dir):
        return []
    files = []
    for f in os.listdir(pending_dir):
        if f.startswith("pending_") and f.endswith(".json"):
            files.append(os.path.join(pending_dir, f))
    return files


def read_pending_file(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def write_structured_result(pending_id: str, structured: dict) -> str:
    """agent完成AI扫描后，将结构化结果写入JSON文件。
    
    自动从 pending 文件合并 _docling_chunks 和 full_text（如果 structured 中缺失），
    确保 boss_upload_step2 能正常写入 LanceDB。
    """
    pending_dir = _get_pending_dir()
    os.makedirs(pending_dir, exist_ok=True)
    
    # Merge chunk data from pending file if missing in structured
    if "_docling_chunks" not in structured or "full_text" not in structured:
        pending_path = os.path.join(pending_dir, f"pending_{pending_id}.json")
        if os.path.exists(pending_path):
            try:
                with open(pending_path, "r", encoding="utf-8") as pf:
                    pending_data = json.load(pf)
                if "_docling_chunks" not in structured:
                    structured["_docling_chunks"] = pending_data.get("_docling_chunks", [])
                if "full_text" not in structured:
                    structured["full_text"] = pending_data.get("full_text", pending_data.get("text", ""))
                if "fragments" not in structured:
                    structured["fragments"] = pending_data.get("fragments", [])
            except Exception:
                pass
    
    result_path = os.path.join(_get_structured_dir(), f"structured_{pending_id}.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(structured, f, ensure_ascii=False)
    return result_path


# ==================== 老板查看文件（原文件 vs 加密处理） ====================

def boss_list_files(password: str) -> dict:
    """
    老板查看全貌：列出原文件和加密知识库的对应关系
    返回 {"originals": [...], "encrypted": [...], "summary": {...}}
    """
    _check_password(password)
    secure_store = _import_secure_store()

    # 原文件：从原文件目录按部门子文件夹扫描
    originals = []
    originals_dir = _config.get("originals_dir", "")
    if originals_dir and os.path.isdir(originals_dir):
        for dept_name in sorted(os.listdir(originals_dir)):
            dept_dir = os.path.join(originals_dir, dept_name)
            if not os.path.isdir(dept_dir):
                continue
            for f in sorted(os.listdir(dept_dir)):
                fp = os.path.join(dept_dir, f)
                if os.path.isfile(fp):
                    originals.append({
                        "filename": f,
                        "department": dept_name,
                        "path": fp,
                        "size_kb": round(os.path.getsize(fp) / 1024, 1),
                        "modified": datetime.fromtimestamp(os.path.getmtime(fp)).isoformat(),
                    })

    # 加密知识库：按部门列出
    encrypted = {}
    for dept in _config["departments"]:
        pwd = _config["departments"][dept]
        try:
            records = secure_store.pw_decrypt_store(pwd, base_dir=_config["base_dir"],
                                                     department=dept,
                                                     encrypted_dir=_config.get("encrypted_dir"))
            encrypted[dept] = []
            for r in records:
                encrypted[dept].append({
                    "id": r.get("id", ""),
                    "source_filename": r.get("source_filename", ""),
                    "department": r.get("department", ""),
                    "upload_time": r.get("upload_time", ""),
                    "tags": r.get("tags", []),
                    "summary": r.get("summary", ""),
                    "has_full_text": bool(r.get("full_text", "")),
                    "table_count": len(r.get("tables", [])),
                })
        except Exception:
            encrypted[dept] = []

    total_originals = len(originals)
    total_encrypted = sum(len(v) for v in encrypted.values())

    return {
        "originals": originals,
        "encrypted": encrypted,
        "summary": {
            "total_originals": total_originals,
            "total_encrypted": total_encrypted,
            "departments": list(encrypted.keys()),
        },
    }


def boss_get_record_detail(password: str, record_id: str) -> dict:
    """
    老板查看单条加密记录的完整内容（原文档全文+AI提取）
    """
    _check_password(password)
    secure_store = _import_secure_store()

    for dept in _config["departments"]:
        pwd = _config["departments"][dept]
        try:
            records = secure_store.pw_decrypt_store(pwd, base_dir=_config["base_dir"],
                                                     department=dept,
                                                     encrypted_dir=_config.get("encrypted_dir"))
            for r in records:
                if r.get("id") == record_id:
                    return {
                        "found": True,
                        "department": dept,
                        "record": r,
                    }
        except Exception:
            continue

    return {"found": False, "message": f"未找到记录: {record_id}"}


def boss_get_original_path(password: str, filename: str, department: str = None) -> dict:
    """老板获取原文件路径。department为None时遍历所有部门子目录"""
    _check_password(password)
    originals_dir = _config.get("originals_dir", "")
    if not originals_dir:
        return {"found": False, "message": "原文件目录未配置"}

    if department:
        fp = os.path.join(originals_dir, department, filename)
        if os.path.exists(fp):
            return {"found": True, "path": fp, "filename": filename, "department": department}
    else:
        for dept_name in sorted(os.listdir(originals_dir)):
            dept_dir = os.path.join(originals_dir, dept_name)
            if not os.path.isdir(dept_dir):
                continue
            fp = os.path.join(dept_dir, filename)
            if os.path.exists(fp):
                return {"found": True, "path": fp, "filename": filename, "department": dept_name}

    return {"found": False, "message": f"原文件不存在: {filename}"}


# ==================== 防幻觉AI扫描提示词 ====================

SCAN_PROMPT = """请严格按以下规则提取文档结构化信息：

【严禁推断规则】
- 只提取原文中明确写出的信息，绝不推断、不补充、不猜测
- 原文没有明确写出的信息，标记为"原文未提及"
- 时间、数值等必须原文原样引用，不要自己计算或换算
- 如果信息在表格中但表格数据提取不完整，标记置信度为"低"
- 只从当前 profile 允许的章节提取，不补充被排除的章节内容

请逐句扫描以下文档内容，提取并标注：

文件名：{filename}
文档内容：
{text}

请返回以下JSON（每条信息必须含source_quote原文引用和confidence置信度）：
{{
  "tags": ["从原文中提取的关键标签"],
  "summary": "仅基于原文明确信息的摘要，不推断",
  "facts": [
    {{
      "fact": "提取的事实",
      "source_quote": "原文中对应的句子或段落（逐字引用）",
      "confidence": "高/中/低",
      "note": "补充说明（如：表格数据提取不完整、日期格式模糊等）"
    }}
  ],
  "entities": ["原文明确出现的实体"],
  "client_name": "甲方/客户名称；原文未提及时填原文未提及",
  "project_name": "项目名称；原文未提及时填原文未提及",
  "product_capacity": "产品类型和产能/规模；原文未提及时填原文未提及",
  "quality_summary": "关键质量/规格指标摘要；优先原样保留数值和单位",
  "objective": "主要目标/目的；原文未提及时填原文未提及",
  "process": "方法/流程/工艺路线；原文未提及时填原文未提及",
  "missing_info": ["用户可能想知道但原文未明确提及的信息"]
}}

只返回JSON，不返回其他内容。"""


def get_scan_prompt(text: str, filename: str) -> str:
    return SCAN_PROMPT.format(text=text, filename=filename)

# ── 子模块覆盖（后导入覆盖内联同名函数，提供模块化入口）──
try:
    from _conflicts import *  # noqa: F403
    from _parser import *  # noqa: F403
    import _conflicts, _parser
    _conflicts._config = _config
    _parser._config = _config
except ImportError:
    pass  # 子模块不存在时降级使用内联定义



# ============ Document Brain: AI Directory Compilation ============

def boss_upload_get_wiki_data(department: str = None) -> dict:
    """
    Document Brain — 收集所有已上传文档的 markdown 数据，供 AI 编写 Wiki。

    AI 收到数据后应编写四层 Wiki：
      L1: shared/wiki/<部门>/index.md — 全局目录
      L2: shared/wiki/<部门>/summaries/{doc}.md — 单文档摘要
      L3: shared/wiki/<部门>/concepts/{概念}.md — 跨文档概念对比
      L4: shared/wiki/<部门>/entities/{实体}.md — 跨文档实体关联

    返回每份文档的 markdown 全文 + dl_prepare_for_wiki 结构化数据。
    """
    import json as _json
    import os as _os

    dept = department or _config.get("department", "")
    structured_dir = _get_structured_dir()
    if not _os.path.isdir(structured_dir):
        return {"status": "empty", "documents": [], "message": "No structured data found. Upload documents first."}

    documents = []
    for fname in sorted(_os.listdir(structured_dir)):
        if not fname.startswith("structured_") or not fname.endswith(".json"):
            continue
        fpath = _os.path.join(structured_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            continue

        doc_dept = data.get("department", "")
        if dept and doc_dept != dept:
            continue

        filename = data.get("filename", "unknown")
        markdown = data.get("_docling_markdown", "")

        # Fallback: re-parse if markdown not saved
        if not markdown:
            pending_id = fname[len("structured_"):-len(".json")]
            meta_path = _os.path.join(_get_pending_dir(), f"meta_{pending_id}.json")
            if _os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = _json.load(f)
                    file_path = meta.get("original_path", "")
                    if file_path and _os.path.exists(file_path):
                        dl = _import_docling()
                        parse = dl.dl_parse_document(file_path)
                        if parse.get("status") == "success":
                            markdown = parse.get("markdown", "")
                except Exception:
                    pass

        # Get structured data for AI
        wiki_data = None
        if markdown:
            try:
                dl = _import_docling()
                wiki_data = dl.dl_prepare_for_wiki(markdown, filename, doc_dept)
            except Exception:
                pass

        documents.append({
            "filename": filename,
            "department": doc_dept,
            "markdown": _sanitize_visible_text(markdown),
            "wiki_data": wiki_data,
            "summary": _sanitize_visible_text(data.get("summary", "")),
            "tags": [_sanitize_visible_text(str(t)) for t in data.get("tags", [])],
            "key_data": {
                _sanitize_visible_text(str(k)): _sanitize_visible_text(str(v))
                for k, v in (data.get("key_data", {}) or {}).items()
            },
        })

    # Compute wiki output paths
    kr_dir = os.environ.get("KNOWLEDGE_RAG_DIR", "")
    if kr_dir:
        wiki_root = _os.path.normpath(_os.path.join(kr_dir, "..", "wiki"))
    else:
        wiki_root = _os.path.join(_config["base_dir"], "shared", "wiki")
    dept_wiki = _os.path.join(wiki_root, dept)

    return {
        "status": "success",
        "document_count": len(documents),
        "department": dept,
        "wiki_root": wiki_root,
        "dept_wiki": dept_wiki,
        "wiki_index_path": _os.path.join(dept_wiki, "index.md"),
        "wiki_summaries_dir": _os.path.join(dept_wiki, "summaries"),
        "wiki_concepts_dir": _os.path.join(dept_wiki, "concepts"),
        "wiki_entities_dir": _os.path.join(dept_wiki, "entities"),
        "documents": documents,
        "ai_instructions": {
            "L1_index": "编写 index.md：汇总所有文档的表格（文档名|甲方|工艺|关键指标），按工艺分类索引导航",
            "L2_summaries": "为每份文档编写 summaries/{doc}.md：复现章节目录 + 每章2-3句摘要 + 关键数据表",
            "L3_concepts": "跨文档编写 concepts/{概念}.md：识别跨文档工艺/技术概念，做对比表，列所有相关文档",
            "L4_entities": "跨文档编写 entities/{实体}.md：识别跨文档甲方/公司，关联所有项目+角色+数据",
        },
    }


# Backward compat: returns wiki data instead of compiling
def boss_upload_compile_directory(pending_id: str) -> dict:
    """Deprecated: AI now handles wiki compilation via boss_upload_get_wiki_data()."""
    return {
        "status": "deprecated",
        "pending_id": pending_id,
        "message": "Wiki compilation is now AI-driven. Use boss_upload_get_wiki_data() to get all documents, then write wiki pages.",
    }


def boss_upload_auto_finalize_with_directory(password: str) -> dict:
    """
    Enhanced auto_finalize that returns wiki data for AI compilation.
    Wraps boss_upload_auto_finalize with wiki data collection.
    """
    base_result = boss_upload_auto_finalize(password)
    wiki_result = boss_upload_get_wiki_data()
    base_result["wiki_data"] = wiki_result
    return base_result

