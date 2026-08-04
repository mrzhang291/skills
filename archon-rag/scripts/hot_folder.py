"""Hot-folder uploader for Archon RAG.

Polls a watch directory, submits new files to the pending queue, and
automatically finalizes structured scan results when they appear.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _configure(base_dir: str, department: str, boss_password: str, dept_password: str) -> None:
    path = ROOT / "boss-upload" / "scripts"
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    import boss_upload
    boss_upload.configure(
        base_dir=base_dir,
        boss_password=boss_password or None,
        departments={department: dept_password},
    )


def _process_once(base_dir: str, department: str, boss_password: str, dept_password: str, watch_dir: str) -> dict:
    _configure(base_dir, department, boss_password, dept_password)
    sys.path.insert(0, str(ROOT / "boss-upload" / "scripts"))
    import boss_upload

    done_dir = os.path.join(watch_dir, ".done")
    os.makedirs(done_dir, exist_ok=True)
    submitted = []
    for fname in sorted(os.listdir(watch_dir)):
        if fname.startswith("."):
            continue
        src = os.path.join(watch_dir, fname)
        if not os.path.isfile(src):
            continue
        try:
            result = boss_upload.boss_upload_step1(src, password=boss_password, department=department)
            submitted.append({"filename": fname, "status": result.get("status"), "pending_id": result.get("pending_id", "")})
            dst = os.path.join(done_dir, fname)
            if not os.path.exists(dst):
                os.replace(src, dst)
        except Exception as exc:
            submitted.append({"filename": fname, "status": "error", "error": str(exc)})

    finalize = boss_upload.boss_upload_auto_finalize(boss_password)
    return {"submitted": submitted, "finalize": finalize}


def run_watch(args) -> int:
    watch_dir = Path(args.watch_dir).resolve()
    os.makedirs(watch_dir, exist_ok=True)
    if args.once:
        import json
        print(json.dumps(_process_once(args.base_dir, args.dept, args.boss_password, args.dept_password, str(watch_dir)), ensure_ascii=False, indent=2))
        return 0
    print(f"Watching {watch_dir} (Ctrl+C to stop)")
    while True:
        try:
            _process_once(args.base_dir, args.dept, args.boss_password, args.dept_password, str(watch_dir))
        except Exception as exc:
            print(f"watch error: {exc}")
        time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Archon hot-folder watcher")
    parser.add_argument("--base-dir", default=os.environ.get("ARCHON_BASE_DIR", "archon-data"))
    parser.add_argument("--dept", default=os.environ.get("ARCHON_DEPARTMENT", "general"))
    parser.add_argument("--dept-password", default=os.environ.get("ARCHON_DEPT_PASSWORD", ""))
    parser.add_argument("--boss-password", default=os.environ.get("ARCHON_BOSS_PASSWORD", ""))
    parser.add_argument("--watch-dir", default=os.environ.get("ARCHON_WATCH_DIR", "inbox"))
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    return run_watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
