#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
import re

import fitz

from runtime_policy import (
    PDF_TAIL_ANALYSIS_SCALE,
    PDF_TAIL_MAX_LARGEST_RASTER_AREA_RATIO,
    PDF_TAIL_MAX_NONWHITE_RATIO,
    PDF_TAIL_MAX_TEXT_CHARS,
    PDF_TAIL_NONWHITE_GRAY_THRESHOLD,
)


def page_metrics(page, scale=PDF_TAIL_ANALYSIS_SCALE):
    text_chars = len(re.sub(r"\s+", "", page.get_text("text") or ""))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, alpha=False)
    samples = pix.samples
    nonwhite = (
        sum(value < PDF_TAIL_NONWHITE_GRAY_THRESHOLD for value in samples) / len(samples)
        if samples else 0.0
    )
    page_area = max(1.0, page.rect.width * page.rect.height)
    largest_raster_area_ratio = 0.0
    for image in page.get_image_info(xrefs=True):
        bbox = fitz.Rect(image.get("bbox") or fitz.Rect()) & page.rect
        if bbox.is_empty or bbox.is_infinite:
            continue
        largest_raster_area_ratio = max(
            largest_raster_area_ratio,
            bbox.width * bbox.height / page_area,
        )
    return {
        "page_number": page.number + 1,
        "text_chars": text_chars,
        "nonwhite_ratio": round(nonwhite, 6),
        "largest_raster_area_ratio": round(largest_raster_area_ratio, 6),
    }


def is_low_information(
    item, max_text_chars, max_nonwhite_ratio, max_largest_raster_area_ratio,
):
    """Use a deliberately strict AND test so sparse image evidence is retained."""
    return (
        item["text_chars"] <= max_text_chars
        and item["nonwhite_ratio"] <= max_nonwhite_ratio
        and item["largest_raster_area_ratio"] <= max_largest_raster_area_ratio
    )


def main():
    parser = argparse.ArgumentParser(description="Remove only low-information trailing pages from a browser-printed PDF")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--max-text-chars", type=int, default=PDF_TAIL_MAX_TEXT_CHARS)
    parser.add_argument("--max-nonwhite-ratio", type=float, default=PDF_TAIL_MAX_NONWHITE_RATIO)
    parser.add_argument(
        "--max-largest-raster-area-ratio",
        type=float,
        default=PDF_TAIL_MAX_LARGEST_RASTER_AREA_RATIO,
    )
    parser.add_argument("--min-pages", type=int, default=1)
    args = parser.parse_args()

    if args.max_text_chars < 0:
        parser.error("--max-text-chars must be non-negative")
    if not 0 <= args.max_nonwhite_ratio <= 1:
        parser.error("--max-nonwhite-ratio must be between 0 and 1")
    if not 0 <= args.max_largest_raster_area_ratio <= 1:
        parser.error("--max-largest-raster-area-ratio must be between 0 and 1")
    if args.min_pages < 1:
        parser.error("--min-pages must be at least 1")

    pdf_path = Path(args.pdf).resolve()
    doc = fitz.open(pdf_path)
    if not doc.is_pdf or doc.needs_pass or doc.page_count < 1:
        doc.close()
        raise ValueError(f"Invalid or protected source PDF: {pdf_path}")
    original_pages = doc.page_count
    metrics = [page_metrics(page) for page in doc]
    keep_pages = original_pages
    minimum = min(original_pages, args.min_pages)
    while keep_pages > minimum:
        item = metrics[keep_pages - 1]
        if not is_low_information(
            item,
            args.max_text_chars,
            args.max_nonwhite_ratio,
            args.max_largest_raster_area_ratio,
        ):
            break
        keep_pages -= 1

    trimmed = original_pages - keep_pages
    trimmed_page_numbers = list(range(keep_pages + 1, original_pages + 1))
    if trimmed:
        doc.delete_pages(keep_pages, original_pages - 1)
        temp_path = pdf_path.with_suffix(pdf_path.suffix + ".trimmed.tmp")
        if temp_path.exists():
            temp_path.unlink()
        doc.save(temp_path, garbage=4, deflate=True)
        doc.close()
        check = fitz.open(temp_path)
        try:
            if not check.is_pdf or check.needs_pass or check.page_count != keep_pages:
                raise RuntimeError("Trimmed PDF failed physical validation")
        finally:
            check.close()
        temp_path.replace(pdf_path)
    else:
        doc.close()

    report = {
        "ok": True,
        "original_pages": original_pages,
        "kept_pages": keep_pages,
        "trimmed_pages": trimmed,
        "kept_page_numbers": list(range(1, keep_pages + 1)),
        "trimmed_page_numbers": trimmed_page_numbers,
        "trim_reason": "consecutive_low_information_tail" if trimmed else None,
        "thresholds": {
            "max_text_chars": args.max_text_chars,
            "max_nonwhite_ratio": args.max_nonwhite_ratio,
            "max_largest_raster_area_ratio": args.max_largest_raster_area_ratio,
            "nonwhite_gray_threshold": PDF_TAIL_NONWHITE_GRAY_THRESHOLD,
            "analysis_scale": PDF_TAIL_ANALYSIS_SCALE,
        },
        "page_metrics": metrics,
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
