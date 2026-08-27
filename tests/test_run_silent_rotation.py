"""run_silent 日志轮转单测：1MiB 上限、.1 后缀重开、append 语义保留（评审 §3.6）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_silent as rs  # noqa: E402


def test_under_limit_no_rotation(tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(b"small")
    assert rs.rotate_log_if_needed(log) is False
    assert log.read_bytes() == b"small"
    assert not log.with_name("a.log.1").exists()


def test_over_limit_rotates_to_dot1(tmp_path):
    log = tmp_path / "a.log"
    payload = b"x" * (rs.LOG_MAX_BYTES + 1)
    log.write_bytes(payload)
    assert rs.rotate_log_if_needed(log) is True
    assert log.with_name("a.log.1").read_bytes() == payload
    assert not log.exists()


def test_second_rotation_replaces_dot1(tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(b"f" * (rs.LOG_MAX_BYTES + 1))
    rs.rotate_log_if_needed(log)
    log.write_bytes(b"s" * (rs.LOG_MAX_BYTES + 1))
    assert rs.rotate_log_if_needed(log) is True
    assert log.with_name("a.log.1").read_bytes().startswith(b"s")
    assert not log.exists()


def test_missing_log_noop(tmp_path):
    assert rs.rotate_log_if_needed(tmp_path / "nope.log") is False
