# -*- coding: utf-8 -*-
"""Persist recruiter scoring drafts before HR confirms archive writes.

This script does not call Feishu. It creates a pending JSON record that can be
shown to HR, updated with feedback, and later passed to feishu_jd.py append-record.
"""
import argparse
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_SKILL_DIR = Path(__file__).parent.parent


def now_compact():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def append_history(skill_dir, entry):
    history_path = skill_dir / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": now_compact(), **entry}
    with history_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def slug_text(value, fallback="candidate"):
    raw = str(value or fallback).strip().lower()
    raw = re.sub(r"[^a-z0-9\u4e00-\u9fff-]+", "-", raw)
    raw = re.sub(r"-+", "-", raw).strip("-")
    return (raw or fallback)[:60]


def relpath(path, base):
    try:
        return str(path.relative_to(base)).replace("\\", "/")
    except ValueError:
        return str(path)


def resolve_pending_path(skill_dir, pending):
    path = Path(pending)
    if path.exists():
        return path
    if not path.is_absolute():
        skill_relative = skill_dir / path
        if skill_relative.exists():
            return skill_relative
    matches = list((skill_dir / "pending").glob(f"**/{pending}.json"))
    if not matches:
        matches = list((skill_dir / "pending").glob(f"**/{pending}-*.json"))
    if not matches and not path.suffix:
        matches = list((skill_dir / "pending").glob(f"**/*{pending}*.json"))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"pending record not found: {pending}")
    raise ValueError(f"pending id is ambiguous: {pending}")


def candidate_from_record(record):
    for key in ("候选人标识", "candidate_id", "candidate", "候选人姓名", "姓名"):
        value = str(record.get(key, "")).strip()
        if value:
            return value
    for key in ("简历文件路径", "简历本地路径", "resume_path", "file_path"):
        value = record.get(key)
        if value:
            return Path(str(value)).stem
    return "未命名候选人"


def cmd_save(args):
    record = read_json(args.record)
    pending_id = f"{now_compact()}-{uuid.uuid4().hex[:8]}"
    candidate = args.candidate or candidate_from_record(record)
    record.update({
        "pending_id": pending_id,
        "pending_status": "pending_hr_feedback",
        "position_id": args.position_id,
        "position_name": args.position_name or record.get("position_name") or args.position_id,
        "候选人标识": record.get("候选人标识") or candidate,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    })
    record.setdefault("评分时间ISO", now_iso())
    path = args.skill_dir / "pending" / args.position_id / f"{pending_id}-{slug_text(candidate)}.json"
    write_json_atomic(path, record)
    append_history(args.skill_dir, {
        "action": "save_pending_score",
        "position_id": args.position_id,
        "candidate": candidate,
        "pending_id": pending_id,
        "pending_file": relpath(path, args.skill_dir),
    })
    print(json.dumps({"ok": True, "pending_id": pending_id, "pending_file": relpath(path, args.skill_dir)}, ensure_ascii=False))


def cmd_update(args):
    path = resolve_pending_path(args.skill_dir, args.pending)
    record = read_json(path)
    before_status = record.get("pending_status")
    if args.hr_feedback is not None:
        record["HR反馈"] = args.hr_feedback
    if args.tier is not None:
        record["tier"] = args.tier
    if args.score is not None:
        record["score"] = args.score
    if args.conclusion is not None:
        record["初筛结论"] = args.conclusion
    if args.status is not None:
        record["pending_status"] = args.status
    record["updated_at"] = now_iso()
    write_json_atomic(path, record)
    append_history(args.skill_dir, {
        "action": "update_pending_score",
        "pending_id": record.get("pending_id"),
        "pending_file": relpath(path, args.skill_dir),
        "status_before": before_status,
        "status_after": record.get("pending_status"),
    })
    print(json.dumps({
        "ok": True,
        "pending_id": record.get("pending_id"),
        "pending_file": relpath(path, args.skill_dir),
        "status": record.get("pending_status"),
    }, ensure_ascii=False))


def cmd_confirm(args):
    path = resolve_pending_path(args.skill_dir, args.pending)
    record = read_json(path)
    record["pending_status"] = "confirmed_for_archive"
    if args.hr_feedback:
        record["HR反馈"] = args.hr_feedback
    record["confirmed_at"] = now_iso()
    record["updated_at"] = now_iso()
    write_json_atomic(path, record)
    append_history(args.skill_dir, {
        "action": "confirm_pending_score",
        "pending_id": record.get("pending_id"),
        "pending_file": relpath(path, args.skill_dir),
    })
    print(json.dumps({"ok": True, "pending_id": record.get("pending_id"), "record_file": relpath(path, args.skill_dir)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="保存招聘初筛待确认评分")
    parser.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("save", help="保存一条待HR确认的评分JSON")
    p.add_argument("--position-id", required=True)
    p.add_argument("--position-name")
    p.add_argument("--candidate")
    p.add_argument("--record", required=True, help="评分 JSON 文件路径")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("update", help="更新待确认评分")
    p.add_argument("--pending", required=True, help="pending_id 或 pending JSON 路径")
    p.add_argument("--hr-feedback")
    p.add_argument("--tier", choices=["reject", "borderline", "pass"])
    p.add_argument("--score", type=int)
    p.add_argument("--conclusion")
    p.add_argument("--status", choices=["pending_hr_feedback", "needs_revision", "confirmed_for_archive", "archived", "discarded"])
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("confirm", help="标记为HR已确认，可传给 feishu_jd.py append-record")
    p.add_argument("--pending", required=True, help="pending_id 或 pending JSON 路径")
    p.add_argument("--hr-feedback")
    p.set_defaults(func=cmd_confirm)

    args = parser.parse_args()
    args.skill_dir = args.skill_dir.resolve()
    try:
        args.func(args)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
