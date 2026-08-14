#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare online and offline full-page screenshots")
    parser.add_argument("--online", required=True)
    parser.add_argument("--offline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-similarity", type=float, default=0.90)
    args = parser.parse_args()

    online_path = Path(args.online).resolve()
    offline_path = Path(args.offline).resolve()
    output = Path(args.output).resolve()
    report = {
        "schema_version": "2.0", "record_type": "screenshot_comparison",
        "online": str(online_path), "offline": str(offline_path),
        "min_similarity": args.min_similarity, "ok": False, "similarity": 0.0, "errors": [],
    }
    try:
        with Image.open(online_path) as left_source, Image.open(offline_path) as right_source:
            left = left_source.convert("RGB")
            right = right_source.convert("RGB")
            report["online_size"] = list(left.size)
            report["offline_size"] = list(right.size)
            size = (384, 384)
            left = left.resize(size, Image.Resampling.LANCZOS)
            right = right.resize(size, Image.Resampling.LANCZOS)
            difference = ImageChops.difference(left, right)
            mean = sum(ImageStat.Stat(difference).mean) / 3
            report["mean_absolute_error"] = round(mean, 6)
            report["similarity"] = round(max(0.0, 1.0 - mean / 255.0), 6)
            height_ratio = min(report["online_size"][1], report["offline_size"][1]) / max(report["online_size"][1], report["offline_size"][1])
            width_ratio = min(report["online_size"][0], report["offline_size"][0]) / max(report["online_size"][0], report["offline_size"][0])
            report["dimension_ratio"] = round(height_ratio * width_ratio, 6)
            report["ok"] = report["similarity"] >= args.min_similarity and report["dimension_ratio"] >= 0.8
    except Exception as exc:
        report["errors"].append(str(exc))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 3)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()

