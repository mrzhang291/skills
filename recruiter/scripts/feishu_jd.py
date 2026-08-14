"""飞书 JD 读写脚本（recruiter skill 用）

子命令：
  read-jd      --position <岗位名>   实时读飞书 JD 文档正文
  list-positions                      列出 JD 目录表格里所有岗位
  create-archive --position <岗位名>  为岗位创建或复用共享入档 Base 下的 table
  verify-archive --position <岗位名>  只读验证岗位入档表记录
  upload-attachment --file <路径>      上传本地简历附件并返回 file_token
  append-record --position <岗位名> --record <json文件路径>
                                       追加评分记录；缺少入档 table 时自动创建；可自动上传简历附件

所有子命令输出 JSON 到 stdout。每次都实时读飞书，不缓存。
"""
import argparse
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_JSON_DUMPS = json.dumps


def _json_default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: str(item))
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def safe_json_dumps(value, *args, **kwargs):
    user_default = kwargs.get("default")

    def default(item):
        try:
            return _json_default(item)
        except TypeError:
            if user_default:
                return user_default(item)
            raise

    kwargs["default"] = default
    return _JSON_DUMPS(value, *args, **kwargs)


json.dumps = safe_json_dumps

SKILL_DIR = Path(__file__).parent.parent
CONFIG_PATH = SKILL_DIR / "config.json"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ARCHIVE_SCHEMA_VERSION = 2
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
CN_MOBILE_RE = re.compile(r"(?<!\d)1[3-9][-\s]?\d{4}[-\s]?\d{4}(?!\d)")
ARCHIVE_FIELD_DEFS = [
    {"name": "候选人", "v1": {"field_name": "候选人", "type": 1}},
    {
        "name": "初筛结论",
        "v1": {"field_name": "初筛结论", "type": 3, "property": {"options": [{"name": "推荐"}, {"name": "待定"}, {"name": "淘汰"}]}},
    },
    {"name": "分数", "v1": {"field_name": "分数", "type": 2, "property": {"formatter": "0"}}},
    {
        "name": "建议动作",
        "v1": {
            "field_name": "建议动作",
            "type": 3,
            "property": {"options": [{"name": "推进初面"}, {"name": "待定复核"}, {"name": "放入人才库"}, {"name": "不推进"}]},
        },
    },
    {"name": "HR反馈", "v1": {"field_name": "HR反馈", "type": 1}},
    {"name": "评审摘要", "v1": {"field_name": "评审摘要", "type": 1}},
    {"name": "简历附件", "v1": {"field_name": "简历附件", "type": 17}},
    {"name": "入档时间", "v1": {"field_name": "入档时间", "type": 5, "property": {"date_formatter": "yyyy-MM-dd HH:mm"}}},
    {"name": "岗位", "v1": {"field_name": "岗位", "type": 1}},
    {"name": "position_id", "v1": {"field_name": "position_id", "type": 1}},
    {"name": "tier", "v1": {"field_name": "tier", "type": 1}},
    {"name": "pending_id", "v1": {"field_name": "pending_id", "type": 1}},
    {"name": "候选人Key", "v1": {"field_name": "候选人Key", "type": 1}},
]
ARCHIVE_FIELD_ORDER = [item["name"] for item in ARCHIVE_FIELD_DEFS]
ARCHIVE_FIELD_SET = set(ARCHIVE_FIELD_ORDER)


def load_config():
    if not CONFIG_PATH.exists():
        print(json.dumps({"ok": False, "error": "config.json not found"}, ensure_ascii=False))
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def http_request(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode("utf-8")
    else:
        data = None
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode("utf-8", errors="ignore")}
    except Exception as e:
        return {"_error": str(e)}


def http_multipart_request(url, headers, fields, file_field, file_path, file_name=None):
    file_path = Path(file_path)
    file_name = file_name or file_path.name
    boundary = f"----RecruiterBoundary{uuid.uuid4().hex}"
    body = bytearray()

    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
        .encode("utf-8")
    )
    body.extend(file_path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(url, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, data=bytes(body), timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode("utf-8", errors="ignore")}
    except Exception as e:
        return {"_error": str(e)}


def get_tenant_token(app_id, app_secret):
    r = http_request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        method="POST", body={"app_id": app_id, "app_secret": app_secret},
    )
    if "tenant_access_token" not in r:
        return None, r
    return r["tenant_access_token"], None


def get_docx_content(token, doc_token):
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_token}/raw_content"
    r = http_request(url, headers={"Authorization": f"Bearer {token}"})
    if r.get("code") != 0:
        return None, r
    return r.get("data", {}).get("content"), r


def get_wiki_node(token, wiki_token):
    url = "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?" + urllib.parse.urlencode({"token": wiki_token})
    r = http_request(url, headers={"Authorization": f"Bearer {token}"})
    if r.get("code") != 0:
        return None, r
    return r.get("data", {}).get("node", {}), r


def list_records(token, app_token, table_id, view_id=None, page_size=100):
    all_records, page_token = [], None
    while True:
        params = {"page_size": page_size}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records?"
            + urllib.parse.urlencode(params)
        )
        r = http_request(url, headers={"Authorization": f"Bearer {token}"})
        if r.get("code") != 0:
            return all_records, r
        data = r.get("data", {})
        all_records.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return all_records, None


def list_fields(token, app_token, table_id, page_size=100):
    all_fields, page_token = [], None
    while True:
        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields?"
            + urllib.parse.urlencode(params)
        )
        r = http_request(url, headers={"Authorization": f"Bearer {token}"})
        if r.get("code") != 0:
            return all_fields, r
        data = r.get("data", {})
        all_fields.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return all_fields, None


def list_tables(token, app_token, page_size=100):
    all_tables, page_token = [], None
    while True:
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables?page_size={page_size}"
        if page_token:
            url += f"&page_token={page_token}"
        r = http_request(url, headers={"Authorization": f"Bearer {token}"})
        if r.get("code") != 0:
            return all_tables, r
        data = r.get("data", {})
        all_tables.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return all_tables, None


def create_bitable_app(token, name):
    return http_request("https://open.feishu.cn/open-apis/bitable/v1/apps", method="POST",
                        headers={"Authorization": f"Bearer {token}"}, body={"name": name})


def create_table(token, app_token, table_name, fields_schema):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables"
    body = {"table": {"name": table_name, "default_view_name": "入档视图", "fields": fields_schema}}
    return http_request(url, method="POST", headers={"Authorization": f"Bearer {token}"}, body=body)


