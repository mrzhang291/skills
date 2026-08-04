import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import archon_config


def test_default_profile():
    profile = archon_config.load_profile("default")
    assert profile["name"] == "default"
    assert archon_config.embedding_dim("default") == 512


def test_water_profile_has_exclusions():
    rules = archon_config.chapter_rules("water-treatment")
    assert rules["exclude"]


def test_embedding_model_env():
    os.environ["ARCHON_EMBEDDING_MODEL"] = "BAAI/bge-m3"
    assert archon_config.embedding_model() == "BAAI/bge-m3"
    assert archon_config.embedding_dim() == 1024
    os.environ.pop("ARCHON_EMBEDDING_MODEL", None)
