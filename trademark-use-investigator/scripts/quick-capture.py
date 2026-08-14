#!/usr/bin/env python3

"""Capture the content-valid Quick shortlist with a small, fail-forward budget."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Callable

from url_utils import normalize_url
from process_utils import run_bounded


DEFAULT_CAPTURE_TIMEOUT_MS = 20_000
DEFAULT_WALL_TIMEOUT_SECONDS = 120.0
SUMMARY_RELATIVE_PATH = Path("capture") / "quick-capture-summary.json"


class QuickCaptureConfigError(ValueError):
    """Raised when a run cannot safely use the Quick capture workflow."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    if not path.is_file():
        raise QuickCaptureConfigError(f"Required input not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QuickCaptureConfigError(f"Cannot read JSON input {path}: {error}") from error
    if not isinstance(value, dict):
        raise QuickCaptureConfigError(f"Expected a JSON object: {path}")
    return value


def read_attempts(run_dir: Path) -> list[dict]:
    path = run_dir / "capture-attempts.jsonl"
    if not path.is_file():
        return []
    records: list[dict] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("record is not an object")
            records.append(value)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise QuickCaptureConfigError(f"Cannot read {path}: {error}") from error
    return records


def positive_integer(container: dict, name: str, *, default: int | None = None) -> int:
    value = container.get(name, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise QuickCaptureConfigError(f"{name} must be a positive integer") from error
    if parsed < 1:
        raise QuickCaptureConfigError(f"{name} must be a positive integer")
    return parsed


def safe_probe_metadata(run_dir: Path, item: dict) -> dict | None:
    """Return matching content-valid probe metadata, never trusting the shortlist alone."""
    probe = item.get("probe") or {}
    if not isinstance(probe, dict):
        return None
    relative = str(probe.get("probe_dir") or "").strip()
    if not relative:
        return None
    probe_dir = (run_dir / relative).resolve()
    try:
        probe_dir.relative_to(run_dir.resolve())
    except ValueError:
        return None
    metadata_path = probe_dir / "metadata.json"
    if not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    normalized = normalize_url(str(item.get("normalized_url") or item.get("url") or ""))
    if not normalized:
        return None
    if (
        metadata.get("content_valid") is not True
        or metadata.get("probe_id") != probe.get("probe_id")
        or metadata.get("frontier_id") != item.get("target_id")
        or normalize_url(str(metadata.get("requested_normalized_url") or "")) != normalized
    ):
        return None
    return metadata


def ordered_shortlist_items(document: dict) -> list[dict]:
    items = [dict(item) for item in (document.get("items") or []) if isinstance(item, dict)]

    def order(item: dict) -> tuple:
        try:
            rank = int(item.get("rank"))
        except (TypeError, ValueError):
            rank = 10**9
        return rank, str(item.get("target_id") or ""), str(item.get("normalized_url") or item.get("url") or "")

    return sorted(items, key=order)


def expected_text_any(config: dict) -> str:
    trademark = config.get("trademark") or {}
    values: list[str] = []
    for value in (
        trademark.get("name"),
        trademark.get("registration_number"),
        trademark.get("owner"),
    ):
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)
    if not values:
        raise QuickCaptureConfigError(
            "At least one trademark name, registration number, or owner is required"
        )
    return "|||".join(values)


def page_type(item: dict) -> str:
    """Infer a conservative evidence type from recorded signals and the URL."""
    url = str(item.get("normalized_url") or item.get("url") or "").casefold()
    title = str(item.get("title") or "").casefold()
    signals = item.get("matched_signals") or {}
    commercial = signals.get("commercial") or item.get("commercial") or []
    if isinstance(commercial, str):
        commercial = [commercial]
    text = f"{url} {title}"
    if any(token in text for token in (
        "cnipa", "sbj.cnipa", "tmview", "/trademark/", "/registry/",
        "商标注册", "注册号", "申请号",
    )):
        return "registry"
    if commercial or any(token in text for token in (
        "/item/", "/items/", "/product/", "/products/", "/detail/", "/goods/",
        "shop", "store", "mall", "1688.com", "taobao.com", "tmall.com", "jd.com",
        "商品", "产品", "价格", "购买", "店铺",
    )):
        return "product"
    if signals.get("owner") or any(token in text for token in (
        "gsxt", "qcc.com", "tianyancha", "aiqicha", "/company/", "/corp/",
        "/enterprise/", "company", "about-us", "公司", "企业信息", "工商",
    )):
        return "company"
    if any(token in text for token in (
        "/article/", "/news/", "/post/", "/blog/", "article", "news", "新闻", "资讯", "文章",
    )):
        return "article"
    return "other"


def parse_capture_payload(stdout: str) -> dict:
    try:
        value = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def run_capture(command: list[str], wall_timeout_seconds: float) -> subprocess.CompletedProcess:
    return run_bounded(command, timeout=wall_timeout_seconds)


def accepted_attempt(record: dict) -> bool:
    return record.get("status") == "accepted_candidate" and record.get("content_valid") is True


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture content-valid Quick probes until the valid-page target or attempt budget is reached"
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--browser-executable")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_CAPTURE_TIMEOUT_MS)
    parser.add_argument("--wall-clock-timeout-seconds", type=float, default=DEFAULT_WALL_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    if args.timeout_ms < 1 or args.timeout_ms > 30_000:
        parser.error("--timeout-ms must be between 1 and 30000 for Quick capture")
    if args.wall_clock_timeout_seconds <= 0:
        parser.error("--wall-clock-timeout-seconds must be positive")
    return args


def summary_template(run_dir: Path) -> dict:
    return {
        "schema_version": "1.0",
        "record_type": "quick_capture_summary",
        "generated_at": utc_now(),
        "run_dir": str(run_dir),
        "execution_profile": None,
        "limits": {},
        "summary": {
            "shortlist_count": 0,
            "eligible_count": 0,
            "attempted_this_run": 0,
            "accepted_this_run": 0,
            "accepted_total": 0,
            "target_met": False,
            "budget_exhausted": False,
        },
        "items": [],
        "skipped_links": [],
        "fatal_error": None,
    }


def write_summary(run_dir: Path, summary: dict) -> Path:
    output = run_dir / SUMMARY_RELATIVE_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    summary["generated_at"] = utc_now()
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def execute(
    argv: list[str],
    *,
    runner: Callable[[list[str], float], subprocess.CompletedProcess] = run_capture,
) -> int:
    args = parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    summary = summary_template(run_dir)
    output_path: Path | None = None

    try:
        config = read_json(run_dir / "run-config.json")
        summary["run_id"] = config.get("run_id")
        summary["execution_profile"] = config.get("execution_profile")
        if config.get("execution_profile") != "quick":
            raise QuickCaptureConfigError("quick-capture.py only accepts execution_profile=quick")

        shortlist = read_json(run_dir / "discovery" / "probe-shortlist.json")
        raw_items = ordered_shortlist_items(shortlist)
        budgets = config.get("budgets") or {}
        requirements = config.get("coverage_requirements") or {}
        max_attempts = positive_integer(budgets, "max_capture_attempts")
        minimum_normal = positive_integer(requirements, "min_normal_target_pages")
        target_pages = positive_integer(
            budgets, "target_full_pages", default=minimum_normal,
        )
        if target_pages < minimum_normal:
            raise QuickCaptureConfigError(
                "target_full_pages cannot be lower than min_normal_target_pages"
            )
        if target_pages > max_attempts:
            raise QuickCaptureConfigError(
                "target_full_pages cannot exceed max_capture_attempts"
            )
        anchors = expected_text_any(config)
        visual_reference_ready = (run_dir / "reference" / "qcc-reference.json").is_file()
        summary["limits"] = {
            "max_capture_attempts": max_attempts,
            "target_full_pages": target_pages,
            "min_normal_target_pages": minimum_normal,
            "timeout_ms": args.timeout_ms,
            "wall_clock_timeout_seconds": args.wall_clock_timeout_seconds,
            "capture_images": visual_reference_ready,
            "headed": False,
            "retry_failed_url": False,
        }
        summary["summary"]["shortlist_count"] = len(raw_items)

        valid_items: list[dict] = []
        invalid_by_identity: dict[int, str] = {}
        for item in raw_items:
            metadata = safe_probe_metadata(run_dir, item)
            if metadata is None:
                invalid_by_identity[id(item)] = "probe_not_content_valid_or_mismatched"
            else:
                valid_items.append(item)
        summary["summary"]["eligible_count"] = len(valid_items)

        attempts = read_attempts(run_dir)
        existing_by_url = {
            normalized: record
            for record in attempts
            if (normalized := normalize_url(str(record.get("url") or "")))
        }
        accepted_urls = {
            normalized
            for record in attempts
            if accepted_attempt(record)
            and (normalized := normalize_url(str(record.get("url") or "")))
        }
        attempted_urls = set(existing_by_url)
        capture_script = Path(__file__).resolve().with_name("capture-target-page.py")
        accepted_this_run = 0
        attempted_this_run = 0

        valid_position_by_identity = {id(item): index for index, item in enumerate(valid_items, start=1)}
        for item in raw_items:
            normalized = normalize_url(str(item.get("normalized_url") or item.get("url") or ""))
            entry = {
                "rank": item.get("rank"),
                "candidate_id": None,
                "frontier_id": item.get("target_id"),
                "probe_id": (item.get("probe") or {}).get("probe_id"),
                "url": normalized or str(item.get("url") or item.get("normalized_url") or ""),
                "label": str(item.get("title") or normalized or item.get("url") or ""),
                "page_type": page_type(item),
                "status": None,
                "returncode": None,
            }
            if id(item) in invalid_by_identity:
                entry["status"] = "skipped_invalid_probe"
                entry["reason"] = invalid_by_identity[id(item)]
            else:
                candidate_number = valid_position_by_identity[id(item)]
                entry["candidate_id"] = f"C{candidate_number:03d}"
                previous = existing_by_url.get(normalized or "")
                if previous:
                    entry["status"] = "already_accepted" if accepted_attempt(previous) else "skipped_already_attempted"
                    entry["returncode"] = 0 if accepted_attempt(previous) else None
                    entry["previous_status"] = previous.get("status")
                    entry["reason"] = "url_was_already_attempted_no_retry"
                elif len(accepted_urls) >= target_pages:
                    entry["status"] = "skipped_target_reached"
                    entry["reason"] = "target_full_pages_reached"
                elif len(attempted_urls) >= max_attempts:
                    entry["status"] = "skipped_budget_exhausted"
                    entry["reason"] = "max_capture_attempts_reached"
                else:
                    command = [
                        sys.executable,
                        str(capture_script),
                        "--run-dir", str(run_dir),
                        "--candidate-id", entry["candidate_id"],
                        "--frontier-id", str(entry["frontier_id"]),
                        "--probe-id", str(entry["probe_id"]),
                        "--url", str(normalized),
                        "--label", entry["label"],
                        "--page-type", entry["page_type"],
                        "--expected-text-any", anchors,
                        "--timeout-ms", str(args.timeout_ms),
                    ]
                    if visual_reference_ready:
                        command.extend(["--allow-image-only", "--capture-images"])
                    if args.browser_executable:
                        command.extend(["--browser-executable", args.browser_executable])
                    completed = runner(command, args.wall_clock_timeout_seconds)
                    attempted_urls.add(str(normalized))
                    attempted_this_run += 1
                    entry["returncode"] = int(completed.returncode)
                    payload = parse_capture_payload(completed.stdout or "")
                    entry["page_state"] = payload.get("page_state")
                    entry["stdout_tail"] = (completed.stdout or "")[-1000:]
                    entry["stderr_tail"] = (completed.stderr or "")[-1000:]
                    if completed.returncode == 0 and payload.get("accepted", True) is not False:
                        entry["status"] = "accepted"
                        accepted_urls.add(str(normalized))
                        accepted_this_run += 1
                    elif completed.returncode == 124:
                        entry["status"] = "subprocess_timeout"
                        entry["reason"] = "wall_clock_timeout_no_retry"
                    else:
                        state = str(payload.get("page_state") or "capture_failed")
                        entry["status"] = state
                        entry["reason"] = "capture_failed_no_retry"

            summary["items"].append(entry)
            if entry["status"] not in {"accepted", "already_accepted"}:
                summary["skipped_links"].append({
                    "rank": entry.get("rank"),
                    "candidate_id": entry.get("candidate_id"),
                    "url": entry.get("url"),
                    "status": entry.get("status"),
                    "reason": entry.get("reason"),
                    "returncode": entry.get("returncode"),
                })

        summary["summary"].update({
            "attempted_this_run": attempted_this_run,
            "accepted_this_run": accepted_this_run,
            "accepted_total": len(accepted_urls),
            "target_met": len(accepted_urls) >= target_pages,
            "minimum_coverage_met": len(accepted_urls) >= minimum_normal,
            "budget_exhausted": len(attempted_urls) >= max_attempts,
        })
        if (run_dir / "reference" / "qcc-reference.json").is_file():
            visual_script = Path(__file__).resolve().with_name("retain-visual-mark-matches.py")
            visual_completed = run_bounded(
                [sys.executable, str(visual_script), "--run-dir", str(run_dir)],
                timeout=120,
            )
            visual_path = run_dir / "visual-match-results.json"
            visual_data = {}
            if visual_path.is_file():
                try:
                    visual_data = json.loads(visual_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    visual_data = {}
            summary["visual_match"] = {
                "status": "complete" if visual_completed.returncode == 0 else "failed",
                "retained_count": int(visual_data.get("retained_count") or 0),
                "visual_near_match_count": int(visual_data.get("visual_near_match_count") or 0),
                "text_review_count": int(visual_data.get("text_review_count") or 0),
                "output": str(visual_path.relative_to(run_dir)).replace("\\", "/") if visual_path.is_file() else None,
            }
        output_path = write_summary(run_dir, summary)
        print(json.dumps({
            "quick_capture_complete": True,
            "output": str(output_path),
            **summary["summary"],
        }, ensure_ascii=False, indent=2))
        return 0 if len(accepted_urls) >= minimum_normal else 4
    except (QuickCaptureConfigError, OSError) as error:
        summary["fatal_error"] = str(error)
        try:
            output_path = write_summary(run_dir, summary)
        except OSError:
            output_path = None
        print(json.dumps({
            "quick_capture_complete": False,
            "output": str(output_path) if output_path else None,
            "error": str(error),
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    return execute(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