def create_field(token, app_token, table_id, field_schema):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    return http_request(url, method="POST", headers={"Authorization": f"Bearer {token}"}, body=field_schema)


def append_record(token, app_token, table_id, fields):
    url = f"https://open.feishu.cn/open-apis/base/v3/bases/{app_token}/tables/{table_id}/records"
    return http_request(url, method="POST", headers={"Authorization": f"Bearer {token}"}, body=fields)


def update_record(token, app_token, table_id, record_id, fields):
    url = f"https://open.feishu.cn/open-apis/base/v3/bases/{app_token}/tables/{table_id}/records/{record_id}"
    return http_request(url, method="PATCH", headers={"Authorization": f"Bearer {token}"}, body=fields)


def upload_media_all(token, app_token, file_path, parent_type="bitable_file"):
    file_path = Path(file_path).expanduser()
    if not file_path.exists() or not file_path.is_file():
        return None, {"ok": False, "error": "简历附件文件不存在", "file_path": str(file_path)}

    size = file_path.stat().st_size
    if size <= 0:
        return None, {"ok": False, "error": "简历附件文件为空", "file_path": str(file_path)}
    if size > MAX_UPLOAD_BYTES:
        return None, {
            "ok": False,
            "error": "简历附件超过上传大小限制",
            "file_path": str(file_path),
            "size": size,
            "max_size": MAX_UPLOAD_BYTES,
        }

    r = http_multipart_request(
        "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
        headers={"Authorization": f"Bearer {token}"},
        fields={
            "file_name": file_path.name,
            "parent_type": parent_type,
            "parent_node": app_token,
            "size": size,
        },
        file_field="file",
        file_path=file_path,
        file_name=file_path.name,
    )
    if r.get("code") != 0:
        return None, {"ok": False, "error": "简历附件上传失败", "detail": r, "file_path": str(file_path)}

    file_token = r.get("data", {}).get("file_token")
    if not file_token:
        return None, {"ok": False, "error": "简历附件上传成功但未返回 file_token", "detail": r, "file_path": str(file_path)}
    return {"file_token": file_token, "file_name": file_path.name, "size": size}, None


# ---------- 辅助 ----------

def extract_text(field_value):
    if isinstance(field_value, list):
        return "".join([x.get("text", "") if isinstance(x, dict) else str(x) for x in field_value]).strip()
    return str(field_value).strip() if field_value else ""


def table_name(table):
    if not isinstance(table, dict):
        return ""
    return str(table.get("name") or table.get("table_name") or table.get("table", {}).get("name") or "").strip()


def table_id(table):
    if not isinstance(table, dict):
        return None
    return table.get("table_id") or table.get("id") or table.get("table", {}).get("table_id")


def resume_file_path_from_score(score):
    for key in ("简历文件路径", "简历本地路径", "简历路径", "resume_file_path", "resume_path", "file_path"):
        value = score.get(key)
        if value:
            return str(value)
    return None


def resolve_record_path(record_path):
    path = Path(record_path).expanduser()
    if path.exists():
        return path
    if not path.is_absolute():
        skill_relative = SKILL_DIR / path
        if skill_relative.exists():
            return skill_relative
    raise FileNotFoundError(str(record_path))


def redact_contact(value):
    text = str(value or "")
    text = EMAIL_RE.sub("[email]", text)
    text = CN_MOBILE_RE.sub("[phone]", text)
    return text


def candidate_name_from_score(score):
    for key in ("候选人姓名", "姓名", "候选人", "candidate_name", "name"):
        value = extract_text(score.get(key))
        if value:
            return redact_contact(value)
    raw = candidate_identifier_from_score(score)
    # Common pending ids look like "姓名-phone-email"; keep only the leading name.
    name = raw
    for sep in ("-", "_", " ", "，", ",", "；", ";"):
        if sep in name:
            name = name.split(sep, 1)[0]
            break
    return redact_contact(name or raw)


def candidate_identifier_from_score(score):
    for key in ("候选人标识", "candidate_id", "candidate", "候选人姓名", "姓名"):
        value = extract_text(score.get(key))
        if value:
            return value
    resume_path = resume_file_path_from_score(score)
    if resume_path:
        return Path(resume_path).stem
    return "未命名候选人"


def candidate_key_from_score(score):
    raw = candidate_identifier_from_score(score)
    base = normalize_duplicate_text(raw)
    if not base:
        base = normalize_duplicate_text(candidate_name_from_score(score))
    import hashlib
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def score_time_ms(score):
    for key in ("评分时间", "scored_at_ms", "score_time_ms"):
        value = score.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    for key in ("评分时间ISO", "scored_at", "score_time"):
        value = score.get(key)
        if not value:
            continue
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(raw).timestamp() * 1000)
        except ValueError:
            pass
    return int(datetime.now().timestamp() * 1000)


def normalize_duplicate_text(value):
    return " ".join(extract_text(value).split()).casefold()


def record_field_text(record, field_name):
    return extract_text(record.get("fields", {}).get(field_name, ""))


def find_duplicate_record(records, candidate, position_id):
    candidate_values = candidate if isinstance(candidate, list) else [candidate]
    target_candidates = {normalize_duplicate_text(item) for item in candidate_values if normalize_duplicate_text(item)}
    target_position_id = normalize_duplicate_text(position_id)
    if not target_candidates:
        return None

    for record in records:
        fields = record.get("fields", {})
        candidates = [
            normalize_duplicate_text(fields.get("候选人Key", "")),
            normalize_duplicate_text(fields.get("候选人标识", "")),
            normalize_duplicate_text(fields.get("候选人", "")),
        ]
        record_candidate = next((item for item in candidates if item), "")
        if record_candidate not in target_candidates:
            continue

        record_position_id = normalize_duplicate_text(fields.get("position_id", ""))
        # Older tables may not have position_id filled. Since each archive table is already per-position,
        # treat candidate match inside the same table as a duplicate when position_id is absent.
        if not record_position_id or record_position_id == target_position_id:
            return record
    return None


def extract_doc_token_from_link(link):
    if not link:
        return None, None
    link = link.split("?")[0].rstrip("/")
    parts = link.split("/")
    for i, p in enumerate(parts):
        if p == "docx" and i + 1 < len(parts):
            return "docx", parts[i + 1]
        if p == "wiki" and i + 1 < len(parts):
            return "wiki", parts[i + 1]
    # Allow pasting a raw docx token in the JD field.
    if re.fullmatch(r"[A-Za-z0-9]{20,}", link):
        return "docx", link
    return None, None


