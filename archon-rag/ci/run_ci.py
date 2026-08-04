"""Local CI gate: compile, tests, and optional retrieval eval thresholds."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Archon CI gate")
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--min-recall", type=float, default=0.8)
    parser.add_argument("--min-mrr", type=float, default=0.7)
    parser.add_argument("--base-dir", default=os.environ.get("ARCHON_BASE_DIR", ""))
    parser.add_argument("--dept", default=os.environ.get("ARCHON_DEPARTMENT", "general"))
    parser.add_argument("--dept-password", default=os.environ.get("ARCHON_DEPT_PASSWORD", ""))
    parser.add_argument("--eval-json", default=str(ROOT / "eval" / "eval_queries.json"))
    args = parser.parse_args()

    print("== compile ==")
    compiled = _run([sys.executable, "-m", "compileall", "-q", str(ROOT)], ROOT)
    print(compiled.stdout.strip())
    if compiled.returncode != 0:
        print(compiled.stderr)
        return 1

    print("== pytest ==")
    tests = _run([sys.executable, "-m", "pytest", "-q", str(ROOT / "tests")], ROOT)
    print(tests.stdout.strip())
    if tests.returncode != 0:
        print(tests.stderr)
        return 1

    if args.skip_eval or not args.base_dir or not os.path.exists(args.eval_json):
        print("== eval skipped ==")
        return 0

    print("== eval ==")
    eval_cmd = [
        sys.executable,
        str(ROOT / "eval" / "run_eval.py"),
        "--base-dir", args.base_dir,
        "--dept", args.dept,
        "--dept-password", args.dept_password,
        "--eval-json", args.eval_json,
        "--k", "5",
    ]
    eval_result = _run(eval_cmd, ROOT)
    if eval_result.returncode != 0:
        print(eval_result.stderr)
        return 1
    try:
        data = json.loads(eval_result.stdout)
    except Exception:
        print(eval_result.stdout)
        return 1
    recall = data.get("mean_recall_at_k", 0.0)
    mrr = data.get("mean_mrr", 0.0)
    print(f"recall@5={recall} mrr={mrr}")
    if recall < args.min_recall or mrr < args.min_mrr:
        print(f"eval gate failed: recall@5 {recall} < {args.min_recall} or mrr {mrr} < {args.min_mrr}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
