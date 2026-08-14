#!/usr/bin/env python3

"""Shared local image matching helpers for trademark candidate screening."""

from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def foreground_template(path: Path) -> dict[str, np.ndarray]:
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"Cannot decode reference image: {path}")
    _, mask = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    points = cv2.findNonZero(mask)
    if points is None:
        raise ValueError("Reference trademark image has no visible foreground")
    x, y, width, height = cv2.boundingRect(points)
    pad = max(2, int(max(width, height) * 0.04))
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(image.shape[1], x + width + pad), min(image.shape[0], y + height + pad)
    crop = image[y0:y1, x0:x1]
    _, crop_mask = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return {"mask": crop_mask, "edges": cv2.Canny(crop, 40, 140)}


def load_gray(path: Path) -> np.ndarray | None:
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    limit = 2200
    height, width = image.shape[:2]
    if max(height, width) > limit:
        ratio = limit / max(height, width)
        image = cv2.resize(image, (max(1, int(width * ratio)), max(1, int(height * ratio))))
    return image


def best_template_match(template: dict[str, np.ndarray], candidate_path: Path) -> dict:
    gray = load_gray(candidate_path)
    if gray is None:
        return {"score": 0.0, "scale": None, "location": None, "template_size": None}
    edges = cv2.Canny(gray, 40, 140)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    best = {"score": 0.0, "scale": None, "location": None, "template_size": None}
    for scale in np.geomspace(0.18, 2.6, 24):
        width = max(6, int(template["mask"].shape[1] * scale))
        height = max(6, int(template["mask"].shape[0] * scale))
        if width >= edges.shape[1] or height >= edges.shape[0]:
            continue
        interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        resized_edges = cv2.resize(template["edges"], (width, height), interpolation=interpolation)
        resized_mask = cv2.resize(template["mask"], (width, height), interpolation=cv2.INTER_NEAREST)
        if np.count_nonzero(resized_mask) < 8:
            continue
        edge_score, edge_location = cv2.minMaxLoc(
            cv2.matchTemplate(edges, resized_edges, cv2.TM_CCOEFF_NORMED)
        )[1::2]
        mask_score, mask_location = cv2.minMaxLoc(
            cv2.matchTemplate(mask, resized_mask, cv2.TM_CCOEFF_NORMED)
        )[1::2]
        score, location = (
            (mask_score, mask_location) if mask_score >= edge_score else (edge_score, edge_location)
        )
        if score > best["score"]:
            best = {
                "score": round(float(score), 6),
                "scale": round(float(scale), 5),
                "location": [int(location[0]), int(location[1])],
                "template_size": [width, height],
            }
    return best
