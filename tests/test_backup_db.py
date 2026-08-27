"""backup_db 脚本单测：文件级拷贝（不连接 DuckDB）+ 保留窗口清理（评审 §3.5）。"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PY = ROOT / ".venv" / "Scripts" / "python.exe"

sys.path.insert(0, str(SCRIPTS))
import backup_db  # noqa: E402


def test_copy_creates_named_backup_with_identical_bytes(tmp_path):
    db = tmp_path / "data" / "foresight.db"
    db.parent.mkdir()
    db.write_bytes(b"\x00duckdb-bytes\x01" * 100)
    dest = backup_db.backup_db(db, tmp_path / "data" / "backup", datetime(2026, 8, 27, 2, 30))
    assert dest.name == "foresight-20260827-0230.db"
    assert dest.read_bytes() == db.read_bytes()
    assert dest.stat().st_mtime == db.stat().st_mtime  # copy2 保留源 mtime


def test_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_db.backup_db(tmp_path / "nope.db", tmp_path / "backup", datetime.now())


def test_prune_removes_stale_keeps_recent(tmp_path):
    bdir = tmp_path / "backup"
    bdir.mkdir()
    stale = bdir / "foresight-20260801-0230.db"
    stale.write_bytes(b"old")
    recent = bdir / "foresight-20260826-0230.db"
    recent.write_bytes(b"new")
    removed = backup_db.prune_old(bdir, datetime(2026, 8, 27, 2, 30), keep_days=7)
    assert removed == [stale]
    assert not stale.exists() and recent.exists()


def test_prune_keeps_boundary_day(tmp_path):
    bdir = tmp_path / "backup"
    bdir.mkdir()
    boundary = bdir / "foresight-20260820-0230.db"  # 27-7=20：边界当日保留
    boundary.write_bytes(b"b")
    assert backup_db.prune_old(bdir, datetime(2026, 8, 27, 2, 30), keep_days=7) == []
    assert boundary.exists()


def test_prune_missing_dir_safe(tmp_path):
    assert backup_db.prune_old(tmp_path / "nope", datetime.now()) == []


def test_cli_end_to_end(tmp_path):
    db = tmp_path / "data" / "foresight.db"
    db.parent.mkdir()
    db.write_bytes(b"x" * 64)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = subprocess.run(
        [str(PY), "-E", "-X", "utf8", str(SCRIPTS / "backup_db.py"), "--db", str(db),
         "--now", "2026-08-27T02:30:00"],
        capture_output=True, env=env, timeout=120, cwd=tmp_path,
    )
    assert r.returncode == 0, r.stdout.decode("utf-8", errors="ignore")
    assert (tmp_path / "data" / "backup" / "foresight-20260827-0230.db").read_bytes() == b"x" * 64


def test_cli_missing_db_exits_1(tmp_path):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = subprocess.run(
        [str(PY), "-E", "-X", "utf8", str(SCRIPTS / "backup_db.py"), "--db",
         str(tmp_path / "nope.db")],
        capture_output=True, env=env, timeout=120, cwd=tmp_path,
    )
    assert r.returncode == 1
