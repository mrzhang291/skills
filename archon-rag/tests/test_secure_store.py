import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "secure-storage" / "scripts"))

import secure_store


def test_password_store_roundtrip(tmp_path):
    record = {"id": "1", "source_filename": "a.md", "department": "general", "summary": "s"}
    secure_store.pw_add_record(record, "pass", base_dir=str(tmp_path), department="general")
    records = secure_store.pw_decrypt_store("pass", base_dir=str(tmp_path), department="general")
    assert len(records) == 1
    assert records[0]["id"] == "1"
    assert secure_store.pw_count_records("pass", base_dir=str(tmp_path), department="general") == 1


def test_wrong_password_fails(tmp_path):
    secure_store.pw_add_record({"id": "1"}, "pass", base_dir=str(tmp_path), department="general")
    try:
        secure_store.pw_decrypt_store("wrong", base_dir=str(tmp_path), department="general")
    except ValueError:
        return
    raise AssertionError("wrong password should fail")
