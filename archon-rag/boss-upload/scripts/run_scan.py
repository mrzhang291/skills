"""Write AI scan results from a JSON file into boss-upload structured output."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boss_upload import configure, write_structured_result


def _default_base_dir() -> str:
    return str(Path(os.environ.get("DOCBRAIN_BASE_DIR", "SecureFile")).resolve())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load {pending_id: structured_result} JSON and write structured files."
    )
    parser.add_argument("scan_json", help="JSON file containing pending id keys")
    parser.add_argument("--base-dir", default=_default_base_dir())
    parser.add_argument("--dept", default=os.environ.get("DOCBRAIN_DEPARTMENT", "general"))
    parser.add_argument("--dept-password", default=os.environ.get("DOCBRAIN_DEPT_PASSWORD", ""))
    args = parser.parse_args()

    configure(base_dir=args.base_dir, departments={args.dept: args.dept_password})
    with open(args.scan_json, "r", encoding="utf-8") as handle:
        scan_results = json.load(handle)

    for pending_id, structured in scan_results.items():
        structured.setdefault("department", args.dept)
        path = write_structured_result(pending_id, structured)
        print(f"OK: {pending_id} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
