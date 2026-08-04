"""Shared profile loader for Archon RAG.

Profiles live in `config/profiles/*.json`. Set `ARCHON_PROFILE` to switch
profiles, or `ARCHON_CONFIG` to point at a custom JSON file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_cache: dict[str, dict] = {}


def skill_root() -> Path:
    """Return the Archon RAG skill root directory."""
    return _SKILL_ROOT


def profile_name(profile: str | None = None) -> str:
    return profile or os.environ.get("ARCHON_PROFILE", "default")


def load_profile(profile: str | None = None) -> dict:
    """Load a profile dict, with a small process-level cache."""
    name = profile_name(profile)
    if name in _cache:
        return _cache[name]

    custom_path = os.environ.get("ARCHON_CONFIG", "")
    if custom_path:
        path = Path(custom_path)
    else:
        path = _SKILL_ROOT / "config" / "profiles" / f"{name}.json"

    if not path.is_file():
        path = _SKILL_ROOT / "config" / "profiles" / "default.json"

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    _cache[name] = data
    return data


def chapter_rules(profile: str | None = None) -> dict:
    return load_profile(profile).get("chapter_rules", {})


def scan_fields(profile: str | None = None) -> dict:
    return load_profile(profile).get("scan_fields", {})


def embedding_model(profile: str | None = None) -> str:
    """Return the configured sentence-transformer model name."""
    env_model = os.environ.get("ARCHON_EMBEDDING_MODEL", "")
    if env_model:
        return env_model
    return load_profile(profile).get("embedding_model", "BAAI/bge-small-zh-v1.5")


def embedding_dim(profile: str | None = None) -> int:
    """Return the expected embedding dimension for the configured model."""
    env_dim = os.environ.get("ARCHON_EMBEDDING_DIM", "")
    if env_dim:
        return int(env_dim)
    env_model = os.environ.get("ARCHON_EMBEDDING_MODEL", "")
    if env_model:
        model = env_model.lower()
        for key, dim in {
            "bge-m3": 1024,
            "bge-large": 1024,
            "bge-base": 768,
            "bge-small": 512,
            "mini-lm": 384,
        }.items():
            if key in model:
                return dim
    profile_dim = load_profile(profile).get("embedding_dim")
    if profile_dim:
        return int(profile_dim)
    model = embedding_model(profile).lower()
    for key, dim in {
        "bge-m3": 1024,
        "bge-large": 1024,
        "bge-base": 768,
        "bge-small": 512,
        "mini-lm": 384,
    }.items():
        if key in model:
            return dim
    return 1024


def allowed_sections_hint(profile: str | None = None) -> str:
    """Build a short prompt hint describing the active include/exclude rules."""
    rules = chapter_rules(profile)
    include = [item.get("canonical", "") for item in rules.get("include", [])]
    exclude = [item.get("canonical", "") for item in rules.get("exclude", [])]
    parts = [f"Allowed sections: {', '.join(include) or '(all)'}"]
    if exclude:
        parts.append(f"Excluded sections: {', '.join(exclude)}")
    return " | ".join(parts)
