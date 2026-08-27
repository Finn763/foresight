"""ensure_dashboard 探测/拉起决策逻辑单测（mock 探测与拉起，不触碰真实 8765 服务）。

覆盖 CC §3.4 ensure 体系接入的核心决策分支：alive 跳过 / occupied 失败 / down 拉起 /
venv 缺失失败 / 拉起后起不来失败，以及 probe 三态与 launch 命令契约。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ensure_dashboard as ed  # noqa: E402


def test_alive_skips_launch(monkeypatch):
    monkeypatch.setattr(ed, "_log_line", lambda msg: None)
    calls = []

    assert ed.main(probe_fn=lambda: "alive", launch_fn=lambda: calls.append("launch"),
                   wait_fn=lambda: True) == 0
    assert calls == []


def test_occupied_fails_without_launch(monkeypatch):
    monkeypatch.setattr(ed, "_log_line", lambda msg: None)
    calls = []

    assert ed.main(probe_fn=lambda: "occupied", launch_fn=lambda: calls.append("launch"),
                   wait_fn=lambda: True) == 1
    assert calls == []


def test_down_launches_and_returns_0(monkeypatch):
    monkeypatch.setattr(ed, "_log_line", lambda msg: None)
    calls = []

    assert ed.main(probe_fn=lambda: "down", launch_fn=lambda: calls.append("launch"),
                   wait_fn=lambda: True) == 0
    assert calls == ["launch"]


def test_down_launch_but_not_up_fails(monkeypatch):
    monkeypatch.setattr(ed, "_log_line", lambda msg: None)

    assert ed.main(probe_fn=lambda: "down", launch_fn=lambda: None,
                   wait_fn=lambda: False) == 1


def test_down_missing_venv_fails_before_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(ed, "_log_line", lambda msg: None)
    calls = []

    assert ed.main(probe_fn=lambda: "down", launch_fn=lambda: calls.append("launch"),
                   wait_fn=lambda: True, venv=tmp_path / "missing.exe") == 1
    assert calls == []


def test_probe_alive_when_listening_and_http_200(monkeypatch):
    monkeypatch.setattr(ed, "listening", lambda: True)
    monkeypatch.setattr(ed, "_http_ok", lambda: True)
    assert ed.probe() == "alive"


def test_probe_occupied_when_listening_but_http_wrong(monkeypatch):
    monkeypatch.setattr(ed, "listening", lambda: True)
    monkeypatch.setattr(ed, "_http_ok", lambda: False)
    assert ed.probe() == "occupied"


def test_probe_down_when_not_listening(monkeypatch):
    monkeypatch.setattr(ed, "listening", lambda: False)
    assert ed.probe() == "down"


def test_launch_command_contract(monkeypatch, tmp_path):
    """launch 的命令/环境契约：venv python + -E -X utf8、PYTHONPATH 移除、
    PYTHONIOENCODING=utf-8、cwd=项目根、stdout/stderr 进独立服务日志。"""
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(ed, "_LOG", tmp_path / "web_server.log")
    monkeypatch.setattr(ed.subprocess, "Popen", FakePopen)

    ed.launch()

    assert captured["cmd"] == [str(ed.PY), "-E", "-X", "utf8", str(ed.SRV_SCRIPT)]
    kw = captured["kwargs"]
    assert "PYTHONPATH" not in kw["env"]
    assert kw["env"]["PYTHONIOENCODING"] == "utf-8"
    assert kw["cwd"] == str(ed.ROOT)
    assert kw["creationflags"] & 0x00000008  # DETACHED_PROCESS
    assert kw["creationflags"] & 0x08000000  # CREATE_NO_WINDOW
    assert kw["creationflags"] & 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
    assert kw["stdin"] is not None or "stdin" in kw  # DEVNULL 已设
