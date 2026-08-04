import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docling" / "scripts"))

import docling_wrapper


def _parse(md):
    return docling_wrapper._semantic_chunk(md, "test.md")


def test_default_profile_keeps_results():
    os.environ["ARCHON_PROFILE"] = "default"
    docling_wrapper._chapter_rules_cache = None
    result = _parse("# Intro\n\n## Introduction\nhello\n\n## Results\nvalue\n\n## Conclusion\ndone")
    assert result["status"] == "success"
    headings = [c["heading"] for c in result["chunks"]]
    assert any("Results" in h for h in headings)


def test_water_profile_excludes_results():
    os.environ["ARCHON_PROFILE"] = "water-treatment"
    docling_wrapper._chapter_rules_cache = None
    result = _parse("## 项目背景\nx\n## 结果与讨论\nbad\n## 结论\ngood")
    assert result["status"] == "success"
    headings = [c["heading"] for c in result["chunks"]]
    assert not any("结果与讨论" in h for h in headings)
    os.environ.pop("ARCHON_PROFILE", None)
    docling_wrapper._chapter_rules_cache = None
