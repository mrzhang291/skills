#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import traceback
from pathlib import Path


WORD_EXTS = {".doc", ".docx"}
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def emit(payload: dict, exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


def file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in WORD_EXTS:
        return "word"
    if suffix in PDF_EXTS:
        return "pdf"
    if suffix in IMAGE_EXTS:
        return "image"
    return "other"


def default_pdf_path(source: Path, output_dir: str | None) -> Path:
    if output_dir:
        target_dir = Path(output_dir).expanduser()
    else:
        target_dir = source.parent / "_converted_pdf"
    target_dir.mkdir(parents=True, exist_ok=True)
    return (target_dir / f"{source.stem}.pdf").resolve()


def pdf_is_current(source: Path, target: Path) -> bool:
    if not target.exists() or target.stat().st_size <= 0:
        return False
    return target.stat().st_mtime >= source.stat().st_mtime


def convert_with_docx2pdf(source: Path, target: Path) -> None:
    from docx2pdf import convert

    # docx2pdf may print progress bars; keep stdout clean for JSON callers.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        convert(str(source), str(target))


def convert_with_word_com(source: Path, target: Path) -> None:
    try:
        import pythoncom
    except Exception:
        pythoncom = None

    import win32com.client

    if pythoncom is not None:
        pythoncom.CoInitialize()

    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(
            str(source),
            ReadOnly=True,
            ConfirmConversions=False,
            AddToRecentFiles=False,
        )
        if hasattr(doc, "SaveAs2"):
            doc.SaveAs2(str(target), FileFormat=17)
        else:
            doc.SaveAs(str(target), FileFormat=17)
    finally:
        if doc is not None:
            try:
                doc.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        if pythoncom is not None:
            pythoncom.CoUninitialize()


def convert_word_to_pdf(source: Path, target: Path, force: bool) -> tuple[Path, str, bool]:
    if not force and pdf_is_current(source, target):
        return target, "existing-current-pdf", True

    if target.exists():
        target.unlink()

    errors: list[str] = []
    methods = ["word-com"] if source.suffix.lower() == ".doc" else ["docx2pdf", "word-com"]

    for method in methods:
        try:
            if method == "docx2pdf":
                convert_with_docx2pdf(source, target)
            else:
                convert_with_word_com(source, target)
            if target.exists() and target.stat().st_size > 0:
                return target, method, False
            errors.append(f"{method}: no PDF output was created")
        except Exception as exc:
            errors.append(f"{method}: {exc}")

    raise RuntimeError("; ".join(errors))


def prepare_resume_file(file_path: str, output_dir: str | None, force: bool) -> dict:
    source = Path(file_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"File not found: {source}")
    if not source.is_file():
        raise ValueError(f"Not a file: {source}")

    kind = file_kind(source)
    if kind != "word":
        return {
            "ok": True,
            "converted": False,
            "reused_existing_pdf": False,
            "file_type": kind,
            "input_file": str(source),
            "original_file": str(source),
            "reading_file": str(source),
            "archive_file": str(source),
            "pending_fields": {
                "简历文件路径": str(source),
                "原始简历文件路径": str(source),
            },
        }

    target = default_pdf_path(source, output_dir)
    pdf_path, method, reused = convert_word_to_pdf(source, target, force)
    return {
        "ok": True,
        "converted": True,
        "reused_existing_pdf": reused,
        "file_type": "word",
        "conversion_method": method,
        "input_file": str(source),
        "original_file": str(source),
        "pdf_file": str(pdf_path),
        "reading_file": str(pdf_path),
        "archive_file": str(pdf_path),
        "pending_fields": {
            "简历文件路径": str(pdf_path),
            "原始简历文件路径": str(source),
            "文件预处理": f"Word converted to PDF via {method}",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare an HR-uploaded resume/portfolio file before recruiter scoring."
    )
    parser.add_argument("--file", required=True, help="Local resume or portfolio file path")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for converted PDFs. Defaults to a _converted_pdf folder next to the source file.",
    )
    parser.add_argument("--force", action="store_true", help="Recreate the PDF even when a current one exists")
    return parser


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        return emit(prepare_resume_file(args.file, args.output_dir, args.force))
    except Exception as exc:
        return emit(
            {
                "ok": False,
                "error": str(exc),
                "detail": traceback.format_exc(limit=3),
                "human_message": "Word 简历转 PDF 失败，请让 HR 重新上传 PDF，或先在 Word/WPS 中手动导出 PDF 后再评分。",
            },
            exit_code=1,
        )


if __name__ == "__main__":
    raise SystemExit(main())
