"""Extract text and tables from files passed on the command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boss_upload import extract_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Test boss-upload document extraction.")
    parser.add_argument("files", nargs="+", help="Files to extract")
    parser.add_argument("--preview", type=int, default=600, help="Preview character count")
    args = parser.parse_args()

    for file_path in args.files:
        result = extract_file(file_path)
        text = result.get("text", "")
        tables = result.get("tables", [])
        print(f"=== {Path(file_path).name} ===")
        print(f"characters={len(text)} tables={len(tables)}")
        print(text[: args.preview])
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
