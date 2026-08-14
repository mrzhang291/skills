#!/usr/bin/env python3

"""Build a resumable human-operated sales-platform capture queue."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sales_platforms import FILING_PLATFORM_ORDER, PLATFORMS, platform_search_url


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def cleaned_goods(config: dict) -> list[str]:
    trademark = config.get("trademark") or {}
    goods = [clean(item) for item in trademark.get("goods_services") or [] if clean(item)]
    goods = [re.sub(r"[（(][一二三四五六七八九十0-9]+[）)]$", "", item).strip() for item in goods]
    return list(dict.fromkeys(item for item in goods if item))


def build(
    run_dir: Path,
    platforms: list[str] | None = None,
    include_mark_only: bool = True,
    force: bool = False,
) -> tuple[Path, Path, Path, dict]:
    run_dir = run_dir.resolve()
    config = read_json(run_dir / "run-config.json")
    if not isinstance(config, dict):
        raise FileNotFoundError(f"run-config.json not found or invalid: {run_dir}")

    selected = list(dict.fromkeys(platforms or FILING_PLATFORM_ORDER))
    unknown = [item for item in selected if item not in PLATFORMS]
    if unknown:
        raise ValueError(f"Unsupported sales platform(s): {', '.join(unknown)}")
    unsupported = []
    for platform in selected:
        try:
            platform_search_url(platform, "test")
        except ValueError:
            unsupported.append(platform)
    if unsupported:
        raise ValueError(f"No human search URL is configured for: {', '.join(unsupported)}")

    discovery_dir = run_dir / "discovery"
    queue_path = discovery_dir / "manual-capture-queue.json"
    state_path = discovery_dir / "manual-capture-state.json"
    markdown_path = discovery_dir / "manual-capture-queue.md"
    if not force and queue_path.is_file() and state_path.is_file():
        existing = read_json(queue_path, {}) or {}
        return queue_path, state_path, markdown_path, existing

    trademark = config.get("trademark") or {}
    mark = clean(trademark.get("name"))
    if not mark:
        raise ValueError("run-config.json trademark.name is required")
    goods = cleaned_goods(config)
    terms: list[tuple[str, str | None]] = []
    if include_mark_only:
        terms.append((mark, None))
    terms.extend((clean(f"{mark} {good}"), good) for good in goods)
    if not terms:
        terms.append((mark, None))

    items = []
    for platform in selected:
        platform_config = PLATFORMS[platform]
        for query, target_good in terms:
            task_id = f"MC{len(items) + 1:03d}"
            items.append({
                "task_id": task_id,
                "order": len(items) + 1,
                "platform": platform,
                "platform_label": platform_config["label"],
                "query": query,
                "target_good": target_good,
                "query_kind": "mark_only" if target_good is None else "mark_plus_good",
                "search_url": platform_search_url(platform, query),
                "allowed_domains": list(platform_config["domains"]),
                "status": "pending",
                "attempt_count": 0,
                "captures": [],
            })

    now = datetime.now(timezone.utc).isoformat()
    queue = {
        "schema_version": "1.0",
        "record_type": "manual_sales_capture_queue",
        "run_id": config.get("run_id"),
        "created_at": now,
        "updated_at": now,
        "platforms": selected,
        "platform_count": len(selected),
        "include_mark_only": include_mark_only,
        "continuous_pages_required": False,
        "task_count": len(items),
        "evidence_level": "related_lead_not_formal_use_evidence",
        "items": items,
    }
    state = {
        "schema_version": "1.0",
        "record_type": "manual_sales_capture_state",
        "run_id": config.get("run_id"),
        "phase": "ready",
        "created_at": now,
        "updated_at": now,
        "current_task_id": items[0]["task_id"] if items else None,
        "completed_count": 0,
        "blocked_count": 0,
        "pending_count": len(items),
        "capture_mode": "human_navigation_local_extension",
    }
    atomic_write_json(queue_path, queue)
    atomic_write_json(state_path, state)

    lines = [
        f"# {mark}：人工检索与一键固证任务",
        "",
        "真人负责登录、验证和相关性判断；本地扩展只在人工点击时归档当前页。搜索结果页属于相关线索，不自动等同商标实际使用证据。",
        "",
        f"共 {len(items)} 项，{len(selected)} 个平台；不要求连续五页。",
        "",
        "| 任务 | 平台 | 查询词 | 核定商品 | 状态 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in items:
        target_good = item["target_good"] or "仅商标名"
        lines.append(
            f"| {item['task_id']} | {item['platform_label']} | "
            f"[{item['query']}]({item['search_url']}) | {target_good} | 待处理 |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return queue_path, state_path, markdown_path, queue


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a human-operated sales capture queue")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--no-mark-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    queue_path, state_path, markdown_path, queue = build(
        Path(args.run_dir), args.platform or None, not args.no_mark_only, args.force
    )
    print(json.dumps({
        "manual_capture_queue_ready": True,
        "task_count": queue.get("task_count"),
        "platform_count": queue.get("platform_count"),
        "continuous_pages_required": False,
        "queue": str(queue_path),
        "state": str(state_path),
        "markdown": str(markdown_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