def get_config_field(fs, key, default):
    value = str(fs.get(key, "")).strip()
    return value or default


def is_placeholder_secret(value):
    raw = str(value or "").strip()
    if not raw:
        return True
    lowered = raw.lower()
    return "请填写" in raw or "placeholder" in lowered or raw.startswith("${")


def resolve_feishu_credentials(fs):
    app_id = os.environ.get("FEISHU_APP_ID") or str(fs.get("app_id", "")).strip()
    secret_env_name = str(fs.get("app_secret_env", "FEISHU_APP_SECRET")).strip() or "FEISHU_APP_SECRET"
    app_secret = os.environ.get(secret_env_name)
    if not app_secret and not is_placeholder_secret(fs.get("app_secret")):
        app_secret = str(fs.get("app_secret", "")).strip()
    missing = []
    if not app_id or "请填写" in app_id:
        missing.append("app_id")
    if not app_secret:
        missing.append(f"app_secret ({secret_env_name})")
    return app_id, app_secret, missing, secret_env_name


def available_field_names(records):
    names = set()
    for rec in records:
        fields = rec.get("fields", {})
        if isinstance(fields, dict):
            names.update(fields.keys())
    return sorted(names)


def extract_link_value(field_value):
    if isinstance(field_value, dict):
        return field_value.get("link") or field_value.get("text")
    if isinstance(field_value, list):
        for item in field_value:
            if isinstance(item, dict):
                link = item.get("link") or item.get("text")
                if link:
                    return link
            elif item:
                return str(item)
        return None
    if isinstance(field_value, str):
        return field_value
    return None


def normalize_tier(value, score=None):
    raw = str(value or "").strip().lower()
    mapping = {
        "pass": "pass",
        "推荐": "pass",
        "通过": "pass",
        "推进": "pass",
        "a": "pass",
        "b": "pass",
        "borderline": "borderline",
        "待定": "borderline",
        "边缘": "borderline",
        "c": "borderline",
        "reject": "reject",
        "淘汰": "reject",
        "不推荐": "reject",
        "不推进": "reject",
        "d": "reject",
    }
    if raw in mapping:
        return mapping[raw]
    if score is not None:
        try:
            numeric = int(score)
            if numeric >= 70:
                return "pass"
            if numeric >= 55:
                return "borderline"
            return "reject"
        except (TypeError, ValueError):
            pass
    return ""


def tier_label(tier):
    return {
        "pass": "推荐",
        "borderline": "待定",
        "reject": "淘汰",
    }.get(tier, "")


def tier_from_hr_feedback(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if any(word in text for word in ("拒绝", "淘汰", "不推进", "不推荐", "不合适", "不要")):
        return "reject"
    if any(word in text for word in ("待定", "复核", "再看")):
        return "borderline"
    if any(word in text for word in ("推荐", "通过", "推进初面", "进初面", "面试")):
        return "pass"
    return ""


def legacy_grade_from_score(score):
    try:
        numeric = int(score)
    except (TypeError, ValueError):
        return ""
    if numeric >= 82:
        return "A"
    if numeric >= 70:
        return "B"
    if numeric >= 55:
        return "C"
    return "D"


def normalize_next_step(value, tier):
    raw = str(value or "").strip()
    # The final tier wins over stale or pre-HR "next step" text in pending JSON.
    # Otherwise a rejected candidate can incorrectly remain "待定复核".
    if tier == "pass":
        return "推进初面"
    if tier == "borderline":
        return "待定复核"
    if tier == "reject":
        return "不推进"
    if raw == "待定":
        return "待定复核"
    if raw in {"推进初面", "待定复核", "放入人才库", "不推进"}:
        return raw
    return raw


def join_value(value):
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value or "")


def bullet_lines(value):
    items = value if isinstance(value, list) else [value]
    lines = []
    for item in items:
        text = str(item or "").strip()
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines) if lines else "- 无"


def build_review_summary(score):
    sections = []
    reason = str(score.get("推荐理由", "")).strip()
    if reason:
        sections.append(f"【判断理由】\n{reason}")
    sections.append(f"【匹配亮点】\n{bullet_lines(score.get('匹配亮点', []))}")
    sections.append(f"【风险点】\n{bullet_lines(score.get('风险点', []))}")
    uncertainties = score.get("不确定项", [])
    if uncertainties:
        sections.append(f"【不确定项】\n{bullet_lines(uncertainties)}")
    reading_scope = str(score.get("阅读范围", "")).strip()
    if reading_scope:
        sections.append(f"【阅读范围】\n{reading_scope}")
    basis = str(score.get("评分依据", "")).strip()
    if basis:
        sections.append(f"【评分依据】\n{basis}")
    resume_path = resume_file_path_from_score(score)
    if resume_path:
        sections.append(f"【简历文件】\n{Path(resume_path).name}")
    return "\n\n".join(sections).strip()


def build_archive_fields_schema():
    """Compact archive columns for newly created tables.

    Keep columns useful for sorting/filtering short, and place long narrative
    content into one review summary column.
    """
    return [json.loads(json.dumps(item["v1"], ensure_ascii=False)) for item in ARCHIVE_FIELD_DEFS]


def archive_schema_report(fields_meta):
    actual = field_names_from_meta(fields_meta)
    missing = [name for name in ARCHIVE_FIELD_ORDER if name not in actual]
    extra = sorted(name for name in actual if name not in ARCHIVE_FIELD_SET)
    return {
        "expected_fields": list(ARCHIVE_FIELD_ORDER),
        "field_names": sorted(actual),
        "missing_fields": missing,
        "extra_fields": extra,
        "schema_matches": not missing and not extra,
    }


def normalize_position_text(value):
    raw = str(value or "").strip().casefold()
    return re.sub(r"[\s\-_/·（）()【】\[\]]+", "", raw)


def suggest_position_id(position_name):
    text = str(position_name or "").strip()
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
    if ascii_slug and not has_cjk:
        return ascii_slug[:60]

    dictionary = [
        ("AI", "ai"),
        ("产品", "product"),
        ("视效", "visual"),
        ("设计", "designer"),
        ("运营", "ops"),
        ("营销", "marketing"),
        ("红人", "influencer"),
        ("全栈", "fullstack"),
        ("前端", "frontend"),
        ("后端", "backend"),
        ("研发", "dev"),
        ("测试", "qa"),
        ("工程师", "engineer"),
        ("实习生", "intern"),
        ("实习", "intern"),
        ("正职", "fulltime"),
        ("交付", "delivery"),
    ]
    parts = []
    upper_text = text.upper()
    if "FDE" in upper_text:
        parts.append("fde")
    for needle, slug in dictionary:
        if needle in text and slug not in parts:
            parts.append(slug)
    return "-".join(parts)[:60] or "position"


