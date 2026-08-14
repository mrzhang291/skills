#!/usr/bin/env python3

import argparse
import io
from pathlib import Path

import fitz
from PIL import Image


def main():
    parser = argparse.ArgumentParser(description="Convert a long screenshot into one or more A4 PDF pages")
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--title", default="Raster fallback")
    parser.add_argument("--url", default="")
    args = parser.parse_args()

    image_path = Path(args.image).resolve()
    output_path = Path(args.output).resolve()
    source = Image.open(image_path).convert("RGB")
    a4 = fitz.paper_rect("a4")
    margin = 28
    content_w = a4.width - margin * 2
    content_h = a4.height - 54
    slice_h = max(1, int(source.width * content_h / content_w))
    doc = fitz.open()
    for top in range(0, source.height, slice_h):
        crop = source.crop((0, top, source.width, min(source.height, top + slice_h)))
        buffer = io.BytesIO()
        crop.save(buffer, format="JPEG", quality=90, optimize=True)
        page = doc.new_page(width=a4.width, height=a4.height)
        page.insert_image(fitz.Rect(margin, 24, a4.width - margin, 24 + crop.height * content_w / crop.width), stream=buffer.getvalue())
        page.insert_textbox(fitz.Rect(margin, a4.height - 24, a4.width - margin, a4.height - 10), args.url, fontsize=6, color=(0.35, 0.35, 0.35))
    doc.set_metadata({"title": args.title, "subject": "Screenshot raster fallback"})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path, deflate=True)
    doc.close()
    print(output_path)


if __name__ == "__main__":
    main()
