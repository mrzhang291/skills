"""Finalize all available boss-upload structured records."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boss_upload import boss_upload_auto_finalize, configure


def _default_base_dir() -> str:
    return str(Path(os.environ.get("DOCBRAIN_BASE_DIR", "SecureFile")).resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run boss_upload_auto_finalize.")
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
    result = boss_upload_auto_finalize(args.boss_password)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"success", "ok"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