def position_id_from_map(position_name, position_id_map):
    if position_name in position_id_map:
        return position_id_map[position_name]
    target = normalize_position_text(position_name)
    for name, position_id in position_id_map.items():
        if normalize_position_text(name) == target:
            return position_id
    return None


def build_position_entries(records, position_id_map, position_name_field="岗位名称"):
    entries = []
    for rec in records:
        name = extract_text(rec.get("fields", {}).get(position_name_field, ""))
        if not name:
            continue
        position_id = position_id_from_map(name, position_id_map)
        entries.append({
            "position_name": name,
            "position_id": position_id,
            "suggested_position_id": None if position_id else suggest_position_id(name),
            "needs_mapping": not bool(position_id),
            "record": rec,
            "record_id": rec.get("record_id"),
        })
    return entries


def public_position_entries(entries):
    return [
        {
            "position_name": item["position_name"],
            "position_id": item.get("position_id"),
            "suggested_position_id": item.get("suggested_position_id"),
            "needs_mapping": item.get("needs_mapping", False),
            "record_id": item.get("record_id"),
        }
        for item in entries
    ]


def build_config_position_entries(position_id_map):
    return [
        {
            "position_name": name,
            "position_id": pid,
            "record": None,
            "record_id": name,
        }
        for name, pid in position_id_map.items()
    ]


def field_name_from_meta(item):
    return str(item.get("field_name") or item.get("name") or "").strip()


def field_names_from_meta(fields_meta):
    return {name for name in (field_name_from_meta(item) for item in fields_meta) if name}


def should_write_field(value):
    if value is None:
        return False
    if value == "":
        return False
    if value == []:
        return False
    return True


def filter_fields_for_table(fields, field_names):
    if not field_names:
        return {k: v for k, v in fields.items() if should_write_field(v)}, []
    filtered = {k: v for k, v in fields.items() if k in field_names and should_write_field(v)}
    omitted = sorted(k for k in fields if k not in filtered)
    return filtered, omitted


