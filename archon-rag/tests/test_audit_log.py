import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_log


def test_audit_roundtrip(tmp_path):
    audit_log.log_audit(str(tmp_path), "tester", "search", "query", {"k": "v"})
    entries = audit_log.read_audit(str(tmp_path))
    assert len(entries) == 1
    assert entries[0]["action"] == "search"
