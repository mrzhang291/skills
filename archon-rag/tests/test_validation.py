import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import archon_validation


def test_scan_validation_rejects_empty():
    result = archon_validation.validate_scan_payload({})
    assert result["valid"] is False
    assert any("missing_field" in e for e in result["errors"])


def test_scan_validation_accepts_complete():
    payload = {
        "tags": ["a"],
        "summary": "summary",
        "entities": ["e"],
        "key_data": {"k": "v"},
        "client_name": "c",
        "project_name": "p",
        "product_capacity": "cap",
        "quality_summary": "q",
        "objective": "o",
        "process": "proc",
        "_docling_chunks": [{"heading": "Introduction", "content": "x"}],
    }
    result = archon_validation.validate_scan_payload(payload)
    assert result["valid"] is True


def test_finalize_validation_detects_excluded_chapter():
    payload = {
        "tags": ["a"], "summary": "s", "entities": ["e"], "key_data": {"k": "v"},
        "client_name": "c", "project_name": "p", "product_capacity": "cap",
        "quality_summary": "q", "objective": "o", "process": "proc",
        "_docling_chunks": [{"heading": "结果与讨论", "content": "bad"}],
    }
    result = archon_validation.validate_finalize_payload(payload, "water-treatment")
    assert result["valid"] is False
    assert result["excluded_headings"]
