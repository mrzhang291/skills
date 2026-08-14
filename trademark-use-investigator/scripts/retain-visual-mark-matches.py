#!/usr/bin/env python3
"""Retain candidate HTML archives whose images resemble the QCC trademark image."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qcc_reference_guard import validate_qcc_reference
from visual_match_utils import best_template_match, foreground_template, sha256


FORBIDDEN_STATES = {
    "captcha", "login_required", "access_denied", "empty_shell", "empty_results",
    "error_page", "search_result_page", "unexpected_content", "subprocess_timeout", "error",
    "subprocess_error", "visual_capture_incomplete", "probe_artifact_failed",
}
HTML_NAMES = ("page.singlefile.html", "rendered-dom.html", "page.html", "response.html")
ARCHIVE_NAMES = (
    *HTML_NAMES, "page.singlefile.raw.html", "page.mhtml", "fullpage.png", "page.pdf",
    "body-text.txt", "metadata.json", "singlefile-validation.json", "singlefile-offline.png",
)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def candidate_dirs(run_dir: Path) -> list[Path]:
    output = []
    for root in (
        run_dir / "candidate-pages",
        run_dir / "capture" / "sales-after-login",
        run_dir / "capture" / "visual-first",
    ):
        if root.is_dir():
            output.extend(path for path in root.iterdir() if path.is_dir())
    unique = {path.resolve(): path for path in output}
    return [unique[key] for key in sorted(unique, key=str)]


def candidate_images(directory: Path) -> list[Path]:
    """Return extracted image elements only; never template-match an entire page screenshot."""
    images = []
    image_dir = directory / "images"
    if image_dir.is_dir():
        images.extend(sorted(path for path in image_dir.iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}))
    return images


def error_page_reason(metadata: dict, title: str, body: str) -> str | None:
    """Detect hard/soft HTTP error pages before any visual comparison."""
    try:
        status = int(metadata.get("http_status")) if metadata.get("http_status") is not None else None
    except (TypeError, ValueError):
        status = None
    if status in {401, 403, 405, 407, 429, 451}:
        return "access_denied"
    if status is not None and status >= 400:
        return "error_page"

    url = str(metadata.get("final_url") or metadata.get("requested_url") or "")
    try:
        parsed = urlsplit(url)
        pathname = parsed.path
        error_hints = dict(
            pair.split("=", 1) if "=" in pair else (pair, "")
            for pair in parsed.query.split("&") if pair
        )
        error_hint = " ".join(
            error_hints.get(key, "") for key in ("status", "error", "code", "httpStatus")
        ).strip()
    except ValueError:
        pathname = url
        error_hint = ""
    if error_hint == "405" or re.search(
        r"(?:^|[/_.-])405(?:[/_.-]|$)|/(?:forbidden|access[-_]?denied|permission[-_]?denied|blocked)(?:[/.]|$)",
        pathname, re.I,
    ):
        return "access_denied"
    if error_hint == "404" or re.search(
        r"(?:^|[/_.-])404(?:[/_.-]|$)|/(?:errors?|not[-_]?found|page[-_]?not[-_]?found)(?:[/.]|$)",
        pathname, re.I,
    ):
        return "error_page"

    clean_title = re.sub(r"\s+", " ", title).strip()
    if re.search(
        r"(?:^|[\s([（])405(?:[\s\])）:：-]+(?:method not allowed|错误|请求不允许|不允许)|\s*$)|method not allowed|access denied|forbidden|拒绝访问|无权访问|访问受限|请求被拒绝",
        clean_title,
        re.I,
    ):
        return "access_denied"
    if re.search(
        r"(?:^|[\s([（])404(?:[\s\])）:：-]+(?:not found|page not found|错误|页面不存在|页面未找到)|\s*$)|page not found|not found|页面不存在|页面未找到|找不到页面|错误页|系统错误|服务器错误|bad gateway|service unavailable",
        clean_title,
        re.I,
    ):
        return "error_page"

    clean_body = re.sub(r"\s+", " ", body).strip()[:12_000]
    if re.search(
        r"\b405\s+method not allowed\b|the requested method is not allowed|请求方法不允许|您没有权限访问|无权访问此页面|拒绝访问此页面",
        clean_body,
        re.I,
    ):
        return "access_denied"
    if re.search(
        r"\b404\s+(?:not found|page not found)\b|the requested (?:url|page) was not found|您访问的页面不存在|页面不存在或已删除|抱歉[，, ]*(?:您访问的)?页面(?:不存在|找不到)|系统(?:发生)?错误|服务器(?:内部)?错误",
        clean_body,
        re.I,
    ):
        return "error_page"
    return None


def looks_like_company_information(metadata: dict, title: str, body: str) -> bool:
    if str(metadata.get("page_type") or "").lower() in {"company", "registry"}:
        return True
    url = str(metadata.get("final_url") or metadata.get("requested_url") or "")
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    if any(domain in host for domain in ("qcc.com", "tianyancha.com", "aiqicha.baidu.com", "shuidi.cn")):
        return True
    sample = f"{title}\n{body[:20_000]}"
    return bool(re.search(
        r"企业信息|工商信息|公司信息|企业信用|统一社会信用代码|法定代表人|企查查|天眼查|爱企查|水滴信用",
        sample,
        re.I,
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description="Retain visually similar trademark candidate HTML pages")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--threshold", type=float, default=0.46)
    parser.add_argument("--text-candidate", action="store_true", default=True)
    args = parser.parse_args()
    if not 0.0 < args.threshold <= 1.0:
        raise ValueError("--threshold must be within (0, 1]")

    run_dir = Path(args.run_dir).resolve()
    qcc_validation = validate_qcc_reference(run_dir)
    if not qcc_validation.get("ok"):
        raise ValueError(
            "QCC visual reference failed strict provenance validation: "
            + ", ".join(qcc_validation.get("errors") or ["unknown_error"])
        )
    reference = read_json(run_dir / "reference" / "reference.json", {}) or {}
    visual_reference = reference.get("visual_reference") or read_json(run_dir / "reference" / "qcc-reference.json", {}) or {}
    image_rel = visual_reference.get("image_file")
    if not image_rel:
        raise ValueError("QCC visual reference is missing; run fetch-qcc-trademark-reference.py first")
    reference_image = (run_dir / image_rel).resolve()
    if not reference_image.is_file() or not reference_image.is_relative_to(run_dir):
        raise ValueError("QCC visual reference image is missing or outside RUN_DIR")
    template = foreground_template(reference_image)
    mark = str(reference.get("name") or visual_reference.get("name") or "").strip()

    retained_root = run_dir / "visual-match-pages"
    retained_root.mkdir(parents=True, exist_ok=True)
    items = []
    for directory in candidate_dirs(run_dir):
        metadata = read_json(directory / "metadata.json", {}) or {}
        state = str(metadata.get("page_state") or "unknown")
        body_path = directory / "body-text.txt"
        body = body_path.read_text(encoding="utf-8", errors="replace") if body_path.is_file() else ""
        title = str(metadata.get("title") or "")
        hard_error = error_page_reason(metadata, title, body)
        if hard_error:
            items.append({"candidate_id": directory.name, "status": "excluded", "reason": hard_error})
            continue
        if state in FORBIDDEN_STATES or metadata.get("content_valid") is False:
            items.append({"candidate_id": directory.name, "status": "excluded", "reason": state})
            continue
        text_match = bool(mark and mark.replace(" ", "") in f"{title}\n{body}".replace(" ", ""))
        if state == "manual_visual_review" and not text_match and looks_like_company_information(metadata, title, body):
            items.append({
                "candidate_id": directory.name,
                "status": "excluded",
                "reason": "manual_visual_review_unanchored_company_information",
            })
            continue
        best = {"score": 0.0, "image": None, "scale": None, "location": None, "template_size": None}
        extracted_images = candidate_images(directory)
        for image_path in extracted_images:
            result = best_template_match(template, image_path)
            if result["score"] > best["score"]:
                best = {**result, "image": str(image_path.relative_to(directory)).replace("\\", "/")}
        visual_match = best["score"] >= args.threshold
        retain = visual_match or (args.text_candidate and text_match)
        status = "visual_near_match" if visual_match else ("text_match_needs_visual_review" if text_match else "not_similar")
        item = {
            "candidate_id": directory.name,
            "source_dir": str(directory.relative_to(run_dir)).replace("\\", "/"),
            "status": status,
            "retained": retain,
            "visual_score": best["score"],
            "threshold": args.threshold,
            "best_image": best["image"],
            "best_scale": best["scale"],
            "best_location": best["location"],
            "template_size": best["template_size"],
            "extracted_candidate_image_count": len(extracted_images),
            "text_match": text_match,
            "url": metadata.get("final_url") or metadata.get("requested_url"),
            "title": title,
            "html_available": any((directory / name).is_file() for name in HTML_NAMES),
        }
        if retain:
            target = retained_root / directory.name
            target.mkdir(parents=True, exist_ok=True)
            copied = []
            for name in ARCHIVE_NAMES:
                source = directory / name
                if source.is_file():
                    destination = target / name
                    shutil.copy2(source, destination)
                    copied.append({"file": name, "sha256": sha256(destination)})
            if best["image"]:
                source = directory / best["image"]
                destination = target / f"best-match{source.suffix.lower()}"
                shutil.copy2(source, destination)
                copied.append({"file": destination.name, "sha256": sha256(destination)})
            item["retained_dir"] = str(target.relative_to(run_dir)).replace("\\", "/")
            item["copied_artifacts"] = copied
            (target / "visual-match.json").write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        items.append(item)

    result = {
        "schema_version": "1.0",
        "record_type": "qcc_reference_visual_match_results",
        "reference_image": str(reference_image.relative_to(run_dir)).replace("\\", "/"),
        "reference_sha256": sha256(reference_image),
        "threshold": args.threshold,
        "candidate_count": len(items),
        "retained_count": sum(bool(item.get("retained")) for item in items),
        "visual_near_match_count": sum(item.get("status") == "visual_near_match" for item in items),
        "text_review_count": sum(item.get("status") == "text_match_needs_visual_review" for item in items),
        "items": items,
    }
    output_path = run_dir / "visual-match-results.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 商标图样近似页面", "",
        f"企查查视觉基准：`{result['reference_image']}`", "",
        f"候选页面：{result['candidate_count']}；保留：{result['retained_count']}；视觉近似：{result['visual_near_match_count']}；文字命中待复核：{result['text_review_count']}。", "",
        "| 候选 | 状态 | 视觉分数 | HTML | 页面 |", "|---|---|---:|---:|---|",
    ]
    for item in items:
        if not item.get("retained"):
            continue
        title = str(item.get("title") or item.get("candidate_id")).replace("|", "\\|")
        url = item.get("url") or ""
        lines.append(f"| {item['candidate_id']} | {item['status']} | {item['visual_score']:.3f} | {'是' if item['html_available'] else '否'} | [{title}]({url}) |")
    (run_dir / "visual-match-results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    index_rows = []
    for item in items:
        if not item.get("retained"):
            continue
        retained = retained_root / str(item["candidate_id"])
        html_name = next((name for name in HTML_NAMES if (retained / name).is_file()), None)
        screenshot = "fullpage.png" if (retained / "fullpage.png").is_file() else None
        record = "visual-match.json" if (retained / "visual-match.json").is_file() else None
        links = []
        if html_name:
            links.append(f'<a href="{html.escape(item["candidate_id"] + "/" + html_name)}">打开离线 HTML</a>')
        if screenshot:
            links.append(f'<a href="{html.escape(item["candidate_id"] + "/" + screenshot)}">整页截图</a>')
        if record:
            links.append(f'<a href="{html.escape(item["candidate_id"] + "/" + record)}">比对记录</a>')
        index_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item['candidate_id']))}</td>"
            f"<td>{html.escape(str(item['status']))}</td>"
            f"<td>{float(item['visual_score']):.3f}</td>"
            f"<td>{html.escape(str(item.get('title') or ''))}</td>"
            f"<td>{' ｜ '.join(links)}</td>"
            "</tr>"
        )
    reference_web_path = "../" + str(reference_image.relative_to(run_dir)).replace("\\", "/")
    index_html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>商标图样近似页面</title>
<style>body{{font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif;margin:32px;color:#222}}img{{max-width:240px;border:1px solid #ddd}}table{{border-collapse:collapse;width:100%;margin-top:20px}}th,td{{border:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}}th{{background:#f5f5f5}}.note{{color:#8a5200;background:#fff8e8;padding:10px}}</style></head>
<body><h1>商标图样近似页面</h1><p class="note">本目录用于保留视觉近似调查线索，不自动证明商标实际使用、商品来源或权利人关系。</p>
<h2>企查查视觉基准</h2><img src="{html.escape(reference_web_path)}" alt="企查查商标图样">
<p>视觉阈值：{args.threshold:.2f}；保留页面：{result['retained_count']}。</p>
<table><thead><tr><th>候选</th><th>状态</th><th>分数</th><th>标题</th><th>离线材料</th></tr></thead><tbody>{''.join(index_rows)}</tbody></table>
</body></html>"""
    (retained_root / "index.html").write_text(index_html, encoding="utf-8")
    print(json.dumps({
        "visual_match_complete": True,
        "candidate_count": result["candidate_count"],
        "retained_count": result["retained_count"],
        "output": str(output_path),
        "html_index": str(retained_root / "index.html"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
