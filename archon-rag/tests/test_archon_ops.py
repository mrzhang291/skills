import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import archon_ops


def test_backup_restore_roundtrip(tmp_path):
    source = tmp_path / "source"
    (source / "shared" / "wiki").mkdir(parents=True)
    (source / "shared" / "wiki" / "index.md").write_text("# index", encoding="utf-8")
    archive = str(tmp_path / "backup.zip")
    result = archon_ops.backup(str(source), archive)
    assert result["status"] == "ok"
    target = tmp_path / "restored"
    restore = archon_ops.restore(archive, str(target))
    assert restore["status"] == "ok"
    assert (target / "shared" / "wiki" / "index.md").exists()


def test_restore_refuses_non_empty_without_force(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    archive = str(tmp_path / "backup.zip")
    archon_ops.backup(str(source), archive)
    target = tmp_path / "target"
    target.mkdir()
    (target / "b.txt").write_text("b", encoding="utf-8")
    result = archon_ops.restore(archive, str(target), force=False)
    assert result["status"] == "error"
