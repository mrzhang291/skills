#!/usr/bin/env python3
r"""Unicode-safe CherryStudio adapter for init-run.py.

CherryStudio agents can write the intake JSON with ASCII ``\uXXXX`` escapes on
Windows, avoiding PowerShell/GBK corruption of Chinese trademark fields.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from process_utils import run_bounded


def clean(value) -> str:
    return str(value or "").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize a trademark run from a UTF-8/ASCII-escaped JSON intake")
    parser.add_argument("--intake-json", required=True)
    args = parser.parse_args()

    intake_path = Path(args.intake_json).expanduser().resolve()
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    required = ("workspace", "run_id", "trademark_name")
    missing = [key for key in required if not clean(intake.get(key))]
    if not clean(intake.get("reference_file")) and intake.get("allow_missing_reference") is not True:
        missing.append("reference_file")
    if missing:
        raise ValueError(f"Missing required intake fields: {', '.join(missing)}")

    goods = intake.get("goods") or intake.get("goods_services") or []
    if isinstance(goods, list):
        goods = ";".join(clean(item) for item in goods if clean(item))
    command = [
        sys.executable,
        str(Path(__file__).with_name("init-run.py")),
        "--profile", clean(intake.get("profile") or "quick"),
        "--workspace", clean(intake["workspace"]),
        "--run-id", clean(intake["run_id"]),
        "--trademark-name", clean(intake["trademark_name"]),
        "--goods", clean(goods),
    ]
    if clean(intake.get("reference_file")):
        command.extend(["--reference-file", clean(intake["reference_file"])])
    else:
        command.append("--allow-missing-reference")
    optional = {
        "registration_number": "--registration-number",
        "owner": "--owner",
        "period": "--period",
    }
    for key, flag in optional.items():
        if clean(intake.get(key)):
            command.extend([flag, clean(intake[key])])

    completed = run_bounded(command, timeout=120)
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.returncode:
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        raise SystemExit(completed.returncode)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
