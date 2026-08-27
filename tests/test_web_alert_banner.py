"""dashboard 告警横幅：/api/ops/alerts + 首页注入 + ack 确认（评审 §3.5 告警消费）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from predictor.data.storage import Storage
from predictor.web.server import create_app


@pytest.fixture
def client(tmp_path):
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True)
    st = Storage(str(db_dir / "e.db"))
    st.create_schema()
    st.close()
    app = create_app(mode="internal")
    app.state.db_path = str(db_dir / "e.db")
    return TestClient(app)


def _write_alert(tmp_path, name, lines):
    alerts = tmp_path / "data" / "alerts"
    alerts.mkdir(parents=True, exist_ok=True)
    (alerts / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _alert_lines(ts="2026-01-02 00:00:00"):
    return [
        "# Foresight 健康告警",
        "",
        f"检出时间：{ts}",
        "",
        "## 告警",
        "",
        "- LLM 揭晓失败 2 次（24h，api_error/护栏）",
        "",
        "## 全量检查",
        "",
        "- [ok] 全绿",
    ]


def test_no_alerts_no_banner(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "alert-banner" not in r.text
    assert client.get("/api/ops/alerts").json() == {"latest": None}


def test_banner_injected(client, tmp_path):
    _write_alert(tmp_path, "alert-20260102-000000.md", _alert_lines())
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="alert-banner"' in r.text
    assert "LLM 揭晓失败 2 次" in r.text
    info = client.get("/api/ops/alerts").json()["latest"]
    assert info["file"] == "alert-20260102-000000.md"
    assert info["title"] == "Foresight 健康告警"
    assert info["items"] == ["LLM 揭晓失败 2 次（24h，api_error/护栏）"]


def test_banner_content_escaped(client, tmp_path):
    _write_alert(
        tmp_path,
        "alert-20260102-000000.md",
        ["# Foresight 健康告警", "", "检出时间：2026-01-02 00:00:00", "", "## 告警", "",
         '- <script>alert(1)</script> 异常', "", "## 全量检查", ""],
    )
    html = client.get("/").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_ack_renames_and_clears_banner(client, tmp_path):
    _write_alert(tmp_path, "alert-20260102-000000.md", _alert_lines())
    r = client.post("/api/ops/alerts/ack", follow_redirects=False)
    assert r.status_code == 303
    assert (tmp_path / "data" / "alerts" / "alert-20260102-000000.ack.md").exists()
    assert client.get("/api/ops/alerts").json() == {"latest": None}
    assert "alert-banner" not in client.get("/").text


def test_acked_file_excluded_from_latest(client, tmp_path):
    _write_alert(tmp_path, "alert-20260102-000000.md", _alert_lines())
    f = tmp_path / "data" / "alerts" / "alert-20260102-000000.md"
    f.replace(f.with_name("alert-20260102-000000.ack.md"))
    assert client.get("/api/ops/alerts").json() == {"latest": None}
    assert "alert-banner" not in client.get("/").text


def test_latest_unack_chosen_by_name(client, tmp_path):
    _write_alert(tmp_path, "alert-20260102-000000.md", _alert_lines("2026-01-02 00:00:00"))
    _write_alert(
        tmp_path,
        "alert-20260827-164000.md",
        ["# Foresight 健康告警", "", "检出时间：2026-01-02 01:00:00", "", "## 告警", "",
         "- 预测跳过 6 题（24h）", "", "## 全量检查", ""],
    )
    info = client.get("/api/ops/alerts").json()["latest"]
    assert info["file"] == "alert-20260827-164000.md"
    assert "预测跳过 6 题" in client.get("/").text


def test_error_alert_banner_fallback_line(client, tmp_path):
    _write_alert(
        tmp_path,
        "alert-20260827-093553-health-error.md",
        ["# Foresight 健康自检异常（等待轮次锁超时）", "", "检出时间：2026-01-02 00:00:53", "",
         "health_check 排队等待 evolve.lock 超过 2700 秒。", "", "异常：LockWaitTimeout", "",
         "判定建议：人工确认。"],
    )
    r = client.get("/")
    assert 'id="alert-banner"' in r.text
    assert "等待轮次锁超时" in r.text
    info = client.get("/api/ops/alerts").json()["latest"]
    assert info["items"] == ["health_check 排队等待 evolve.lock 超过 2700 秒。"]


def test_public_mode_no_banner_and_no_endpoint(tmp_path):
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True)
    st = Storage(str(db_dir / "e.db"))
    st.create_schema()
    st.close()
    app = create_app(mode="public")
    app.state.db_path = str(db_dir / "e.db")
    c = TestClient(app)
    _write_alert(tmp_path, "alert-20260102-000000.md", _alert_lines())
    assert c.get("/api/ops/alerts").status_code == 404
    assert "alert-banner" not in c.get("/").text
