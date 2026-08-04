"""JSONL audit log for Archon operations."""

from __future__ import annotations

import json
import os
from datetime import datetime


def _log_path(base_dir: str) -> str:
    d = os.path.join(base_dir, ".agent_data", "audit")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "audit.log")


def log_audit(base_dir: str, actor: str, action: str, target: str = "", details: dict = None) -> str:
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "target": target,
        "details": details or {},
    }
    line = json.dumps(entry, ensure_ascii=False)
    with open(_log_path(base_dir), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line


def read_audit(base_dir: str, limit: int = 100) -> list:
    path = _log_path(base_dir)
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
    return entries[-limit:]
