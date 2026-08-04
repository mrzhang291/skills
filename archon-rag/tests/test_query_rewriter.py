import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import query_rewriter


def test_expand_query_cod():
    variants = query_rewriter.expand_query("COD去除率")
    assert "COD去除率" in variants
    assert any("化学需氧量" in v or "chemical oxygen demand" in v.lower() for v in variants)


def test_build_citations():
    records = [{"source_filename": "a.md", "content": "## 结论\n这是原文。"}]
    citations = query_rewriter.build_citations(records)
    assert citations[0]["section"] == "结论"
    assert citations[0]["quote"]
