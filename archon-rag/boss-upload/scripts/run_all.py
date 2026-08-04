"""Write scan results, then finalize the same pending ids."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boss_upload import boss_upload_step2, configure, write_structured_result


def _default_base_dir() -> str:
    return str(Path(os.environ.get("DOCBRAIN_BASE_DIR", "SecureFile")).resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description="Write structured scan JSON and finalize records.")
    parser.add_argument("scan_json", help="JSON file containing {pending_id: structured_result}")
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
    with open(args.scan_json, "r", encoding="utf-8") as handle:
        scan_results = json.load(handle)

    final_results = []
    for pending_id, structured in scan_results.items():
        structured.setdefault("department", args.dept)
        write_structured_result(pending_id, structured)
        result = boss_upload_step2(pending_id)
        final_results.append(result)
        print(f"{result.get('status', 'unknown').upper()}: {result.get('filename', pending_id)}")

    print(json.dumps(final_results, ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") == "success" for item in final_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
