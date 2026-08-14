#!/usr/bin/env python3

"""Download official-API product images and shortlist visual/text trademark matches."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from visual_match_utils import best_template_match, foreground_template, sha256


MAX_IMAGE_BYTES = 12 * 1024 * 1024
IMAGE_SUFFIXES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def compact(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def candidate_id(url: str) -> str:
    return "API-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12].upper()


def download_image(url: str, destination_base: Path, timeout: float) -> tuple[Path, dict]:
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    })
    with urlopen(request, timeout=timeout) as response:
        content_type = str(response.headers.get_content_type() or "").lower()
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
        data = response.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
    decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None:
        raise ValueError("response is not a decodable image")
    suffix = IMAGE_SUFFIXES.get(content_type, ".img")
    if suffix == ".img":
        suffix = ".png" if data.startswith(b"\x89PNG") else ".jpg"
    destination = destination_base.with_suffix(suffix)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination, {
        "url": url,
        "file": destination.name,
        "content_type": content_type,
        "size_bytes": len(data),
        "sha256": sha256(destination),
        "width": int(decoded.shape[1]),
        "height": int(decoded.shape[0]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prefilter official-API sales candidates by QCC trademark image")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--threshold", type=float, default=0.46)
    parser.add_argument("--max-candidates", type=int, default=50)
    parser.add_argument("--max-images-per-candidate", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    if not 0.0 < args.threshold <= 1.0:
        raise ValueError("--threshold must be within (0, 1]")

    run_dir = Path(args.run_dir).resolve()
    reference = read_json(run_dir / "reference" / "reference.json", {}) or {}
    visual_reference = reference.get("visual_reference") or read_json(run_dir / "reference" / "qcc-reference.json", {}) or {}
    image_rel = visual_reference.get("image_file")
    if not image_rel:
        raise ValueError("QCC visual reference is missing; run fetch-qcc-trademark-reference.py first")
    reference_image = (run_dir / str(image_rel)).resolve()
    if not reference_image.is_file() or not reference_image.is_relative_to(run_dir):
        raise ValueError("QCC visual reference image is missing or outside RUN_DIR")
    mark = str(reference.get("name") or visual_reference.get("name") or "").strip()
    template = foreground_template(reference_image)

    records = read_jsonl(run_dir / "discovery" / "official-api-results.jsonl")
    deduplicated = {}
    for record in records:
        url = str(record.get("normalized_url") or record.get("url") or "").strip()
        if url and url not in deduplicated:
            deduplicated[url] = record
    records = list(deduplicated.values())[:max(1, min(200, args.max_candidates))]
    if not records:
        raise ValueError("No official API product records are available")

    image_root = run_dir / "discovery" / "api-candidate-images"
    diagnostics_root = run_dir / "capture-diagnostics"
    diagnostics_root.mkdir(parents=True, exist_ok=True)
    diagnostic_items = []
    evaluated = []
    for record in records:
        url = str(record.get("normalized_url") or record.get("url"))
        cid = candidate_id(url)
        directory = image_root / cid
        directory.mkdir(parents=True, exist_ok=True)
        sample = "\n".join(str(record.get(key) or "") for key in ("title", "snippet", "shop_name"))
        text_match = bool(mark and compact(mark) in compact(sample))
        downloads = []
        best = {"score": 0.0, "image": None, "scale": None, "location": None, "template_size": None}
        for index, image_url in enumerate((record.get("image_urls") or [])[:max(1, min(20, args.max_images_per_candidate))], start=1):
            try:
                path, metadata = download_image(str(image_url), directory / f"image-{index:03d}", max(1.0, min(30.0, args.timeout)))
                downloads.append(metadata)
                match = best_template_match(template, path)
                if match["score"] > best["score"]:
                    best = {**match, "image": str(path.relative_to(run_dir)).replace("\\", "/")}
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                diagnostic_items.append({
                    "candidate_id": cid,
                    "platform": record.get("platform"),
                    "product_url": url,
                    "image_url": image_url,
                    "status": "image_download_failed",
                    "error": str(exc),
                })
        visual_match = best["score"] >= args.threshold
        retained = visual_match or text_match
        status = "visual_near_match" if visual_match else ("text_match_needs_visual_review" if text_match else "not_similar")
        evaluated.append({
            **record,
            "candidate_id": cid,
            "url": url,
            "status": status,
            "retained": retained,
            "visual_score": best["score"],
            "visual_threshold": args.threshold,
            "best_image": best["image"],
            "best_scale": best["scale"],
            "best_location": best["location"],
            "template_size": best["template_size"],
            "text_match": text_match,
            "downloaded_images": downloads,
        })

    shortlist = [item for item in evaluated if item["retained"]]
    output = {
        "schema_version": "1.0",
        "record_type": "official_api_visual_prefilter",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_image": str(reference_image.relative_to(run_dir)).replace("\\", "/"),
        "reference_sha256": sha256(reference_image),
        "threshold": args.threshold,
        "candidate_count": len(evaluated),
        "shortlist_count": len(shortlist),
        "visual_near_match_count": sum(item["status"] == "visual_near_match" for item in shortlist),
        "text_review_count": sum(item["status"] == "text_match_needs_visual_review" for item in shortlist),
        "items": shortlist,
        "evaluated_items": evaluated,
        "limitations": [
            "API图片筛选只决定是否访问直达商品页，不自动证明商标法律使用。",
            "联盟或授权目录未返回商品，不代表平台全站不存在该商品。",
        ],
    }
    output_path = run_dir / "discovery" / "api-visual-shortlist.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    diagnostics_path = diagnostics_root / "api-image-downloads.jsonl"
    diagnostics_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in diagnostic_items), encoding="utf-8")
    print(json.dumps({
        "prefilter_complete": True,
        "candidate_count": len(evaluated),
        "shortlist_count": len(shortlist),
        "visual_near_match_count": output["visual_near_match_count"],
        "text_review_count": output["text_review_count"],
        "output": str(output_path),
        "diagnostic_count": len(diagnostic_items),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
