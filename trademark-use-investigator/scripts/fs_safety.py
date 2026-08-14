#!/usr/bin/env python3

"""Filesystem-boundary checks shared by promotion and final validation."""

from __future__ import annotations

import os
import stat
from pathlib import Path


TRANSACTION_DIR_NAME = ".finalize-transaction"
SENSITIVE_DIR_NAMES = {
    ".private",
    "browser-profile",
    "browser_profile",
    "chrome-profile",
    "chrome_profile",
    "chromium-profile",
    "chromium_profile",
    "user-data-dir",
    "user_data_dir",
    "storage",
    "cookies",
}
SENSITIVE_FILE_NAMES = {
    "browser-state.json",
    "browser_state.json",
    "storage-state.json",
    "storage_state.json",
    "local-storage.json",
    "local_storage.json",
    "session-storage.json",
    "session_storage.json",
    "cookies.json",
    "cookies.sqlite",
    "cookies.sqlite3",
    "cookies.db",
    "cookie.json",
    "credentials.json",
}


def is_reparse_point(path: Path) -> bool:
    """Return true for symlinks, Windows junctions and other reparse points."""
    try:
        if path.is_symlink():
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if isjunction is not None and isjunction(path):
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError:
        return False


def _resolved_inside(path: Path, boundary: Path) -> bool:
    try:
        return path.resolve(strict=True).is_relative_to(boundary.resolve(strict=True))
    except (OSError, RuntimeError):
        return False


def assert_safe_tree(root: Path, run_dir: Path, label: str) -> None:
    """Reject any missing, escaping or reparse-backed root/descendant."""
    run_dir = run_dir.resolve(strict=True)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"{label} directory is missing: {root}")
    if is_reparse_point(root) or not _resolved_inside(root, run_dir):
        raise ValueError(f"{label} root is a reparse point or escapes RUN_DIR: {root}")
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if is_reparse_point(current_path) or not _resolved_inside(current_path, run_dir):
            raise ValueError(f"{label} directory is a reparse point or escapes RUN_DIR: {current_path}")
        for name in [*directories, *files]:
            child = current_path / name
            if is_reparse_point(child) or not _resolved_inside(child, run_dir):
                raise ValueError(f"{label} child is a reparse point or escapes RUN_DIR: {child}")


def sensitive_run_path(path: Path) -> bool:
    name = path.name.casefold()
    if path.is_dir():
        return name in SENSITIVE_DIR_NAMES or name.startswith(("browser-profile-", "user-data-dir-"))
    return name in SENSITIVE_FILE_NAMES


def audit_run_tree(run_dir: Path, *, allow_active_transaction: bool = False) -> None:
    """Reject private browser state, transaction residue and filesystem escapes."""
    run_dir = run_dir.resolve(strict=True)
    if is_reparse_point(run_dir):
        raise ValueError(f"RUN_DIR cannot be a symlink, junction or reparse point: {run_dir}")
    for current, directories, files in os.walk(run_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        if is_reparse_point(current_path) or not _resolved_inside(current_path, run_dir):
            raise ValueError(f"RUN_DIR contains a reparse/escaping directory: {current_path}")
        if current_path != run_dir and sensitive_run_path(current_path):
            raise ValueError(f"RUN_DIR contains sensitive browser state: {current_path.relative_to(run_dir)}")
        kept_directories = []
        for name in directories:
            child = current_path / name
            if child == run_dir / TRANSACTION_DIR_NAME and allow_active_transaction:
                continue
            if child == run_dir / TRANSACTION_DIR_NAME:
                raise ValueError(f"RUN_DIR contains an unfinished finalization transaction: {child}")
            if is_reparse_point(child) or not _resolved_inside(child, run_dir):
                raise ValueError(f"RUN_DIR contains a reparse/escaping directory: {child}")
            if sensitive_run_path(child):
                raise ValueError(f"RUN_DIR contains sensitive browser state: {child.relative_to(run_dir)}")
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in files:
            child = current_path / name
            if is_reparse_point(child) or not _resolved_inside(child, run_dir):
                raise ValueError(f"RUN_DIR contains a reparse/escaping file: {child}")
            if sensitive_run_path(child):
                raise ValueError(f"RUN_DIR contains sensitive browser state: {child.relative_to(run_dir)}")
