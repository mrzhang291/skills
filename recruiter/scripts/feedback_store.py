# -*- coding: utf-8 -*-
"""Store recruiter feedback into local examples and preference profiles.

This script does not call Feishu. It keeps the local learning layer stable:
examples/{position_id}/, profiles/global.md, profiles/positions/{position_id}.md,
and history.jsonl.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_SKILL_DIR = Path(__file__).parent.parent
PROFILE_SECTIONS = {
    "hard": "硬性要求",
    "must": "硬性要求",
    "硬性要求": "硬性要求",
    "soft": "软性偏好",
    "preference": "软性偏好",
    "软性偏好": "软性偏好",
    "veto": "否决项",
    "reject": "否决项",
    "否决项": "否决项",
    "compensation": "补偿规则",
    "comp": "补偿规则",
    "补偿规则": "补偿规则",
    "note": "备注",
    "备注": "备注",
    "pending": "待确认偏好",
    "待确认偏好": "待确认偏好",
}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
CN_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
FILE_TOKEN_RE = re.compile(r"(?i)(file[_ -]?token\s*[:=]\s*)[A-Za-z0-9_-]{10,}")


def now_compact():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def relpath(path, base):
    try:
        return str(path.relative_to(base)).replace("\\", "/")
    except ValueError:
        return str(path)


def read_text(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_text_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    tmp.replace(path)


def append_history(skill_dir, entry):
    history_path = skill_dir / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": now_compact(), **entry}
    with history_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def default_profile(title):
    return "\n".join([
        f"# 偏好档案 - {title}",
        "",
        "## 硬性要求（不满足即降分或reject）",
        "- （待HR反馈后自动填充）",
        "",
        "## 软性偏好（影响打分权重）",
        "- （待HR反馈后自动填充）",
        "",
        "## 否决项（一票否决）",
        "- （待HR反馈后自动填充）",
        "",
        "## 补偿规则（A可弥补B）",
        "- （待HR反馈后自动填充）",
        "",
        "## 待确认偏好（不参与评分，HR确认后再移入上方正式区）",
        "- （待HR确认后自动填充）",
        "",
        "## 备注",
        f"- 最后更新: {today()}",
        "- 示例数: 0",
        "",
    ])


def profile_path(skill_dir, scope, position_id=None):
    if scope == "global":
        return skill_dir / "profiles" / "global.md"
    if scope == "position":
        if not position_id:
            raise ValueError("position scope requires --position-id")
        return skill_dir / "profiles" / "positions" / f"{position_id}.md"
    raise ValueError("scope must be global or position")


def ensure_profile(path, title):
    if path.exists():
        return
    write_text_atomic(path, default_profile(title))


def backup_profile(skill_dir, path, reason):
    previous = read_text(path)
    append_history(skill_dir, {
        "action": "backup_profile",
        "reason": reason,
        "file_path": relpath(path, skill_dir),
        "previous_content": previous,
    })


def normalize_section(section):
    key = PROFILE_SECTIONS.get(str(section or "").strip())
    if not key:
        valid = sorted(set(PROFILE_SECTIONS.values()))
        raise ValueError(f"unknown profile section: {section}. valid: {', '.join(valid)}")
    return key


def redact_text(value):
    text = str(value or "")
    text = EMAIL_RE.sub("[email redacted]", text)
    text = CN_MOBILE_RE.sub("[phone redacted]", text)
    text = FILE_TOKEN_RE.sub(r"\1[file token redacted]", text)
    return text


def redact_values(values):
    return [redact_text(value) for value in (values or [])]


def append_bullet_to_section(content, section, bullet):
    section = normalize_section(section)
    lines = content.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and section in line:
            header_idx = i
            break

    if header_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"## {section}", bullet])
        return "\n".join(lines).rstrip() + "\n"

    next_header = len(lines)
    for i in range(header_idx + 1, len(lines)):
        if lines[i].startswith("## "):
            next_header = i
            break

    body = lines[header_idx + 1:next_header]
    placeholders = ("待HR反馈后自动填充", "待HR确认后自动填充")
    body = [line for line in body if not any(marker in line for marker in placeholders)]
    body.append(bullet)
    lines = lines[:header_idx + 1] + body + lines[next_header:]
    return "\n".join(update_last_updated(lines)).rstrip() + "\n"


def update_last_updated(lines):
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("- 最后更新:"):
            lines[i] = f"- 最后更新: {today()}"
            updated = True
    if not updated:
        lines.extend(["", "## 备注", f"- 最后更新: {today()}"])
    return lines


def append_profile_note(skill_dir, scope, position_id, section, note, source, status="confirmed"):
    path = profile_path(skill_dir, scope, position_id)
    title = "全局" if scope == "global" else position_id
    ensure_profile(path, title)
    backup_profile(skill_dir, path, f"append_profile_note:{source}")
    section = normalize_section(section)
    safe_note = redact_text(note)
    if status == "pending":
        bullet = f"- [{section}] {safe_note}（{today()}，来源：{source}，待HR确认，不参与评分）"
        content = append_bullet_to_section(read_text(path), "待确认偏好", bullet)
    else:
        bullet = f"- {safe_note}（{today()}，来源：{source}）"
        content = append_bullet_to_section(read_text(path), section, bullet)
    write_text_atomic(path, content)
    append_history(skill_dir, {
        "action": "append_profile_note",
        "scope": scope,
        "position_id": position_id,
        "section": section,
        "status": status,
        "note": safe_note,
        "file_path": relpath(path, skill_dir),
    })
    return path


def split_lines(values):
    result = []
    for value in values or []:
        for line in str(value).splitlines():
            line = line.strip()
            if line:
                result.append(line)
    return result


def bullet_block(values, fallback="未记录"):
    items = split_lines(values)
    if not items:
        return f"- {fallback}"
    return "\n".join(f"- {item}" for item in items)


def unique_example_path(example_dir, timestamp):
    path = example_dir / f"{timestamp}.md"
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = example_dir / f"{timestamp}-{i}.md"
        if not candidate.exists():
            return candidate
    raise RuntimeError("too many examples with same timestamp")


def add_example(skill_dir, args):
    timestamp = args.timestamp or now_compact()
    example_dir = skill_dir / "examples" / args.position_id
    example_dir.mkdir(parents=True, exist_ok=True)
    path = unique_example_path(example_dir, timestamp)
    hr_score_line = f"- HR修正分数: {args.hr_score}" if args.hr_score is not None else "- HR修正分数: 未修正"
    extracted = redact_text(args.extracted_preference or "无")
    content = "\n".join([
        f"# 示例 - {args.position_name or args.position_id} - {timestamp}",
        "",
        "## 简历摘要",
        f"- 候选人: {redact_text(args.candidate)}",
        "",
        "### 关键经验",
        bullet_block(redact_values(args.key_experience)),
        "",
        "### 核心技能",
        bullet_block(redact_values(args.core_skills)),
        "",
        "## 评分",
        f"- tier: {args.tier}",
        f"- 综合分: {args.score}",
        "",
        "### 主要concerns",
        bullet_block(redact_values(args.concerns)),
        "",
        "## HR反馈",
        hr_score_line,
        f"- HR理由: {redact_text(args.hr_feedback)}",
        f"- 抽取的偏好: {extracted}",
        "",
    ])
    write_text_atomic(path, content)
    append_history(skill_dir, {
        "action": "add_example",
        "position_id": args.position_id,
        "candidate": redact_text(args.candidate),
        "tier": args.tier,
        "score": args.score,
        "example_file": relpath(path, skill_dir),
    })
    removed = prune_examples(skill_dir, args.position_id, args.max_examples)
    return path, removed


def prune_examples(skill_dir, position_id, max_examples):
    example_dir = skill_dir / "examples" / position_id
    if not example_dir.exists():
        return []
    files = sorted(
        [p for p in example_dir.glob("*.md") if p.is_file()],
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    if len(files) <= max_examples:
        return []
    removed = []
    for path in files[:len(files) - max_examples]:
        previous = read_text(path)
        path.unlink()
        removed.append(relpath(path, skill_dir))
        append_history(skill_dir, {
            "action": "prune_example",
            "position_id": position_id,
            "file_path": relpath(path, skill_dir),
            "previous_content": previous,
            "reason": f"max_examples={max_examples}",
        })
    return removed


def cmd_add_example(args):
    path, removed = add_example(args.skill_dir, args)
    print(json.dumps({"ok": True, "example_file": relpath(path, args.skill_dir), "pruned": removed}, ensure_ascii=False))


def cmd_append_profile_note(args):
    path = append_profile_note(
        args.skill_dir,
        args.scope,
        args.position_id,
        args.section,
        args.note,
        args.source,
        args.status,
    )
    print(json.dumps({"ok": True, "profile_file": relpath(path, args.skill_dir)}, ensure_ascii=False))


def cmd_backup_profile(args):
    path = profile_path(args.skill_dir, args.scope, args.position_id)
    ensure_profile(path, "全局" if args.scope == "global" else args.position_id)
    backup_profile(args.skill_dir, path, args.reason)
    print(json.dumps({"ok": True, "profile_file": relpath(path, args.skill_dir)}, ensure_ascii=False))


def cmd_prune_examples(args):
    removed = prune_examples(args.skill_dir, args.position_id, args.max_examples)
    print(json.dumps({"ok": True, "position_id": args.position_id, "pruned": removed}, ensure_ascii=False))


def cmd_add_feedback(args):
    example_path, removed = add_example(args.skill_dir, args)
    profile_file = None
    if args.preference_note and args.preference_scope != "none":
        profile_file = append_profile_note(
            args.skill_dir,
            args.preference_scope,
            args.position_id,
            args.preference_section,
            args.preference_note,
            "HR反馈",
            args.preference_status,
        )
    print(json.dumps({
        "ok": True,
        "example_file": relpath(example_path, args.skill_dir),
        "profile_file": relpath(profile_file, args.skill_dir) if profile_file else None,
        "pruned": removed,
    }, ensure_ascii=False))


def add_common_example_args(parser):
    parser.add_argument("--position-id", required=True)
    parser.add_argument("--position-name")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--tier", required=True, choices=["reject", "borderline", "pass"])
    parser.add_argument("--score", required=True, type=int)
    parser.add_argument("--hr-feedback", required=True)
    parser.add_argument("--hr-score", type=int)
    parser.add_argument("--key-experience", action="append", default=[])
    parser.add_argument("--core-skills", action="append", default=[])
    parser.add_argument("--concerns", action="append", default=[])
    parser.add_argument("--extracted-preference")
    parser.add_argument("--timestamp")
    parser.add_argument("--max-examples", type=int, default=50)


def main():
    parser = argparse.ArgumentParser(description="记录招聘初筛HR反馈")
    parser.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-example", help="写入一条岗位示例")
    add_common_example_args(p)
    p.set_defaults(func=cmd_add_example)

    p = sub.add_parser("append-profile-note", help="向全局或岗位偏好追加一条偏好")
    p.add_argument("--scope", required=True, choices=["global", "position"])
    p.add_argument("--position-id")
    p.add_argument("--section", required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--source", default="HR反馈")
    p.add_argument("--status", choices=["pending", "confirmed"], default="confirmed")
    p.set_defaults(func=cmd_append_profile_note)

    p = sub.add_parser("backup-profile", help="把当前profile完整快照写入history.jsonl")
    p.add_argument("--scope", required=True, choices=["global", "position"])
    p.add_argument("--position-id")
    p.add_argument("--reason", default="manual_backup")
    p.set_defaults(func=cmd_backup_profile)

    p = sub.add_parser("prune-examples", help="按上限清理岗位示例")
    p.add_argument("--position-id", required=True)
    p.add_argument("--max-examples", type=int, default=50)
    p.set_defaults(func=cmd_prune_examples)

    p = sub.add_parser("add-feedback", help="写示例，并可同时追加偏好")
    add_common_example_args(p)
    p.add_argument("--preference-scope", choices=["none", "global", "position"], default="none")
    p.add_argument("--preference-section", default="soft")
    p.add_argument("--preference-note")
    p.add_argument("--preference-status", choices=["pending", "confirmed"], default="pending")
    p.set_defaults(func=cmd_add_feedback)

    args = parser.parse_args()
    args.skill_dir = args.skill_dir.resolve()
    try:
        args.func(args)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
