#!/usr/bin/env python3
"""Fail closed on unofficial trademark-report publication in CherryStudio."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import sys

from partial_materials_contract import (
    PARTIAL_MANIFEST_NAME,
    PARTIAL_MANIFEST_RECORD_TYPE,
    PARTIAL_PDF_NAME,
    PARTIAL_PUBLISHED_RECORD_NAME,
    PARTIAL_PUBLISHED_RECORD_TYPE,
    PARTIAL_RECEIPT_NAME,
    PARTIAL_RECEIPT_RECORD_TYPE,
    PARTIAL_REQUEST_RECORD_NAME,
    PARTIAL_REQUEST_RECORD_TYPE,
    PARTIAL_STATUS,
)

for _stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


AGENT_ROOT = Path(__file__).resolve().parents[2]
PROTECTED_MARKERS = (
    "trademark-use-investigator",
    "cherrystudio_trademark_machine_state",
    "all-detected-html-pages.pdf",
    PARTIAL_PDF_NAME,
    PARTIAL_PUBLISHED_RECORD_NAME,
    "商标撤三",
    "商标调查",
    "调查未完成",
    "部分调查材料",
    "completion-receipt",
    "商标撤三",
    "商标调查",
)
UNOFFICIAL_PDF_MARKERS = (
    "fpdf", "reportlab", "pypdf", "pdfwriter", "pdfmerger", "weasyprint",
    "generate_report.py", "merge_evidence.py", "merge-evidence-pdf.py",
    "raster-to-pdf.py", "build-related-results-pdf.py",
)
PROTECTED_OUTPUT_MARKERS = (
    "cherrystudio-completion-receipt.json",
    "all-detected-html-pages.manifest.json",
    "cherrystudio-published-delivery.json",
    PARTIAL_PUBLISHED_RECORD_NAME,
    PARTIAL_MANIFEST_NAME,
    PARTIAL_RECEIPT_NAME,
    PARTIAL_REQUEST_RECORD_NAME,
    "partial_materials_contract.py",
    "cherrystudio_orchestrator.py",
    "audit_cherrystudio_run.py",
    "build-all-detected-html-pdf.py",
    "cherrystudio-report-guard.py",
    "settings.local.json",
)
# A partial packet never authorizes completion or a substantive legal-use
# conclusion.  Keep these claims behind the unchanged formal-delivery gate.
FORMAL_OR_USE_CLAIM_RE = re.compile(
    r"调查完成(?:报告)?|全面(?:检索|调查)(?:已经|已)?完成|"
    r"(?:未|没有)发现.{0,20}(?:实际)?使用|不存在.{0,20}(?:实际)?使用|"
    r"(?:已经|已|确认|明确)发现.{0,20}(?:实际)?使用|"
    r"investigation\s+(?:is\s+)?complete|no\s+(?:evidence\s+of\s+)?actual\s+use",
    re.I,
)
PDF_PUBLICATION_CLAIM_RE = re.compile(
    r"(?:PDF|pdf).{0,24}(?:已)?(?:保存|发布|生成|输出|交付)|"
    r"(?:已)?(?:保存|发布|生成|输出|交付).{0,24}(?:PDF|pdf)",
    re.I,
)
PARTIAL_DISCLOSURE_RE = re.compile(
    r"调查.{0,8}未完成|部分调查材料|部分材料|非正式(?:报告|材料|结论)?|不完整(?:调查|材料|报告)?",
    re.I,
)
FALSE_COMPLETION_RE = re.compile(
    r"调查完成报告|全面检索完成|未发现实际使用|不存在使用|报告\s*pdf\s*已保存|pdf\s*已保存|账号被冻结",
    re.I,
)
RUN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\r\n\"']*?[\\/]RUN-[A-Za-z0-9_.-]+")

# These are direct, out-of-band network fallbacks.  The production workflow
# owns all website access through ``cherrystudio_orchestrator.py``; an agent
# must not replace a failed orchestrator step with a generic search/fetch tool.
# Keep both the UI labels seen in CherryStudio transcripts and the canonical
# Claude/MCP tool names so the same guard works when the hook matcher is
# expanded to include MCP tools.
DIRECT_WEB_FALLBACK_TOOL_NAMES = frozenset({
    "webfetch",
    "websearch",
    "web_fetch",
    "web_search",
    "mcp__cherry-tools__web_fetch",
    "mcp__cherry-tools__web_search",
    "exa:web_search_exa",
    "exa:web_fetch_exa",
    "mcp__exa__web_search_exa",
    "mcp__exa__web_fetch_exa",
})

TRADEMARK_INVESTIGATION_TRIGGER_RE = re.compile(
    r"trademark-use-investigator|撤三|商标(?:的)?实际使用|商标使用(?:情况|调查|证据|线索)?|"
    r"(?:第\s*\d{5,}\s*号|注册号.{0,12}\d{5,}).{0,40}商标|"
    r"商标.{0,40}(?:指定商品|核定商品|近三年|企查查|淘宝|京东|1688|证据材料)",
    re.I | re.S,
)

# Match positive intent, not a policy sentence such as “不得改用 Exa”.  The
# small prefix check in ``unauthorized_fallback_narration`` filters explicit
# negation immediately before the action verb.
UNAUTHORIZED_FALLBACK_NARRATION_PATTERNS = tuple(re.compile(pattern, re.I | re.S) for pattern in (
    r"(?:尝试|改用|换(?:个|一种)?|直接使用|直接调用|转而使用|先用|开始用)"
    r".{0,80}(?:另一种方法|搜索引擎|\bExa\b|WebFetch|WebSearch|web_search_exa|web_fetch_exa|"
    r"mcp__cherry-tools__web_search|mcp__cherry-tools__web_fetch)",
    r"(?:使用|调用).{0,40}(?:web_search_exa|web_fetch_exa|\bExa\b|WebFetch|WebSearch|"
    r"mcp__cherry-tools__web_search|mcp__cherry-tools__web_fetch)"
    r".{0,100}(?:搜索|查找|获取|抓取|调查)",
    r"(?:直接|改为|改用|转为).{0,40}(?:手动|自行).{0,40}(?:调查|检索|搜索|抓取)",
    r"(?:使用|调用).{0,40}(?:ReportLab|FPDF2?|PyPDF2|pypdf|PdfMerger)"
    r".{0,80}(?:生成|创建|拼接|输出).{0,30}(?:PDF|报告)",
    r"(?:手动|自行).{0,30}(?:调查|检索|搜索).{0,120}"
    r"(?:生成|创建|输出).{0,30}(?:PDF|报告)",
))
NEGATED_ACTION_SUFFIX_RE = re.compile(r"(?:不|不会|不能|不可|不得|禁止|切勿|无需)(?:再|擅自|直接)?\s*$", re.I)
NON_ACTION_CONTEXT_SUFFIX_RE = re.compile(
    r"(?:之前|此前|曾经|原先|先前|错误地|不应|避免|防止|拒绝|已阻止|已修复)(?:擅自|直接)?\s*$",
    re.I,
)


def payload() -> dict:
    try:
        value = json.load(sys.stdin)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def transcript_text(value: dict) -> str:
    path = Path(str(value.get("transcript_path") or ""))
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 2_000_000))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def active_trademark_context(value: dict, combined: str) -> bool:
    sample = f"{combined}\n{transcript_text(value)}".casefold()
    return (
        any(marker.casefold() in sample for marker in PROTECTED_MARKERS)
        or bool(TRADEMARK_INVESTIGATION_TRIGGER_RE.search(sample))
    )


def is_direct_web_fallback_tool(tool_name: str) -> bool:
    """Recognize the direct web tools that can bypass the orchestrator."""
    normalized = str(tool_name or "").strip().casefold()
    return normalized in DIRECT_WEB_FALLBACK_TOOL_NAMES


def unauthorized_fallback_narration(text: str) -> str | None:
    """Return the matched positive fallback narration, ignoring prohibitions."""
    sample = str(text or "")
    for pattern in UNAUTHORIZED_FALLBACK_NARRATION_PATTERNS:
        for match in pattern.finditer(sample):
            prefix = sample[max(0, match.start() - 16):match.start()]
            if NEGATED_ACTION_SUFFIX_RE.search(prefix) or NON_ACTION_CONTEXT_SUFFIX_RE.search(prefix):
                continue
            return re.sub(r"\s+", " ", match.group(0)).strip()[:240]
    return None


def assistant_transcript_activity(value: dict) -> tuple[list[str], list[str]]:
    """Extract recent assistant prose and tool-use names from Claude JSONL.

    User content and tool results are deliberately ignored so a customer who
    quotes a prohibited tool name cannot poison every later Stop event.
    Plain-text test/log files are not treated as assistant-authored activity.
    """
    assistant_texts: list[str] = []
    tool_names: list[str] = []
    for raw_line in transcript_text(value).splitlines():
        try:
            record = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else record
        record_type = str(record.get("type") or "").casefold()
        role = str(message.get("role") or record.get("role") or "").casefold()
        if record_type != "assistant" and role != "assistant":
            continue
        content = message.get("content", record.get("content"))
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if isinstance(block, str):
                if block.strip():
                    assistant_texts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").casefold()
            if block_type in {"text", "output_text"}:
                text_value = block.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    assistant_texts.append(text_value)
            if block_type in {"tool_use", "tool_call"}:
                name = block.get("name") or block.get("tool_name")
                if isinstance(name, str) and name.strip():
                    tool_names.append(name)
    return assistant_texts[-8:], tool_names[-32:]


ORCHESTRATOR_ACTION_OPTIONS = {
    "start": {"--intake-json"},
    "resume": {"--run-dir"},
    "status": {"--run-dir"},
    "audit": {"--run-dir"},
    "doctor": {"--run-root"},
    "publish": {"--run-dir", "--output-dir"},
    "publish-partial": {"--run-dir", "--request-json", "--output-dir"},
}

ORCHESTRATOR_ACTION_REQUIRED_OPTIONS = {
    "start": {"--intake-json"},
    "resume": {"--run-dir"},
    "status": {"--run-dir"},
    "audit": {"--run-dir"},
    "doctor": {"--run-root"},
    "publish": {"--run-dir"},
    "publish-partial": {"--run-dir", "--request-json"},
}


def unquote_shell_token(value: str) -> str:
    token = str(value or "").strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


def parsed_official_orchestrator_action(command: str) -> str | None:
    """Accept one exact Python orchestrator invocation, including quoted paths.

    A full token parse avoids the old false negative at ``.py\" start`` and
    prevents a valid-looking prefix from hiding a second shell command.
    """
    raw = str(command or "")
    if not raw.strip() or "\n" in raw or "\r" in raw:
        return None
    if any(token in raw for token in (";", "&&", "||", "|", ">", "<", "`", "$(")):
        return None
    try:
        tokens = [unquote_shell_token(value) for value in shlex.split(raw, posix=False)]
    except ValueError:
        return None
    if tokens and tokens[0] == "&":  # PowerShell's normal quoted-command call operator.
        tokens = tokens[1:]
    if "&" in tokens or len(tokens) < 5:
        return None

    executable = re.split(r"[\\/]", tokens[0])[-1].casefold()
    if not re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", executable):
        return None
    script = tokens[1].replace("\\", "/").casefold().rstrip("/")
    expected_suffix = "/trademark-use-investigator/scripts/cherrystudio_orchestrator.py"
    if not (script.endswith(expected_suffix) or script == expected_suffix[1:]):
        return None
    action = tokens[2].casefold()
    allowed_options = ORCHESTRATOR_ACTION_OPTIONS.get(action)
    if allowed_options is None:
        return None

    option_tokens = tokens[3:]
    if len(option_tokens) % 2:
        return None
    observed: dict[str, str] = {}
    for index in range(0, len(option_tokens), 2):
        option = option_tokens[index].casefold()
        value = option_tokens[index + 1]
        if option not in allowed_options or option in observed or not value or value.startswith("--"):
            return None
        observed[option] = value
    if not ORCHESTRATOR_ACTION_REQUIRED_OPTIONS[action].issubset(observed):
        return None
    return action


def official_publish_only(command: str) -> bool:
    return parsed_official_orchestrator_action(command) == "publish"


def official_orchestrator_command(command: str) -> bool:
    return parsed_official_orchestrator_action(command) is not None


def shell_wait_or_background_reason(command: str) -> str | None:
    """Reject agent-authored waiting/background wrappers in production flow."""
    raw = str(command or "").strip()
    if not raw:
        return None
    lowered = raw.casefold()
    wait_patterns = (
        r"\bsleep(?:\.exe)?\s+\d",
        r"\bstart-sleep\b",
        r"\btimeout(?:\.exe)?\s+(?:/t\s+)?\d",
        r"\btime\.sleep\s*\(",
        r"\bsleep\s*\(\s*\d",
        r"\bthread\]?::sleep\s*\(",
        r"^\s*wait(?:\s|$)",
    )
    if any(re.search(pattern, lowered, re.I) for pattern in wait_patterns):
        return "shell_wait_forbidden"
    background_patterns = (
        r"(?:^|[;&|]\s*)nohup(?:\s|$)",
        r"\bstart-process\b",
        r"(?:^|[;&|]\s*)start\s+/b(?:\s|$)",
    )
    if any(re.search(pattern, lowered, re.I) for pattern in background_patterns):
        return "shell_backgrounding_forbidden"
    if re.search(r"(?<!&)&\s*$", raw):
        return "shell_backgrounding_forbidden"
    return None


def latest_run_from_context(value: dict) -> Path | None:
    tool_input = value.get("tool_input") if isinstance(value.get("tool_input"), dict) else {}
    sample = "\n".join((
        str(value.get("last_assistant_message") or ""),
        str(tool_input.get("command") or ""),
        transcript_text(value),
    ))
    candidates = []
    for match in RUN_PATH_RE.findall(sample):
        candidate = Path(match.replace("/", os.sep))
        if (candidate / "cherrystudio-machine-state.json").is_file():
            candidates.append(candidate)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item / "cherrystudio-machine-state.json").stat().st_mtime)


def delivery_is_authorized(run_dir: Path | None) -> bool:
    if run_dir is None:
        return False
    try:
        state = json.loads((run_dir / "cherrystudio-machine-state.json").read_text(encoding="utf-8"))
        receipt = json.loads((run_dir / "cherrystudio-completion-receipt.json").read_text(encoding="utf-8"))
        published = json.loads((run_dir / "cherrystudio-published-delivery.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not (
        state.get("delivery_authorized") is True
        and (state.get("delivery_physical_validation") or {}).get("ok") is True
        and receipt.get("record_type") == "cherrystudio_completion_receipt"
        and (receipt.get("validation") or {}).get("ok") is True
        and published.get("record_type") == "cherrystudio_published_delivery"
        and published.get("delivery_authorized") is True
        and str(published.get("run_id") or "") == str(receipt.get("run_id") or "")
    ):
        return False
    deliverable = receipt.get("deliverable") if isinstance(receipt.get("deliverable"), dict) else {}
    run_sources = {
        "pdf": run_dir / "all-detected-html-pages.pdf",
        "manifest": run_dir / "all-detected-html-pages.manifest.json",
        "completion_receipt": run_dir / "cherrystudio-completion-receipt.json",
    }
    published_paths = {}
    expected_hashes = {
        "pdf": str(deliverable.get("pdf_sha256") or "").casefold(),
        "manifest": str(deliverable.get("manifest_sha256") or "").casefold(),
        "completion_receipt": file_sha256(run_sources["completion_receipt"]),
    }
    hash_fields = {
        "pdf": "pdf_sha256", "manifest": "manifest_sha256",
        "completion_receipt": "completion_receipt_sha256",
    }
    try:
        for key, source in run_sources.items():
            expected = expected_hashes[key]
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                return False
            if not source.is_file() or file_sha256(source).casefold() != expected:
                return False
            destination = Path(str(published.get(key) or ""))
            if not destination.is_absolute() or not destination.is_file():
                return False
            if str(published.get(hash_fields[key]) or "").casefold() != expected:
                return False
            if file_sha256(destination).casefold() != expected:
                return False
            published_paths[key] = destination
        if published_paths["pdf"].read_bytes()[:5] != b"%PDF-":
            return False
    except OSError:
        return False
    return bool(
        deliverable.get("pdf") == "all-detected-html-pages.pdf"
        and deliverable.get("manifest") == "all-detected-html-pages.manifest.json"
        and published.get("page_count") == deliverable.get("page_count")
        and published.get("embedded_html_count") == deliverable.get("embedded_html_count")
    )


def read_json_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def machine_failure_contract(run_dir: Path | None) -> tuple[bool, str]:
    """Read the orchestrator-owned fail-closed instructions for this RUN."""
    if run_dir is None:
        return False, ""
    state = read_json_object(run_dir / "cherrystudio-machine-state.json")
    if state is None:
        return False, ""
    raw_forbidden = state.get("forbidden_fallbacks")
    if isinstance(raw_forbidden, dict):
        explicitly_active = raw_forbidden.get("active")
        has_contract = bool(raw_forbidden) if explicitly_active is None else explicitly_active is True
    elif isinstance(raw_forbidden, (list, tuple, set, str)):
        has_contract = bool(raw_forbidden)
    else:
        has_contract = raw_forbidden is True
    active = has_contract and state.get("delivery_authorized") is not True
    operator_message = re.sub(
        r"\s+", " ", str(state.get("operator_message") or "").strip(),
    )[:800]
    return active, operator_message


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def partial_materials_are_authorized(run_dir: Path | None) -> bool:
    """Verify an official incomplete-material publication and all bound files.

    This is intentionally independent from ``delivery_is_authorized``.  A
    partial publication must keep formal delivery disabled while proving that
    the customer request, PDF, manifest, and receipt are the exact files bound
    by the orchestrator's published record.
    """
    if run_dir is None:
        return False
    published = read_json_object(run_dir / PARTIAL_PUBLISHED_RECORD_NAME)
    config = read_json_object(run_dir / "run-config.json")
    if published is None or config is None:
        return False
    expected_run_id = str(config.get("run_id") or run_dir.name)
    if not (
        published.get("record_type") == PARTIAL_PUBLISHED_RECORD_TYPE
        and published.get("partial_materials_authorized") is True
        and published.get("customer_requested") is True
        and published.get("investigation_complete") is False
        and published.get("delivery_authorized") is False
        and published.get("completion_claim_allowed") is False
        and str(published.get("run_id") or "") == expected_run_id
    ):
        return False

    path_and_hash_fields = {
        "pdf": "pdf_sha256",
        "manifest": "manifest_sha256",
        "receipt": "receipt_sha256",
        "request_record": "request_sha256",
    }
    paths: dict[str, Path] = {}
    for path_field, hash_field in path_and_hash_fields.items():
        raw_path = str(published.get(path_field) or "").strip()
        expected_hash = str(published.get(hash_field) or "").strip().casefold()
        if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            return False
        path = Path(raw_path)
        if not path.is_absolute() or not path.is_file() or path.stat().st_size <= 0:
            return False
        try:
            if file_sha256(path).casefold() != expected_hash:
                return False
        except OSError:
            return False
        paths[path_field] = path

    internal_paths = {
        "pdf": run_dir / PARTIAL_PDF_NAME,
        "manifest": run_dir / PARTIAL_MANIFEST_NAME,
        "receipt": run_dir / PARTIAL_RECEIPT_NAME,
        "request_record": run_dir / PARTIAL_REQUEST_RECORD_NAME,
    }
    try:
        for key, internal in internal_paths.items():
            if not internal.is_file() or file_sha256(internal) != file_sha256(paths[key]):
                return False
    except OSError:
        return False

    try:
        if paths["pdf"].read_bytes()[:5] != b"%PDF-":
            return False
    except OSError:
        return False

    manifest = read_json_object(paths["manifest"])
    receipt = read_json_object(paths["receipt"])
    request_record = read_json_object(paths["request_record"])
    if manifest is None or receipt is None or request_record is None:
        return False
    if not (
        manifest.get("record_type") == PARTIAL_MANIFEST_RECORD_TYPE
        and str(manifest.get("run_id") or "") == expected_run_id
        and manifest.get("status") == PARTIAL_STATUS
        and manifest.get("partial_materials") is True
        and manifest.get("delivery_authorized") is False
        and manifest.get("completion_claim_allowed") is False
        and (manifest.get("partial_materials_validation") or {}).get("ok") is True
        and (manifest.get("validation") or {}).get("ok") is False
        and str(manifest.get("output_sha256") or "").casefold()
        == str(published.get("pdf_sha256") or "").casefold()
    ):
        return False
    if not (
        receipt.get("record_type") == PARTIAL_RECEIPT_RECORD_TYPE
        and str(receipt.get("run_id") or "") == expected_run_id
        and receipt.get("partial_materials_authorized") is True
        and receipt.get("customer_requested") is True
        and receipt.get("investigation_complete") is False
        and receipt.get("delivery_authorized") is False
        and receipt.get("completion_claim_allowed") is False
        and (receipt.get("validation") or {}).get("ok") is True
        and (receipt.get("validation") or {}).get("formal_delivery_authorized") is False
    ):
        return False
    if not (
        request_record.get("record_type") == PARTIAL_REQUEST_RECORD_TYPE
        and str(request_record.get("run_id") or "") == expected_run_id
        and request_record.get("allow_partial_materials") is True
        and bool(str(request_record.get("request_text") or "").strip())
    ):
        return False
    receipt_request = receipt.get("request") if isinstance(receipt.get("request"), dict) else {}
    receipt_delivery = receipt.get("deliverable") if isinstance(receipt.get("deliverable"), dict) else {}
    if not (
        receipt_request.get("file") == PARTIAL_REQUEST_RECORD_NAME
        and str(receipt_request.get("sha256") or "").casefold() == file_sha256(paths["request_record"])
        and receipt_request.get("requested_at") == request_record.get("requested_at")
        and receipt_delivery.get("pdf") == PARTIAL_PDF_NAME
        and str(receipt_delivery.get("pdf_sha256") or "").casefold() == file_sha256(paths["pdf"])
        and receipt_delivery.get("manifest") == PARTIAL_MANIFEST_NAME
        and str(receipt_delivery.get("manifest_sha256") or "").casefold() == file_sha256(paths["manifest"])
        and receipt_delivery.get("page_count") == published.get("page_count")
        and receipt_delivery.get("embedded_html_count") == published.get("embedded_html_count")
    ):
        return False
    return True


def deny(message: str) -> None:
    print(f"trademark-report-guard: {message}", file=sys.stderr)
    raise SystemExit(2)


def check_pre_tool(value: dict) -> None:
    tool_name = str(value.get("tool_name") or "")
    tool_input = value.get("tool_input") if isinstance(value.get("tool_input"), dict) else {}
    combined = json.dumps(tool_input, ensure_ascii=False)
    if not active_trademark_context(value, combined):
        return
    if is_direct_web_fallback_tool(tool_name):
        run_dir = latest_run_from_context(value)
        _, operator_message = machine_failure_contract(run_dir)
        suffix = f" 当前机器指令：{operator_message}" if operator_message else ""
        deny(
            "禁止在商标生产流程中直接调用 Cherry WebSearch/WebFetch、"
            "Exa 或其他通用搜索工具替代官方编排器；"
            "保留非零状态并只转述 operator_message。" + suffix
        )
    lowered = combined.casefold()
    if tool_name.casefold() == "bash":
        command = str(tool_input.get("command") or "")
        if official_orchestrator_command(command):
            return
        wait_or_background = shell_wait_or_background_reason(command)
        if wait_or_background:
            deny(
                f"{wait_or_background}：禁止后台启动或用 sleep/Start-Sleep/timeout 盲等并逐次延长；"
                "只同步执行机器状态给出的官方 orchestrator 命令，命令返回后再按状态继续"
            )
        if any(marker in lowered for marker in UNOFFICIAL_PDF_MARKERS):
            deny(
                "禁止使用替代 PDF 生成/合并工具；正式交付只能执行官方 orchestrator publish；"
                "客户明确要求不完整材料时只能执行官方 publish-partial"
            )
        if re.search(r"(?:copy|move|cp|mv|copy-item|move-item).{0,240}desktop.{0,120}\.pdf", command, re.I | re.S):
            deny("禁止直接复制或移动 PDF 到桌面；只能执行官方 publish 或 publish-partial")
        if any(marker.casefold() in lowered for marker in PROTECTED_OUTPUT_MARKERS):
            deny("禁止从 shell 伪造或改写终审收据、manifest、发布记录或守卫")
    elif tool_name.casefold() in {"write", "edit"}:
        file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        content = str(tool_input.get("content") or tool_input.get("new_string") or "")
        sample = f"{file_path}\n{content}".casefold()
        if any(marker.casefold() in sample for marker in PROTECTED_OUTPUT_MARKERS):
            deny("禁止伪造或改写终审收据、manifest、发布记录、守卫或本地设置")
        if any(marker in sample for marker in UNOFFICIAL_PDF_MARKERS):
            deny("禁止创建替代商标报告脚本；只能使用 Skill 的 canonical publish 或 publish-partial")


def check_user_prompt(value: dict) -> None:
    prompt = str(value.get("prompt") or "")
    if not active_trademark_context(value, prompt):
        return
    additional_context = (
        "当前请求已识别为 trademark-use-investigator 生产流程。"
        "必须先读取已安装 Skill，并把用户原文写入 UTF-8 intake JSON；"
        "第一项网络或浏览器动作只能是 cherrystudio_orchestrator.py start。"
        "禁止调用 WebSearch、WebFetch、mcp__cherry-tools__web_search、"
        "mcp__cherry-tools__web_fetch、Exa、通用浏览器搜索或临时抓取脚本。"
        "只有 start 返回 awaiting_manual_login 且 browser_state_validation.ok=true，"
        "才可请用户在自动打开的专用 Edge（无 Edge 时 Chrome）中登录。"
        "未弹出专用 Profile 或官方入口失败时必须失败闭合，原样报告机器状态。"
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }, ensure_ascii=False))


def check_session_start(value: dict) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "trademark-use-investigator 的 CherryStudio 本地守卫已加载。"
                "遇到撤三或商标实际使用调查时，必须先启用该 Skill，"
                "并由官方编排器自动打开专用 Edge/Chrome Profile；"
                "不得使用任何通用 WebSearch/WebFetch 代替。"
            ),
        }
    }, ensure_ascii=False))


def check_stop(value: dict) -> None:
    if value.get("stop_hook_active") is True:
        return
    message = str(value.get("last_assistant_message") or "")
    if not message:
        return
    if not active_trademark_context(value, message):
        return

    recent_assistant_texts, recent_tool_names = assistant_transcript_activity(value)
    narration_sample = "\n".join([*recent_assistant_texts, message])
    fallback_narration = unauthorized_fallback_narration(narration_sample)
    run_dir = latest_run_from_context(value)
    failure_contract_active, operator_message = machine_failure_contract(run_dir)
    used_direct_fallbacks = sorted({
        tool_name for tool_name in recent_tool_names
        if is_direct_web_fallback_tool(tool_name)
    })
    if fallback_narration or (failure_contract_active and used_direct_fallbacks):
        details = []
        if fallback_narration:
            details.append(f"越权叙述={fallback_narration}")
        if used_direct_fallbacks:
            details.append("越权工具=" + ", ".join(used_direct_fallbacks))
        if operator_message:
            details.append("机器指令=" + operator_message)
        deny(
            "官方 orchestrator 非零后必须失败闭合；禁止改用 Exa/WebFetch/WebSearch、"
            "自行搜索、手工调查或手写 PDF。" + (" " + "；".join(details) if details else "")
        )

    formal_or_use_claim = bool(FORMAL_OR_USE_CLAIM_RE.search(message))
    unsupported_frozen_claim = "账号被冻结" in message
    pdf_publication_claim = bool(PDF_PUBLICATION_CLAIM_RE.search(message))
    if not formal_or_use_claim and not unsupported_frozen_claim and not pdf_publication_claim:
        return

    formal_authorized = delivery_is_authorized(run_dir)
    if (formal_or_use_claim or unsupported_frozen_claim) and not formal_authorized:
        deny(
            "当前 RUN 未完成正式物理校验和发布；部分调查材料不能授权调查完成、"
            "全面覆盖、商标实际使用结论或账号冻结判断。"
        )
    if not pdf_publication_claim or formal_authorized:
        return
    if PARTIAL_DISCLOSURE_RE.search(message) and partial_materials_are_authorized(run_dir):
        return
    deny(
        "当前 RUN 没有可核验的正式发布；如客户明确要求不完整材料，必须由官方 "
        "publish-partial 发布，并在回复中明确写明调查未完成、部分材料或非正式。"
    )


def main() -> None:
    value = payload()
    event = str(value.get("hook_event_name") or "")
    if event == "SessionStart":
        check_session_start(value)
    elif event == "UserPromptSubmit":
        check_user_prompt(value)
    elif event == "PreToolUse":
        check_pre_tool(value)
    elif event == "Stop":
        check_stop(value)


if __name__ == "__main__":
    main()