def record_id_from_write_response(response):
    def find_record_id(value):
        if isinstance(value, dict):
            for key in ("record_id", "id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
            for nested in value.values():
                found = find_record_id(nested)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = find_record_id(item)
                if found:
                    return found
        return None

    return find_record_id(response)


def archive_record_summary(record):
    fields = record.get("fields", {}) if isinstance(record, dict) else {}
    return {
        "record_id": record.get("record_id") or record.get("id"),
        "候选人": extract_text(fields.get("候选人", "")),
        "初筛结论": extract_text(fields.get("初筛结论", "")),
        "分数": fields.get("分数", ""),
        "建议动作": extract_text(fields.get("建议动作", "")),
        "tier": extract_text(fields.get("tier", "")),
        "pending_id": extract_text(fields.get("pending_id", "")),
        "候选人Key": extract_text(fields.get("候选人Key", "")),
    }


def record_matches_candidate(record, candidate):
    target = normalize_duplicate_text(candidate)
    if not target:
        return True
    fields = record.get("fields", {}) if isinstance(record, dict) else {}
    candidates = [
        record.get("record_id") if isinstance(record, dict) else "",
        fields.get("候选人", ""),
        fields.get("候选人Key", ""),
        fields.get("候选人标识", ""),
        fields.get("pending_id", ""),
    ]
    for value in candidates:
        normalized = normalize_duplicate_text(value)
        if normalized and (target in normalized or normalized in target):
            return True
    return False


def ensure_archive_fields(token, app_token, table_id):
    fields_meta, err = list_fields(token, app_token, table_id)
    if err:
        return None, {
            "ok": False,
            "error": "读取入档表字段失败",
            "detail": err,
            "created_fields": [],
        }

    existing = field_names_from_meta(fields_meta)
    created = []
    failed = []
    for schema in build_archive_fields_schema():
        name = schema["field_name"]
        if name in existing:
            continue
        r = create_field(token, app_token, table_id, schema)
        if r.get("code") == 0:
            created.append(name)
            existing.add(name)
        else:
            failed.append({"field_name": name, "detail": r})

    refreshed_meta, refresh_err = list_fields(token, app_token, table_id)
    if refresh_err:
        refreshed_meta = fields_meta
    report = archive_schema_report(refreshed_meta)
    return {
        **report,
        "created_fields": created,
        "failed_fields": failed,
        "refresh_error": refresh_err,
    }, None if not failed else {
        "ok": False,
        "error": "部分入档字段创建失败",
        "created_fields": created,
        "failed_fields": failed,
        "refresh_error": refresh_err,
    }


def dedupe_position_entries(entries):
    seen, unique = set(), []
    for item in entries:
        key = item.get("record_id") or item["position_name"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def resolve_position(entries, target):
    target_raw = str(target or "").strip()
    target_norm = normalize_position_text(target_raw)
    if not target_norm:
        return None, []

    def position_id_norm(item):
        return normalize_position_text(item.get("position_id"))

    exact = [
        item for item in entries
        if item["position_name"] == target_raw or item.get("position_id") == target_raw
    ]
    exact = dedupe_position_entries(exact)
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact

    normalized_exact = [
        item for item in entries
        if normalize_position_text(item["position_name"]) == target_norm
        or position_id_norm(item) == target_norm
    ]
    normalized_exact = dedupe_position_entries(normalized_exact)
    if len(normalized_exact) == 1:
        return normalized_exact[0], []
    if len(normalized_exact) > 1:
        return None, normalized_exact

    contains = [
        item for item in entries
        if target_norm in normalize_position_text(item["position_name"])
        or normalize_position_text(item["position_name"]) in target_norm
        or (position_id_norm(item) and target_norm in position_id_norm(item))
    ]
    contains = dedupe_position_entries(contains)
    if len(contains) == 1:
        return contains[0], []
    if len(contains) > 1:
        return None, contains

    return None, []


def ensure_archive_table(config, token, position_id, position_name, create_if_missing=True):
    archive_tables = config.setdefault("archive_tables", {})
    archive_base = config.get("archive_base") or {}
    archive = archive_tables.get(position_id)
    app_token = archive_base.get("app_token")
    result = {
        "position_id": position_id,
        "position_name": position_name,
        "url": archive_base.get("url"),
        "created_base": False,
        "created_table": False,
        "reused_existing_table": False,
        "used_legacy_archive": False,
        "config_updated": False,
    }

    if archive and app_token:
        result.update({"app_token": app_token, "archive": archive, "table_id": archive.get("table_id")})
        return result, None

    old = config.get("archive_apps", {}).get(position_id)
    if old and old.get("app_token") and old.get("table_id"):
        result.update({
            "app_token": old.get("app_token"),
            "archive": old,
            "table_id": old.get("table_id"),
            "url": old.get("url"),
            "used_legacy_archive": True,
        })
        return result, None

    if not create_if_missing:
        return None, {"ok": False, "error": "该岗位还没有入档表格", "position_id": position_id}

    if not app_token:
        r = create_bitable_app(token, "招聘入档")
        if r.get("code") != 0:
            return None, {"ok": False, "error": "创建共享入档 Base 失败", "detail": r}
        app = r.get("data", {}).get("app", {})
        app_token = app.get("app_token")
        url = app.get("url")
        if not app_token:
            return None, {"ok": False, "error": "创建共享入档 Base 成功但未返回 app_token", "detail": r}
        archive_base = {"app_token": app_token, "url": url, "created_at": datetime.now().strftime("%Y-%m-%d")}
        config["archive_base"] = archive_base
        result.update({"created_base": True, "config_updated": True, "url": url})
    else:
        result["url"] = archive_base.get("url")

    tables, table_err = list_tables(token, app_token)
    if table_err:
        return None, {"ok": False, "error": "查询共享入档 Base 的 tables 失败", "detail": table_err}

    existing_table = None
    for item in tables:
        if normalize_position_text(table_name(item)) == normalize_position_text(position_name):
            existing_table = item
            break

    if existing_table:
        existing_table_id = table_id(existing_table)
        if not existing_table_id:
            return None, {"ok": False, "error": "找到同名 table 但未返回 table_id", "table": existing_table}
        archive = {
            "table_id": existing_table_id,
            "created_at": datetime.now().strftime("%Y-%m-%d"),
            "reused_existing": True,
            "schema_version": ARCHIVE_SCHEMA_VERSION,
        }
        archive_tables[position_id] = archive
        result.update({"reused_existing_table": True, "config_updated": True})
    else:
        r2 = create_table(token, app_token, position_name, build_archive_fields_schema())
        if r2.get("code") != 0:
            return None, {"ok": False, "error": "创建入档 table 失败", "detail": r2}
        new_table_id = r2.get("data", {}).get("table_id") or table_id(r2.get("data", {}).get("table", {}))
        if not new_table_id:
            return None, {"ok": False, "error": "创建入档 table 成功但未返回 table_id", "detail": r2}
        archive = {
            "table_id": new_table_id,
            "created_at": datetime.now().strftime("%Y-%m-%d"),
            "schema_version": ARCHIVE_SCHEMA_VERSION,
        }
        archive_tables[position_id] = archive
        result.update({"created_table": True, "config_updated": True})

    save_config(config)
    result.update({
        "app_token": app_token,
        "archive": archive,
        "table_id": archive.get("table_id"),
        "url": (config.get("archive_base") or {}).get("url"),
    })
    return result, None


def get_token_and_config():
    config = load_config()
    fs = config["feishu"]
    app_id, app_secret, missing, secret_env_name = resolve_feishu_credentials(fs)
    if missing:
        print(json.dumps({
            "ok": False,
            "error": "缺少飞书凭证",
            "missing": missing,
            "hint": f"请把飞书 App Secret 放到环境变量 {secret_env_name}，不要写入 config.json",
        }, ensure_ascii=False))
        sys.exit(1)
    token, err = get_tenant_token(app_id, app_secret)
    if not token:
        print(json.dumps({"ok": False, "error": "获取 token 失败", "detail": err}, ensure_ascii=False))
        sys.exit(1)
    return config, fs, token


# ---------- 子命令 ----------

def cmd_list_positions(args):
    config, fs, token = get_token_and_config()
    records, err = list_records(token, fs["jd_app_token"], fs["jd_index_table_id"], fs.get("jd_index_view_id"))
    if err:
        print(json.dumps({"ok": False, "error": "读 JD 目录表格失败", "detail": err}, ensure_ascii=False))
        sys.exit(1)
    position_name_field = get_config_field(fs, "jd_position_name_field", "岗位名称")
    position_id_map = config.get("position_id_map", {})
    archive_tables = config.get("archive_tables", {})
    archive_apps = config.get("archive_apps", {})  # 旧格式兼容
    positions = []
    entries = build_position_entries(records, position_id_map, position_name_field)
    if records and not entries:
        print(json.dumps({
            "ok": False,
            "error": "JD目录表格里未找到有效岗位名称",
            "position_name_field": position_name_field,
            "available_fields": available_field_names(records),
        }, ensure_ascii=False))
        sys.exit(1)
    for item in entries:
        pid = item.get("position_id")
        positions.append({
            "position_name": item["position_name"],
            "position_id": pid,
            "suggested_position_id": item.get("suggested_position_id"),
            "needs_mapping": item.get("needs_mapping", False),
            "has_archive": bool(pid and (pid in archive_tables or pid in archive_apps)),
            "record_id": item.get("record_id"),
        })
    print(json.dumps({"ok": True, "positions": positions}, ensure_ascii=False))


def cmd_read_jd(args):
    config, fs, token = get_token_and_config()
    records, err = list_records(token, fs["jd_app_token"], fs["jd_index_table_id"], fs.get("jd_index_view_id"))
    if err:
        print(json.dumps({"ok": False, "error": "读 JD 目录表格失败", "detail": err}, ensure_ascii=False))
        sys.exit(1)

    target = args.position
    position_id_map = config.get("position_id_map", {})
    position_name_field = get_config_field(fs, "jd_position_name_field", "岗位名称")
    jd_doc_link_field = get_config_field(fs, "jd_doc_link_field", "JD文档")
    entries = build_position_entries(records, position_id_map, position_name_field)
    if records and not entries:
        print(json.dumps({
            "ok": False,
            "error": "JD目录表格里未找到有效岗位名称",
            "position": target,
            "position_name_field": position_name_field,
            "available_fields": available_field_names(records),
        }, ensure_ascii=False))
        sys.exit(1)
    found, candidates = resolve_position(entries, target)

    if candidates:
        print(json.dumps({
            "ok": False,
            "error": "岗位匹配不唯一，请让HR确认具体岗位",
            "position": target,
            "position_name_field": position_name_field,
            "candidates": public_position_entries(candidates),
        }, ensure_ascii=False))
        sys.exit(1)

    if not found:
        print(json.dumps({
            "ok": False,
            "error": "未找到该岗位",
            "position": target,
            "position_name_field": position_name_field,
            "available": public_position_entries(entries),
            "available_fields": available_field_names(records),
        }, ensure_ascii=False))
        sys.exit(1)

    found_rec = found["record"]
    position_name = found["position_name"]
    position_id = found.get("position_id")
    jd_field = found_rec.get("fields", {}).get(jd_doc_link_field, {})
    link = extract_link_value(jd_field)

    doc_kind, doc_ref = extract_doc_token_from_link(link)
    doc_token = doc_ref if doc_kind == "docx" else None
    wiki_node = None
    if doc_kind == "wiki":
        wiki_node, wiki_err = get_wiki_node(token, doc_ref)
        if not wiki_node:
            print(json.dumps({
                "ok": False,
                "error": "解析 wiki JD 文档失败",
                "position": position_name,
                "position_id": position_id,
                "doc_link": link,
                "detail": wiki_err,
            }, ensure_ascii=False))
            sys.exit(1)
        doc_token = wiki_node.get("obj_token")

    if not doc_token:
        print(json.dumps({
            "ok": False,
            "error": "JD文档字段无有效链接",
            "position": position_name,
            "position_id": position_id,
            "jd_doc_link_field": jd_doc_link_field,
            "available_fields": available_field_names([found_rec]),
        }, ensure_ascii=False))
        sys.exit(1)

    content, doc_err = get_docx_content(token, doc_token)
    if content is None:
        print(json.dumps({"ok": False, "error": "读 JD 文档正文失败", "position": position_name, "position_id": position_id, "doc_token": doc_token, "doc_link": link, "detail": doc_err}, ensure_ascii=False))
        sys.exit(1)

    # schema_ok 仅用于兼容保留，本函数实际未触发表结构校验
    schema_ok = True
    print(json.dumps({
        "ok": schema_ok,
        "position": target,
        "position_name": position_name,
        "position_id": position_id,
        "doc_kind": doc_kind,
        "jd_text": content,
        "jd_length": len(content),
    }, ensure_ascii=False))


def cmd_create_archive(args):
    config, fs, token = get_token_and_config()
    position_id_map = config.get("position_id_map", {})
    position, candidates = resolve_position(build_config_position_entries(position_id_map), args.position)
    if candidates:
        print(json.dumps({
            "ok": False,
            "error": "岗位匹配不唯一，请让HR确认具体岗位",
            "position": args.position,
            "candidates": public_position_entries(candidates),
        }, ensure_ascii=False))
        sys.exit(1)
    if not position:
        print(json.dumps({"ok": False, "error": "position_id_map 里没有该岗位", "position": args.position}, ensure_ascii=False))
        sys.exit(1)
    position_name = position["position_name"]
    position_id = position["position_id"]

    archive_result, archive_err = ensure_archive_table(config, token, position_id, position_name, create_if_missing=True)
    if archive_err:
        print(json.dumps(archive_err, ensure_ascii=False))
        sys.exit(1)
    schema_result, schema_err = ensure_archive_fields(
        token,
        archive_result["app_token"],
        archive_result["table_id"],
    )
    if schema_result and archive_result.get("archive") is not None:
        archive_result["archive"]["schema_version"] = ARCHIVE_SCHEMA_VERSION
        save_config(config)
    print(json.dumps({
        "ok": True,
        "position_id": position_id,
        "app_token": archive_result["app_token"],
        "table_id": archive_result["table_id"],
        "url": archive_result.get("url"),
        "shared_base": not archive_result.get("used_legacy_archive"),
        "created_base": archive_result["created_base"],
        "created_table": archive_result["created_table"],
        "reused_existing_table": archive_result["reused_existing_table"],
        "used_legacy_archive": archive_result["used_legacy_archive"],
        "schema_upgrade": schema_result,
        "schema_error": schema_err,
    }, ensure_ascii=False))


def cmd_upload_attachment(args):
    file_path = Path(args.file).expanduser()
    if not file_path.exists() or not file_path.is_file():
        print(json.dumps({"ok": False, "error": "简历附件文件不存在", "file_path": str(file_path)}, ensure_ascii=False))
        sys.exit(1)

    config, fs, token = get_token_and_config()
    app_token = args.app_token or config.get("archive_base", {}).get("app_token")
    if not app_token:
        print(json.dumps({
            "ok": False,
            "error": "缺少入档 Base app_token，请先 create-archive 或传 --app-token",
        }, ensure_ascii=False))
        sys.exit(1)

    upload_result, upload_err = upload_media_all(token, app_token, file_path, args.parent_type)
    if upload_err:
        print(json.dumps(upload_err, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({"ok": True, **upload_result, "app_token": app_token, "parent_type": args.parent_type}, ensure_ascii=False))


def cmd_sync_archive_schema(args):
    config, fs, token = get_token_and_config()
    position_id_map = config.get("position_id_map", {})
    if args.all:
        positions = build_config_position_entries(position_id_map)
    else:
        position, candidates = resolve_position(build_config_position_entries(position_id_map), args.position)
        if candidates:
            print(json.dumps({
                "ok": False,
                "error": "岗位匹配不唯一，请让HR确认具体岗位",
                "position": args.position,
                "candidates": public_position_entries(candidates),
            }, ensure_ascii=False))
            sys.exit(1)
        if not position:
            print(json.dumps({"ok": False, "error": "position_id_map 里没有该岗位", "position": args.position}, ensure_ascii=False))
            sys.exit(1)
        positions = [position]

    results = []
    for position in positions:
        position_name = position["position_name"]
        position_id = position["position_id"]
        archive_result, archive_err = ensure_archive_table(config, token, position_id, position_name, create_if_missing=True)
        if archive_err:
            results.append({
                "position_name": position_name,
                "position_id": position_id,
                "ok": False,
                "error": archive_err,
            })
            continue
        schema_result, schema_err = ensure_archive_fields(token, archive_result["app_token"], archive_result["table_id"])
        if archive_result.get("archive") is not None:
            archive_result["archive"]["schema_version"] = ARCHIVE_SCHEMA_VERSION
        results.append({
            "position_name": position_name,
            "position_id": position_id,
            "ok": schema_err is None and bool(schema_result and schema_result.get("schema_matches")),
            "table_id": archive_result["table_id"],
            "url": archive_result.get("url"),
            "created_table": archive_result["created_table"],
            "reused_existing_table": archive_result["reused_existing_table"],
            "schema_upgrade": schema_result,
            "schema_error": schema_err,
        })
    save_config(config)
    print(json.dumps({"ok": all(item.get("ok") for item in results), "results": results}, ensure_ascii=False))


def cmd_verify_archive(args):
    config, fs, token = get_token_and_config()
    position_id_map = config.get("position_id_map", {})
    if args.position:
        position, candidates = resolve_position(build_config_position_entries(position_id_map), args.position)
        if candidates:
            print(json.dumps({
                "ok": False,
                "error": "岗位匹配不唯一，请让HR确认具体岗位",
                "position": args.position,
                "candidates": public_position_entries(candidates),
            }, ensure_ascii=False))
            sys.exit(1)
        if not position:
            print(json.dumps({"ok": False, "error": "position_id_map 里没有该岗位", "position": args.position}, ensure_ascii=False))
            sys.exit(1)
        positions = [position]
    else:
        positions = build_config_position_entries(position_id_map)

    results = []
    for position in positions:
        position_name = position["position_name"]
        position_id = position["position_id"]
        archive_result, archive_err = ensure_archive_table(config, token, position_id, position_name, create_if_missing=False)
        if archive_err:
            results.append({
                "position_name": position_name,
                "position_id": position_id,
                "ok": False,
                "error": archive_err,
                "records": [],
                "record_count": 0,
                "matched_count": 0,
            })
            continue

        records, records_err = list_records(token, archive_result["app_token"], archive_result["table_id"])
        if records_err:
            results.append({
                "position_name": position_name,
                "position_id": position_id,
                "ok": False,
                "error": {"ok": False, "error": "读取入档记录失败", "detail": records_err},
                "table_id": archive_result["table_id"],
                "records": [],
                "record_count": 0,
                "matched_count": 0,
            })
            continue

        matched = [record for record in records if record_matches_candidate(record, args.candidate)]
        limit = max(args.limit, 0)
        summarized = [archive_record_summary(record) for record in matched[:limit or None]]
        results.append({
            "position_name": position_name,
            "position_id": position_id,
            "ok": True,
            "url": archive_result.get("url"),
            "table_id": archive_result["table_id"],
            "used_legacy_archive": archive_result["used_legacy_archive"],
            "record_count": len(records),
            "matched_count": len(matched),
            "candidate_filter": args.candidate or "",
            "records": summarized,
        })

    print(json.dumps({
        "ok": all(item.get("ok") for item in results),
        "candidate_filter": args.candidate or "",
        "results": results,
    }, ensure_ascii=False))


def cmd_append_record(args):
    config, fs, token = get_token_and_config()
    position_id_map = config.get("position_id_map", {})
    position, candidates = resolve_position(build_config_position_entries(position_id_map), args.position)
    if candidates:
        print(json.dumps({
            "ok": False,
            "error": "岗位匹配不唯一，请让HR确认具体岗位",
            "position": args.position,
            "candidates": public_position_entries(candidates),
        }, ensure_ascii=False))
        sys.exit(1)
    if not position:
        print(json.dumps({"ok": False, "error": "position_id_map 里没有该岗位", "position": args.position}, ensure_ascii=False))
        sys.exit(1)
    position_name = position["position_name"]
    position_id = position["position_id"]

    archive_result, archive_err = ensure_archive_table(config, token, position_id, position_name, create_if_missing=True)
    if archive_err:
        print(json.dumps(archive_err, ensure_ascii=False))
        sys.exit(1)
    archive = archive_result["archive"]
    app_token = archive_result["app_token"]
    schema_result, schema_err = ensure_archive_fields(token, app_token, archive["table_id"])
    if schema_err:
        print(json.dumps(schema_err, ensure_ascii=False))
        sys.exit(1)
    if not schema_result or not schema_result.get("schema_matches"):
        print(json.dumps({
            "ok": False,
            "error": "入档表字段与 skill 标准字段不一致，已停止写入",
            "position_id": position_id,
            "table_id": archive["table_id"],
            "schema": schema_result,
        }, ensure_ascii=False))
        sys.exit(1)
    archive_field_names = set(schema_result.get("field_names", []))
    archive_fields_err = schema_err
    if schema_result:
        archive["schema_version"] = ARCHIVE_SCHEMA_VERSION
        save_config(config)

    try:
        record_path = resolve_record_path(args.record)
    except FileNotFoundError:
        print(json.dumps({
            "ok": False,
            "error": "评分 JSON 文件不存在",
            "record": args.record,
            "hint": "可传绝对路径、当前目录相对路径，或 recruiter skill 根目录下的相对路径",
        }, ensure_ascii=False))
        sys.exit(1)

    with record_path.open("r", encoding="utf-8-sig") as f:
        score = json.load(f)

    candidate_name = candidate_name_from_score(score)
    candidate_raw_id = candidate_identifier_from_score(score)
    candidate_key = candidate_key_from_score(score)
    score_value = score.get("score", score.get("总分", score.get("综合分", 0)))
    hr_feedback = score.get("HR反馈", "")
    tier = tier_from_hr_feedback(hr_feedback) or normalize_tier(
        score.get("tier", score.get("初筛结论", score.get("推荐等级"))),
        score_value,
    )
    next_step = normalize_next_step(score.get("建议下一步", score.get("recommendation")), tier)
    fields = {
        "候选人": candidate_name,
        "初筛结论": tier_label(tier),
        "分数": score_value,
        "建议动作": next_step,
        "HR反馈": hr_feedback,
        "评审摘要": build_review_summary(score),
        "入档时间": score_time_ms(score),
        "岗位": position_name,
        "position_id": position_id,
        "tier": tier,
        "pending_id": score.get("pending_id", ""),
        "候选人Key": candidate_key,
    }

    duplicate_check = {
        "action": args.duplicate_action,
        "checked": False,
        "duplicate_found": False,
        "existing_record_id": None,
        "error": None,
    }
    duplicate_record_id = None
    if args.duplicate_action in {"skip", "update"} and candidate_key:
        duplicate_check["checked"] = True
        existing_records, duplicate_err = list_records(token, app_token, archive["table_id"])
        if duplicate_err:
            duplicate_check["error"] = duplicate_err
            print(json.dumps({
                "ok": False,
                "error": "入档查重失败",
                "duplicate_check": duplicate_check,
            }, ensure_ascii=False))
            sys.exit(1)
        duplicate = find_duplicate_record(existing_records, [candidate_key, candidate_raw_id, candidate_name], position_id)
        if duplicate:
            duplicate_check["duplicate_found"] = True
            duplicate_check["existing_record_id"] = duplicate.get("record_id")
            duplicate_record_id = duplicate.get("record_id")
            if args.duplicate_action == "skip":
                print(json.dumps({
                    "ok": True,
                    "skipped_duplicate": True,
                    "record_id": duplicate.get("record_id"),
                    "url": config.get("archive_base", {}).get("url") or archive.get("url"),
                    "table_id": archive_result["table_id"],
                    "duplicate_check": duplicate_check,
                    "attachment_upload": {"attempted": False, "uploaded": False, "file_token": None, "error": None},
                }, ensure_ascii=False))
                return

    attachment_upload = {
        "attempted": False,
        "uploaded": False,
        "file_token": score.get("简历附件_file_token"),
        "error": None,
    }
    if score.get("简历附件_file_token"):
        fields["简历附件"] = [{"file_token": score["简历附件_file_token"]}]
    else:
        resume_file_path = resume_file_path_from_score(score)
        if resume_file_path:
            attachment_upload["attempted"] = True
            upload_result, upload_err = upload_media_all(token, app_token, resume_file_path)
            if upload_err:
                attachment_upload["error"] = upload_err
            else:
                attachment_upload["uploaded"] = True
                attachment_upload["file_token"] = upload_result["file_token"]
                fields["简历附件"] = [{"file_token": upload_result["file_token"]}]
    unexpected_generated_fields = sorted(set(fields) - ARCHIVE_FIELD_SET)
    if unexpected_generated_fields:
        print(json.dumps({
            "ok": False,
            "error": "skill 生成了非标准入档字段，已停止写入",
            "unexpected_generated_fields": unexpected_generated_fields,
            "expected_fields": ARCHIVE_FIELD_ORDER,
        }, ensure_ascii=False))
        sys.exit(1)
    fields_to_write, omitted_fields = filter_fields_for_table(fields, archive_field_names)
    if duplicate_record_id and args.duplicate_action == "update":
        r = update_record(token, app_token, archive["table_id"], duplicate_record_id, fields_to_write)
    else:
        r = append_record(token, app_token, archive["table_id"], fields_to_write)
    if r.get("code") != 0:
        print(json.dumps({
            "ok": False,
            "error": "追加记录失败",
            "detail": r,
            "fields_sent": fields_to_write,
            "expected_fields": ARCHIVE_FIELD_ORDER,
            "omitted_fields": omitted_fields,
            "field_meta_error": archive_fields_err,
            "schema_upgrade": schema_result,
        }, ensure_ascii=False))
        sys.exit(1)
    record_id = duplicate_record_id or record_id_from_write_response(r)
    print(json.dumps({
        "ok": True,
        "record_id": record_id,
        "updated_existing": bool(duplicate_record_id and args.duplicate_action == "update"),
        "url": config.get("archive_base", {}).get("url") or archive.get("url"),
        "used_legacy_schema": False,
        "archive_created_base": archive_result["created_base"],
        "archive_created_table": archive_result["created_table"],
        "archive_reused_existing_table": archive_result["reused_existing_table"],
        "used_legacy_archive": archive_result["used_legacy_archive"],
        "table_id": archive_result["table_id"],
        "duplicate_check": duplicate_check,
        "attachment_upload": attachment_upload,
        "omitted_fields": omitted_fields,
        "field_meta_error": archive_fields_err,
        "schema_upgrade": schema_result,
    }, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="飞书 JD 读写脚本（recruiter）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list-positions", help="列出所有岗位")
    p.set_defaults(func=cmd_list_positions)
    p = sub.add_parser("read-jd", help="读岗位 JD 全文")
    p.add_argument("--position", required=True)
    p.set_defaults(func=cmd_read_jd)
    p = sub.add_parser("create-archive", help="为岗位创建或复用入档表格")
    p.add_argument("--position", required=True)
    p.set_defaults(func=cmd_create_archive)
    p = sub.add_parser("upload-attachment", help="上传本地简历附件并返回 file_token")
    p.add_argument("--file", required=True, help="本地简历文件路径")
    p.add_argument("--app-token", help="入档 Base 的 app_token；默认使用 config.json 的 archive_base.app_token")
    p.add_argument("--parent-type", default="bitable_file", help="上传 parent_type，默认 bitable_file")
    p.set_defaults(func=cmd_upload_attachment)
    p = sub.add_parser("sync-archive-schema", help="为已有入档表补齐紧凑版字段")
    p.add_argument("--position", help="岗位名或position_id；不传时需使用 --all")
    p.add_argument("--all", action="store_true", help="为 position_id_map 中所有岗位补齐字段")
    p.set_defaults(func=cmd_sync_archive_schema)
    p = sub.add_parser("verify-archive", help="只读验证入档表记录；不创建/更新飞书记录")
    p.add_argument("--position", help="岗位名或position_id；不传则验证所有已配置岗位")
    p.add_argument("--candidate", help="按候选人、候选人Key、pending_id 或 record_id 过滤")
    p.add_argument("--limit", type=int, default=20, help="每个岗位最多返回多少条匹配记录；0 表示不限制")
    p.set_defaults(func=cmd_verify_archive)
    p = sub.add_parser("append-record", help="追加评分记录；缺少入档表格时自动创建")
    p.add_argument("--position", required=True)
    p.add_argument("--record", required=True, help="评分 JSON 文件路径")
    p.add_argument("--duplicate-action", choices=["skip", "create", "update"], default="skip",
                   help="发现同候选人同岗位已入档时的处理：skip=跳过写入（默认），create=强制新建，update=更新已有记录")
    p.set_defaults(func=cmd_append_record)
    args = parser.parse_args()
    if args.cmd == "sync-archive-schema" and not args.all and not args.position:
        parser.error("sync-archive-schema requires --position or --all")
    try:
        args.func(args)
    except Exception as e:
        print(json.dumps({
            "ok": False,
            "error": str(e),
            "exception": type(e).__name__,
        }, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
