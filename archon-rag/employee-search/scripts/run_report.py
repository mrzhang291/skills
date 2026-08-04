"""Generate a report through the local employee_search.py CLI."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
EMPLOYEE_SEARCH = SCRIPT_DIR / "employee_search.py"


def run_report(doc_id: str, answer_file: Path, title: str, fmt: str = "pdf") -> int:
    answer = answer_file.read_text(encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(EMPLOYEE_SEARCH),
            "report",
            doc_id,
            "--format",
            fmt,
            "--title",
            title,
            "--answer",
            answer,
        ],
        capture_output=True,
    )
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an employee-search report.")
    parser.add_argument("--doc-id", default="af0e2afc")
    parser.add_argument("--answer-file", default=str(SCRIPT_DIR / "answer.txt"))
    parser.add_argument("--title", default="Document Report")
    parser.add_argument("--format", default="pdf", choices=["pdf", "docx", "txt"])
    args = parser.parse_args()
    return run_report(args.doc_id, Path(args.answer_file), args.title, args.format)


if __name__ == "__main__":
    raise SystemExit(main())
