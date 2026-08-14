#!/usr/bin/env python3

"""Run a localhost workstation for human navigation and one-click page archiving."""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from process_utils import run_bounded
import threading
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edge_profile import (
    browser_profile_process_is_running,
    dedicated_browser_user_data,
    is_default_browser_user_data,
    resolve_browser_selection,
)
from instant_data_import import decode_upload, import_export_bytes
from sales_platforms import allowed_url


ARTIFACT_SPECS = {
    "visible_png": ("visible.png", 30 * 1024 * 1024),
    "fullpage_png": ("fullpage.png", 100 * 1024 * 1024),
    "pdf": ("page.pdf", 100 * 1024 * 1024),
    "mhtml": ("page.mhtml", 150 * 1024 * 1024),
    "dom_html": ("rendered-dom.html", 60 * 1024 * 1024),
    "body_text": ("body-text.txt", 15 * 1024 * 1024),
}
REQUIRED_PAGE_ARTIFACTS = frozenset(ARTIFACT_SPECS)
FINAL_CAPTURE_KINDS = {"result", "zero"}
ALLOWED_CAPTURE_KINDS = FINAL_CAPTURE_KINDS | {"detail", "blocked"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def file_record(path: Path, base: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(path.relative_to(base)).replace("\\", "/"),
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def decode_artifact(value: str, maximum: int) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("Artifact payload is empty")
    if value.startswith("data:"):
        marker = value.find(",")
        if marker < 0:
            raise ValueError("Malformed data URL")
        value = value[marker + 1:]
    estimated = (len(value) * 3) // 4
    if estimated > maximum:
        raise ValueError(f"Artifact exceeds {maximum} bytes")
    decoded = base64.b64decode(value, validate=True)
    if not decoded or len(decoded) > maximum:
        raise ValueError(f"Artifact is empty or exceeds {maximum} bytes")
    return decoded


def capture_foreground_window(output: Path) -> dict:
    """Capture the active top-level window, including browser chrome and address bar."""
    if sys.platform != "win32":
        raise RuntimeError("Foreground-window capture is only supported on Windows")
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise RuntimeError("Pillow ImageGrab is required") from exc

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError("No foreground window is available")
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect failed")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < 200 or height < 150:
        raise RuntimeError("Foreground window is too small to be an evidence capture")
    title_length = user32.GetWindowTextLengthW(hwnd)
    title_buffer = ctypes.create_unicode_buffer(max(1, title_length + 1))
    user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
    class_buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
    image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG")
    return {
        "window_title": title_buffer.value,
        "window_class": class_buffer.value,
        "window_bounds": [rect.left, rect.top, rect.right, rect.bottom],
        "width": width,
        "height": height,
    }


class Workstation:
    def __init__(self, run_dir: Path, foreground_capture=capture_foreground_window):
        self.run_dir = run_dir.resolve()
        self.queue_path = self.run_dir / "discovery" / "manual-capture-queue.json"
        self.state_path = self.run_dir / "discovery" / "manual-capture-state.json"
        self.foreground_capture = foreground_capture
        self.lock = threading.RLock()
        if not self.queue_path.is_file() or not self.state_path.is_file():
            raise FileNotFoundError("Build discovery/manual-capture-queue.json before starting the workstation")

    def _load(self) -> tuple[dict, dict]:
        queue = read_json(self.queue_path)
        state = read_json(self.state_path)
        if not isinstance(queue, dict) or not isinstance(state, dict):
            raise ValueError("Manual capture queue/state is invalid")
        return queue, state

    @staticmethod
    def _task_map(queue: dict) -> dict[str, dict]:
        return {str(item.get("task_id")): item for item in queue.get("items") or []}

    @staticmethod
    def _next_pending(queue: dict, after_order: int = 0) -> dict | None:
        items = sorted(queue.get("items") or [], key=lambda item: int(item.get("order") or 0))
        for item in items:
            if int(item.get("order") or 0) > after_order and item.get("status") == "pending":
                return item
        for item in items:
            if item.get("status") == "pending":
                return item
        return None

    @staticmethod
    def _refresh_state(queue: dict, state: dict, after_order: int = 0) -> None:
        items = queue.get("items") or []
        next_item = Workstation._next_pending(queue, after_order)
        completed = sum(item.get("status") in FINAL_CAPTURE_KINDS for item in items)
        blocked = sum(item.get("status") == "blocked" for item in items)
        pending = sum(item.get("status") == "pending" for item in items)
        state.update({
            "updated_at": utc_now(),
            "current_task_id": next_item.get("task_id") if next_item else None,
            "completed_count": completed,
            "blocked_count": blocked,
            "pending_count": pending,
            "phase": "complete" if not pending and not blocked else (
                "complete_with_gaps" if not pending else "running"
            ),
        })

    def status(self) -> dict:
        with self.lock:
            queue, state = self._load()
            by_id = self._task_map(queue)
            current = by_id.get(str(state.get("current_task_id") or ""))
            if current is None or current.get("status") != "pending":
                current = self._next_pending(queue)
                state["current_task_id"] = current.get("task_id") if current else None
                atomic_write_json(self.state_path, state)
            items = queue.get("items") or []
            export_batch_count = sum(len(item.get("data_exports") or []) for item in items)
            export_item_count = sum(
                sum(int(record.get("item_count") or 0) for record in item.get("data_exports") or [])
                for item in items
            )
            return {
                "ok": True,
                "run_id": queue.get("run_id"),
                "phase": state.get("phase"),
                "task_count": queue.get("task_count"),
                "platform_count": queue.get("platform_count"),
                "continuous_pages_required": False,
                "completed_count": state.get("completed_count", 0),
                "blocked_count": state.get("blocked_count", 0),
                "pending_count": state.get("pending_count", queue.get("task_count", 0)),
                "structured_export_batch_count": export_batch_count,
                "structured_export_item_count": export_item_count,
                "current_task": current,
                "next_blocked_task": next(
                    (item for item in sorted(queue.get("items") or [], key=lambda value: int(value.get("order") or 0))
                     if item.get("status") == "blocked"),
                    None,
                ),
            }

    def import_structured_export(self, payload: dict) -> dict:
        """Attach a local CSV/XLSX export to a task without completing that task."""
        with self.lock:
            task_id = str(payload.get("task_id") or "").strip() or None
            filename = str(payload.get("filename") or "").strip()
            data = decode_upload(payload.get("content_base64"))
            manifest = import_export_bytes(self.run_dir, data, filename, task_id=task_id)
            response = self.status()
            response.update({
                "structured_export_imported": True,
                "duplicate_import": bool(manifest.get("duplicate_import")),
                "import_id": manifest.get("import_id"),
                "task_id": manifest.get("task_id"),
                "normalized_item_count": manifest.get("normalized_item_count", 0),
                "rejected_row_count": manifest.get("rejected_row_count", 0),
                "manifest": manifest.get("manifest_path"),
                "candidate_catalog": manifest.get("candidate_catalog"),
            })
            return response

    def reopen_task(self, task_id: str) -> dict:
        with self.lock:
            queue, state = self._load()
            task = self._task_map(queue).get(task_id)
            if task is None:
                raise ValueError(f"Unknown task_id: {task_id}")
            task["status"] = "pending"
            task["reopened_at"] = utc_now()
            state["current_task_id"] = task_id
            self._refresh_state(queue, state, int(task.get("order") or 1) - 1)
            queue["updated_at"] = utc_now()
            atomic_write_json(self.queue_path, queue)
            atomic_write_json(self.state_path, state)
            return self.status()

    def save_capture(self, payload: dict) -> dict:
        with self.lock:
            queue, state = self._load()
            task_id = str(payload.get("task_id") or "").strip()
            capture_kind = str(payload.get("capture_kind") or "").strip().lower()
            task = self._task_map(queue).get(task_id)
            if task is None:
                raise ValueError(f"Unknown task_id: {task_id}")
            if capture_kind not in ALLOWED_CAPTURE_KINDS:
                raise ValueError(f"Unsupported capture_kind: {capture_kind}")
            url = str(payload.get("url") or "").strip()
            parsed = urlsplit(url)
            domain_ok = parsed.scheme in {"http", "https"} and allowed_url(url, task.get("allowed_domains") or [])
            artifact_payloads = payload.get("artifacts") or {}
            if not isinstance(artifact_payloads, dict):
                raise ValueError("artifacts must be an object")
            missing = sorted(REQUIRED_PAGE_ARTIFACTS - set(artifact_payloads))
            page_capture_kind = capture_kind in FINAL_CAPTURE_KINDS or capture_kind == "detail"
            structurally_valid = domain_ok and (not page_capture_kind or not missing)

            now = utc_now()
            capture_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            if structurally_valid and capture_kind in FINAL_CAPTURE_KINDS:
                base_dir = self.run_dir / "manual-capture" / "pages" / task_id / capture_id
            elif structurally_valid and capture_kind == "detail":
                base_dir = self.run_dir / "manual-capture" / "details" / task_id / capture_id
            else:
                base_dir = self.run_dir / "capture-diagnostics" / "manual-capture" / task_id / capture_id
            base_dir.mkdir(parents=True, exist_ok=False)

            artifact_records = {}
            decode_errors = []
            for key, (filename, maximum) in ARTIFACT_SPECS.items():
                value = artifact_payloads.get(key)
                if value is None:
                    continue
                try:
                    output = base_dir / filename
                    output.write_bytes(decode_artifact(value, maximum))
                    artifact_records[key] = file_record(output, base_dir)
                except Exception as exc:
                    decode_errors.append(f"{key}: {exc}")

            foreground = None
            foreground_error = None
            try:
                browser_window = base_dir / "browser-window.png"
                foreground = self.foreground_capture(browser_window)
                artifact_records["browser_window_png"] = file_record(browser_window, base_dir)
            except Exception as exc:
                foreground_error = str(exc)

            if page_capture_kind:
                missing_after_write = sorted(REQUIRED_PAGE_ARTIFACTS - set(artifact_records))
            else:
                missing_after_write = []
            accepted = bool(
                structurally_valid
                and not decode_errors
                and not missing_after_write
                and foreground is not None
                and capture_kind in (FINAL_CAPTURE_KINDS | {"detail"})
            )
            if not accepted and not base_dir.is_relative_to(self.run_dir / "capture-diagnostics"):
                diagnostic = self.run_dir / "capture-diagnostics" / "manual-capture" / task_id / capture_id
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                base_dir.replace(diagnostic)
                base_dir = diagnostic
                artifact_records = {
                    key: file_record(base_dir / record["path"], base_dir)
                    for key, record in artifact_records.items()
                    if (base_dir / record["path"]).is_file()
                }

            metadata = {
                "schema_version": "1.0",
                "record_type": "manual_sales_page_capture",
                "run_id": queue.get("run_id"),
                "capture_id": capture_id,
                "task_id": task_id,
                "task_order": task.get("order"),
                "platform": task.get("platform"),
                "platform_label": task.get("platform_label"),
                "query": task.get("query"),
                "target_good": task.get("target_good"),
                "capture_kind": capture_kind,
                "page_state": {
                    "result": "normal_search_results",
                    "zero": "explicit_zero_results",
                    "detail": "manual_product_detail",
                    "blocked": "blocked_or_verification",
                }[capture_kind],
                "captured_at": str(payload.get("captured_at") or now),
                "received_at": now,
                "url": url,
                "title": str(payload.get("title") or "")[:1000],
                "note": str(payload.get("note") or "")[:4000],
                "allowed_domains": task.get("allowed_domains") or [],
                "domain_ok": domain_ok,
                "human_navigation": True,
                "webdriver_navigation": False,
                "cookie_exported": False,
                "continuous_pages_required": False,
                "accepted": accepted,
                "missing_payload_artifacts": missing,
                "missing_written_artifacts": missing_after_write,
                "decode_errors": decode_errors,
                "foreground_window": foreground,
                "foreground_capture_error": foreground_error,
                "extension_warnings": payload.get("warnings") or [],
                "artifacts": artifact_records,
            }
            metadata_path = base_dir / "metadata.json"
            atomic_write_json(metadata_path, metadata)
            hashes = {path.name: file_record(path, base_dir) for path in base_dir.iterdir() if path.is_file()}
            atomic_write_json(base_dir / "hashes.json", {
                "schema_version": "1.0",
                "generated_at": utc_now(),
                "files": hashes,
            })

            task["attempt_count"] = int(task.get("attempt_count") or 0) + 1
            relative = str(base_dir.relative_to(self.run_dir)).replace("\\", "/")
            task.setdefault("captures", []).append({
                "capture_id": capture_id,
                "capture_kind": capture_kind,
                "accepted": accepted,
                "path": relative,
                "captured_at": metadata["captured_at"],
                "url": url,
            })
            if accepted and capture_kind in FINAL_CAPTURE_KINDS:
                task["status"] = capture_kind
                task["completed_at"] = now
            elif capture_kind == "blocked":
                task["status"] = "blocked"
                task["blocked_at"] = now
            task["last_capture_path"] = relative
            task["last_capture_accepted"] = accepted
            queue["updated_at"] = now
            self._refresh_state(queue, state, int(task.get("order") or 0))
            atomic_write_json(self.queue_path, queue)
            atomic_write_json(self.state_path, state)
            response = self.status()
            response.update({
                "capture_saved": True,
                "capture_id": capture_id,
                "capture_kind": capture_kind,
                "accepted": accepted,
                "path": relative,
                "errors": [*decode_errors, *([foreground_error] if foreground_error else []), *missing_after_write],
            })
            return response


def dashboard_html() -> bytes:
    return """<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>商标人工固证工作台</title><style>
body{font-family:system-ui,'Microsoft YaHei',sans-serif;background:#f4f6f8;color:#17202a;margin:0}
main{max-width:900px;margin:36px auto;padding:0 20px}.card{background:#fff;border-radius:14px;padding:24px;box-shadow:0 5px 24px #1f29371a}
h1{margin-top:0}.muted{color:#667085}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stat{background:#f8fafc;padding:14px;border-radius:10px}
code{word-break:break-all}button{border:0;border-radius:8px;padding:10px 16px;background:#2563eb;color:#fff;cursor:pointer;margin:4px 8px 4px 0}
input[type=file]{display:block;margin:8px 0 12px;max-width:100%}.lead-box{margin-top:20px;padding:16px;border:1px solid #d0d5dd;border-radius:10px;background:#f9fafb}
button.secondary{background:#475467}@media(max-width:650px){.grid{grid-template-columns:1fr}}</style>
<main><div class=\"card\"><h1>商标人工固证工作台</h1><p class=\"muted\">保持此服务运行；在本次所选的专用 Edge/Chrome 中用扩展打开任务和保存当前页。</p>
<div class=\"grid\"><div class=\"stat\">已完成<br><strong id=\"done\">-</strong></div><div class=\"stat\">待处理<br><strong id=\"pending\">-</strong></div><div class=\"stat\">阻断<br><strong id=\"blocked\">-</strong></div><div class=\"stat\">结构化线索<br><strong id=\"exports\">-</strong></div></div>
<h2 id=\"task\">正在读取任务…</h2><p>查询词：<code id=\"query\">-</code></p><p>核定商品：<span id=\"good\">-</span></p>
<button onclick=\"copyQuery()\">复制查询词</button><button id=\"retry\" class=\"secondary\" onclick=\"retryBlocked()\" hidden>重试阻断任务</button><button class=\"secondary\" onclick=\"refresh()\">刷新</button>
<div class=\"lead-box\"><strong>导入免费采集工具的当前页结果</strong><p class=\"muted\">在 Instant Data Scraper 中导出 CSV/XLSX 后，选择文件并绑定到当前任务。导出仅作为筛查线索，不会把任务标为完成；结果页仍须点击“采集当前页”固证。</p><input id=\"exportFile\" type=\"file\" accept=\".csv,.xlsx\"><button id=\"importButton\" onclick=\"importExport()\">导入当前任务 CSV/XLSX</button><span id=\"taskExports\" class=\"muted\"></span></div>
<p id=\"message\" class=\"muted\"></p></div></main>
<script>let current=null;async function refresh(){const r=await fetch('/api/status');const s=await r.json();current=s.current_task;
done.textContent=s.completed_count+'/'+s.task_count;pending.textContent=s.pending_count;blocked.textContent=s.blocked_count;
exports.textContent=s.structured_export_batch_count+'批 / '+s.structured_export_item_count+'条';
task.textContent=current?current.task_id+' · '+current.platform_label:(s.next_blocked_task?'基础任务已轮询完，请重试阻断项':'全部基础任务已处理');query.textContent=current?current.query:'-';good.textContent=current?(current.target_good||'仅商标名'):'-';
taskExports.textContent=current?('当前任务已导入 '+(current.structured_export_batch_count||0)+' 批 / '+(current.structured_export_item_count||0)+' 条'):'';
retry.hidden=!!current||!s.next_blocked_task;retry.dataset.task=s.next_blocked_task?s.next_blocked_task.task_id:'';}
async function copyQuery(){if(!current)return;await navigator.clipboard.writeText(current.query);message.textContent='查询词已复制';}
async function importExport(){if(!current){message.textContent='当前没有待处理任务；请先重开需要绑定的任务';return;}const f=exportFile.files[0];if(!f){message.textContent='请先选择 CSV 或 XLSX 文件';return;}importButton.disabled=true;message.textContent='正在本地导入、去重并计算哈希…';try{const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',',2)[1]);reader.onerror=()=>reject(reader.error);reader.readAsDataURL(f);});const r=await fetch('/api/import-export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:current.task_id,filename:f.name,content_base64:data})});const s=await r.json();if(!r.ok)throw new Error(s.error||'导入失败');message.textContent=s.duplicate_import?'此文件已导入，未重复写入':('已导入 '+s.normalized_item_count+' 条线索，拒绝 '+s.rejected_row_count+' 行');exportFile.value='';await refresh();}catch(e){message.textContent='导入失败：'+e.message;}finally{importButton.disabled=false;}}
async function retryBlocked(){const id=retry.dataset.task;if(!id)return;await fetch('/api/reopen',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:id})});await refresh();}refresh();setInterval(refresh,3000);</script></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    workstation: Workstation
    max_request_bytes = 360 * 1024 * 1024

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin", "")
        return not origin or origin.startswith("chrome-extension://") or origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")

    def _send_json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0 or length > self.max_request_bytes:
            raise ValueError("Request body is empty or too large")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_OPTIONS(self):
        if not self._origin_allowed():
            self.send_error(403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "null"))
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        if not self._origin_allowed():
            self.send_error(403)
            return
        if self.path == "/" or self.path.startswith("/index.html"):
            body = dashboard_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/status"):
            self._send_json(200, self.workstation.status())
            return
        self.send_error(404)

    def do_POST(self):
        if not self._origin_allowed():
            self._send_json(403, {"ok": False, "error": "Origin is not allowed"})
            return
        try:
            payload = self._read_json()
            if self.path == "/api/capture":
                result = self.workstation.save_capture(payload)
            elif self.path == "/api/import-export":
                result = self.workstation.import_structured_export(payload)
            elif self.path == "/api/reopen":
                result = self.workstation.reopen_task(str(payload.get("task_id") or ""))
            else:
                self._send_json(404, {"ok": False, "error": "Unknown endpoint"})
                return
            self._send_json(200, result)
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})

    def log_message(self, format, *args):
        sys.stderr.write("manual-workstation: " + (format % args) + "\n")


def launch_browser(browser: str, executable: Path, user_data: Path, profile: str, dashboard_url: str) -> int:
    browser = str(browser or "").strip().casefold()
    if browser not in {"chrome", "edge"}:
        raise ValueError(f"Unsupported browser: {browser!r}")
    label = "Google Chrome" if browser == "chrome" else "Microsoft Edge"
    if not executable.is_file():
        raise FileNotFoundError(f"{label} executable not found: {executable}")
    if is_default_browser_user_data(browser, user_data):
        raise ValueError(f"Refusing to use the system default {label} profile")
    if browser_profile_process_is_running(browser, user_data):
        raise RuntimeError(f"Close the dedicated trademark {label} profile before launching the workstation")
    user_data.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([
        str(executable),
        f"--user-data-dir={user_data}",
        f"--profile-directory={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--start-maximized",
        dashboard_url,
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return process.pid


def launch_edge(edge: Path, user_data: Path, profile: str, dashboard_url: str) -> int:
    """Backward-compatible Edge launcher."""
    return launch_browser("edge", edge, user_data, profile, dashboard_url)


def ensure_queue(run_dir: Path, platforms: list[str]) -> None:
    queue_path = run_dir / "discovery" / "manual-capture-queue.json"
    state_path = run_dir / "discovery" / "manual-capture-state.json"
    if queue_path.is_file() and state_path.is_file():
        return
    command = [
        sys.executable,
        str(Path(__file__).resolve().parent / "build-manual-capture-queue.py"),
        "--run-dir", str(run_dir),
    ]
    for platform in platforms:
        command.extend(["--platform", platform])
    completed = run_bounded(command, timeout=60)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Queue generation failed").strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the human-operated trademark capture workstation")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8794)
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--launch-browser", action="store_true")
    parser.add_argument("--launch-chrome", action="store_true")
    parser.add_argument("--launch-edge", action="store_true")
    parser.add_argument("--browser", choices=("auto", "edge", "chrome"), default="auto")
    parser.add_argument("--browser-executable")
    parser.add_argument("--browser-user-data")
    parser.add_argument("--chrome-executable")
    parser.add_argument("--chrome-user-data")
    parser.add_argument("--edge-executable")
    parser.add_argument("--edge-user-data")
    parser.add_argument("--profile-directory", default="Default")
    args = parser.parse_args()
    if args.launch_chrome and args.launch_edge:
        raise ValueError("Choose only one of --launch-chrome and --launch-edge")
    if args.bind not in {"127.0.0.1", "localhost"}:
        raise ValueError("The workstation may bind only to loopback")
    if not 1024 <= args.port <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    run_dir = Path(args.run_dir).resolve()
    ensure_queue(run_dir, args.platform)
    workstation = Workstation(run_dir)
    Handler.workstation = workstation
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    dashboard_url = f"http://127.0.0.1:{args.port}/"
    browser_request = "chrome" if args.launch_chrome else ("edge" if args.launch_edge else args.browser)
    launch_requested = args.launch_browser or args.launch_chrome or args.launch_edge
    if args.edge_executable or args.edge_user_data:
        browser_request = "edge"
    elif args.chrome_executable or args.chrome_user_data:
        browser_request = "chrome"
    automatic_selection = browser_request == "auto"
    specific_executable = args.chrome_executable if browser_request == "chrome" else args.edge_executable
    browser, executable = resolve_browser_selection(
        browser_request, args.browser_executable or specific_executable,
    )
    specific_user_data = args.chrome_user_data if browser == "chrome" else args.edge_user_data
    user_data = Path(args.browser_user_data or specific_user_data or dedicated_browser_user_data(browser)).resolve()
    browser_pid = None
    if launch_requested:
        browser_pid = launch_browser(
            browser, executable, user_data, args.profile_directory, dashboard_url,
        )
    print(json.dumps({
        "manual_capture_workstation_ready": True,
        "dashboard": dashboard_url,
        "run_dir": str(run_dir),
        "browser": browser,
        "browser_selection_policy": "edge_then_chrome",
        "browser_fallback_used": automatic_selection and browser == "chrome",
        "browser_executable": str(executable),
        "browser_user_data": str(user_data),
        "browser_launched": bool(browser_pid),
        "browser_pid": browser_pid,
        "edge_launched": bool(browser_pid) if browser == "edge" else False,
        "edge_pid": browser_pid if browser == "edge" else None,
        "instruction": f"Use the local extension in {browser.title()} to open the current task and archive the page; keep this process running.",
    }, ensure_ascii=False, indent=2), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
