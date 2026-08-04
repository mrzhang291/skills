"""Task status and heartbeat helpers for the Archon pipeline."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime


def _status_dir(base_dir: str) -> str:
    path = os.path.join(base_dir, ".agent_data", "status")
    os.makedirs(path, exist_ok=True)
    return path


def _path(base_dir: str, pending_id: str) -> str:
    return os.path.join(_status_dir(base_dir), f"task_{pending_id}.json")


def update_task_status(base_dir: str, pending_id: str, status: str, **extra) -> dict:
    """Write a task status heartbeat."""
    data = {
        "pending_id": pending_id,
        "status": status,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "attempts": int(extra.pop("attempts", 0)),
    }
    data.update(extra)
    with open(_path(base_dir, pending_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def record_attempt(base_dir: str, pending_id: str, status: str = "claimed", **extra) -> dict:
    """Increment the attempt counter for a task."""
    existing = get_task_status(base_dir, pending_id)
    attempts = int(existing.get("attempts", 0)) + 1
    return update_task_status(base_dir, pending_id, status, attempts=attempts, **extra)


def get_task_status(base_dir: str, pending_id: str) -> dict:
    path = _path(base_dir, pending_id)
    if not os.path.exists(path):
        return {"pending_id": pending_id, "status": "unknown", "attempts": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"pending_id": pending_id, "status": "unknown", "attempts": 0}


def list_task_status(base_dir: str) -> list:
    d = _status_dir(base_dir)
    out = []
    for fname in sorted(os.listdir(d)):
        if not fname.startswith("task_") or not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            pass
    return out


def cleanup_stale_status(base_dir: str, max_age_hours: int = 24) -> int:
    """Remove task status files that have not been updated recently."""
    d = _status_dir(base_dir)
    now = time.time()
    cleaned = 0
    for fname in os.listdir(d):
        if not fname.startswith("task_") or not fname.endswith(".json"):
            continue
        path = os.path.join(d, fname)
        try:
            if now - os.path.getmtime(path) > max_age_hours * 3600:
                os.remove(path)
                cleaned += 1
        except Exception:
            pass
    return cleaned
