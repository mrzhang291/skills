#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageStat


def is_blank_region(image: Image.Image) -> bool:
    """Return true only for an almost-flat, almost-white painted image region."""
    sample = image.convert("RGB")
    if sample.width > 240 or sample.height > 240:
        sample.thumbnail((240, 240), Image.Resampling.LANCZOS)
    stat = ImageStat.Stat(sample)
    mean = sum(stat.mean) / 3
    deviation = sum(stat.stddev) / 3
    pixels = list(sample.getdata())
    near_white = sum(1 for red, green, blue in pixels if red >= 246 and green >= 246 and blue >= 246)
    white_ratio = near_white / max(1, len(pixels))
    return mean >= 247 and deviation <= 3.2 and white_ratio >= 0.975


def stitch(manifest_path: Path, output_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tiles = manifest.get("tiles") or []
    if not tiles:
        raise ValueError("tile manifest is empty")

    first = Image.open(tiles[0]["path"]).convert("RGB")
    viewport_width = max(1.0, float(tiles[0].get("viewport_width_css") or first.width))
    viewport_height = max(1.0, float(tiles[0].get("viewport_height_css") or first.height))
    scale_x = first.width / viewport_width
    scale_y = first.height / viewport_height
    document_height = max(
        float(manifest.get("document_height_css") or 0),
        max(float(tile.get("scroll_y_css") or 0) + float(tile.get("viewport_height_css") or viewport_height) for tile in tiles),
    )
    canvas_width = first.width
    canvas_height = max(1, math.ceil(document_height * scale_y))
    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")

    checked_regions = 0
    blank_regions = 0
    per_tile = []
    for tile in sorted(tiles, key=lambda value: float(value.get("scroll_y_css") or 0)):
        tile_image = Image.open(tile["path"]).convert("RGB")
        tile_scale_x = tile_image.width / max(1.0, float(tile.get("viewport_width_css") or viewport_width))
        tile_scale_y = tile_image.height / max(1.0, float(tile.get("viewport_height_css") or viewport_height))
        destination_y = max(0, round(float(tile.get("scroll_y_css") or 0) * scale_y))
        remaining = canvas_height - destination_y
        if remaining <= 0:
            continue
        painted = tile_image if tile_image.height <= remaining else tile_image.crop((0, 0, tile_image.width, remaining))
        canvas.paste(painted, (0, destination_y))

        tile_checked = 0
        tile_blank = 0
        for box in tile.get("paint_boxes") or []:
            left = max(0, min(tile_image.width, math.floor(float(box.get("x") or 0) * tile_scale_x)))
            top = max(0, min(tile_image.height, math.floor(float(box.get("y") or 0) * tile_scale_y)))
            right = max(left, min(tile_image.width, math.ceil((float(box.get("x") or 0) + float(box.get("width") or 0)) * tile_scale_x)))
            bottom = max(top, min(tile_image.height, math.ceil((float(box.get("y") or 0) + float(box.get("height") or 0)) * tile_scale_y)))
            if right - left < 24 or bottom - top < 24:
                continue
            tile_checked += 1
            checked_regions += 1
            if is_blank_region(tile_image.crop((left, top, right, bottom))):
                tile_blank += 1
                blank_regions += 1
        per_tile.append({
            "index": tile.get("index"),
            "checked_paint_regions": tile_checked,
            "blank_paint_regions": tile_blank,
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    blank_ratio = blank_regions / max(1, checked_regions)
    expected_items = max(0, int(manifest.get("expected_item_count") or 0))
    unique_sources = len(set(manifest.get("paint_source_keys") or []))
    required_candidates = min(3, expected_items) if expected_items else 0
    enough_candidates = unique_sources >= required_candidates
    no_unresolved_images = int(manifest.get("visible_unresolved_images") or 0) <= max(
        2, math.ceil(int(manifest.get("visible_substantive_images") or 0) * 0.08)
    )
    acceptable = (
        blank_regions <= max(1, math.floor(checked_regions * 0.08))
        and enough_candidates
        and no_unresolved_images
    )
    return {
        "strategy": "viewport_tile_stitch_v1",
        "tile_count": len(tiles),
        "document_height_css": document_height,
        "output_width_px": canvas_width,
        "output_height_px": canvas_height,
        "checked_paint_regions": checked_regions,
        "blank_paint_regions": blank_regions,
        "blank_paint_ratio": round(blank_ratio, 4),
        "unique_paint_sources": unique_sources,
        "required_visual_candidates": required_candidates,
        "visible_substantive_images": int(manifest.get("visible_substantive_images") or 0),
        "visible_unresolved_images": int(manifest.get("visible_unresolved_images") or 0),
        "acceptable": acceptable,
        "per_tile": per_tile,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Stitch viewport screenshots and validate painted image regions")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = stitch(Path(args.manifest).resolve(), Path(args.output).resolve())
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["acceptable"] else 3)


if __name__ == "__main__":
    main()
