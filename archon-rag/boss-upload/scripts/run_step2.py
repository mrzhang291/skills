"""Run boss-upload step 2 for pending ids passed on the command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boss_upload import boss_upload_step2, configure


def _default_base_dir() -> str:
    return str(Path(os.environ.get("DOCBRAIN_BASE_DIR", "SecureFile")).resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description="Finalize structured pending records by id.")
    parser.add_argument("pending_ids", nargs="+", help="Pending ids to finalize")
    parser.add_argument("--base-dir", default=_default_base_dir())
    parser.add_argument("--dept", default=os.environ.get("DOCBRAIN_DEPARTMENT", "general"))
    parser.add_argument("--boss-password", default=os.environ.get("BOSS_UPLOAD_PASSWORD", ""))
    parser.add_argument("--dept-password", default=os.environ.get("DOCBRAIN_DEPT_PASSWORD", ""))
    args = parser.parse_args()

    configure(
        base_dir=args.base_dir,
        boss_password=args.boss_password or None,
        departments={args.dept: args.dept_password},
    )

    results = []
    for pending_id in args.pending_ids:
        try:
            result = boss_upload_step2(pending_id)
            results.append(result)
            status = "OK" if result.get("status") == "success" else "FAIL"
            print(f"{status}: {result.get('filename', pending_id)}")
        except Exception as exc:
            result = {"pending_id": pending_id, "error": str(exc)}
            results.append(result)
            print(f"ERR: {pending_id} -> {exc}")

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") == "success" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
