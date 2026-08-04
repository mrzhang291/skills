"""冲突检测与关键指标模块。"""
import sys, os, json, re
from datetime import datetime
from pathlib import Path

# ── 模块级配置（默认值，由 boss_upload.configure() 覆盖）──
_config = {
    "base_dir": None,
    "originals_dir": None,
    "encrypted_dir": None,
    "boss_password": "",
    "departments": {},
    "dept_patterns": {},
}

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
    """
    csv_path = _get_metrics_csv_path(department)
    if not csv_path:
        return {"status": "error", "message": "base_dir 未配置"}

    # 动态列：固定的 + key_data 的 keys
    fieldnames = ["department", "filename", "upload_time", "summary"]
    if key_data:
        fieldnames.extend(key_data.keys())

    row = {
        "department": department,
        "filename": filename,
        "upload_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
    }
    if key_data:
        row.update(key_data)

    file_exists = os.path.exists(csv_path)
    try:
        with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        return {"status": "success", "csv_path": csv_path}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def _normalize_project_name(filename: str) -> str:
    """Extract a stable project key from a filename for conflict comparison."""
    import re
    name = re.sub(r"\.[^.]+$", "", filename)
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

