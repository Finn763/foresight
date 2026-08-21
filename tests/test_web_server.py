"""web 服务骨架：create_app + health + 请求级短连接。"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from predictor.data.storage import Storage
from predictor.web.server import create_app


@pytest.fixture
def db_path(tmp_path):
    st = Storage(str(tmp_path / "e.db"))
    st.create_schema()
    q = st.add_question("题一", datetime(2026, 9, 1), resolution_class="A")
    st.add_prediction(q, 0.6, evidence_ids=[1], model_runs={"m": [0.6]})
    st.close()
    return str(tmp_path / "e.db")


def test_health_ok(db_path):
    app = create_app(mode="internal")
    app.state.db_path = db_path
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_degraded_when_db_missing():
    app = create_app(mode="internal")
    app.state.db_path = str(Path("不存在.db"))
    with TestClient(app) as c:
        r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"


def test_internal_api_404_in_public_mode(db_path):
    app = create_app(mode="public")
    app.state.db_path = db_path
    with TestClient(app) as c:
        r = c.get("/api/questions")
    assert r.status_code == 404


def test_public_api_served_in_internal_mode(db_path):
    # spec §3.2：internal 模式挂载全部端点（内部 + /api/public/*），
    # ?mode=public 前端视图依赖 internal 启动下对外端点可用。
    app = create_app(mode="internal")
    app.state.db_path = db_path
    with TestClient(app) as c:
        s = c.get("/api/public/summary")
        r = c.get("/api/public/resolved")
    assert s.status_code == 200
    assert s.json()["resolved"] == 0  # 造数无已揭晓题 → 空状态口径
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_index_served(db_path):
    app = create_app(mode="internal")
    app.state.db_path = db_path
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200
    assert "Foresight" in r.text
