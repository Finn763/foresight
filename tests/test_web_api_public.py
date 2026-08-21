"""对外 API：仅已揭晓题 + 字段白名单（无 arm/spec/evidence/model_runs/未揭晓题）。"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

from predictor.data.storage import Storage
from predictor.web.server import create_app


@pytest.fixture
def client(tmp_path):
    st = Storage(str(tmp_path / "e.db"))
    st.create_schema()
    # 日期全部动态（now±N 天）——硬编码日期是延迟归档时间炸弹（项目已有两次教训）
    now = datetime.now()
    q1 = st.add_question("已揭晓题", now + timedelta(days=2), resolution_class="A")
    st.add_prediction(q1, 0.7, evidence_ids=[1], model_runs={"m": [0.7]})
    st.resolve_question(q1, outcome=True, resolution_source="sina")
    q2 = st.add_question("未揭晓题", now + timedelta(days=2), resolution_class="C")
    st.add_prediction(q2, 0.9, evidence_ids=[2], model_runs={"m": [0.9]})
    st.add_document(q2, "gdelt", "https://x", "证据", "内容", published_at=None)
    # I-1 场景：同一题 2 条预测（baseline 0.7 先 + experiment 0.9 后，模拟 P1 注册
    # 候选杠杆后 predict_round 先臂 A 后臂 B）→ 对外榜必须取 baseline 行
    # （resolve 只给「最后一条 baseline 行」计分 → brier_score=0.09 非 NULL）。
    q3 = st.add_question("双臂已揭晓题", now + timedelta(days=2), resolution_class="B")
    st.add_prediction(q3, 0.7, evidence_ids=[3], model_runs={"m": [0.7]})
    st.add_prediction(q3, 0.9, evidence_ids=[4], model_runs={"m": [0.9]}, arm="experiment")
    st.resolve_question(q3, outcome=True, resolution_source="sina")
    # I-2 场景：回测题（is_public=False，compare_backtest 写入）已揭晓也不进对外榜
    q4 = st.add_question("回测题", now + timedelta(days=2), resolution_class="B", is_public=False)
    st.add_prediction(q4, 0.6, evidence_ids=[5], model_runs={"m": [0.6]})
    st.resolve_question(q4, outcome=True, resolution_source="sina", force_score=True)
    st.close()
    app = create_app(mode="public")
    app.state.db_path = str(tmp_path / "e.db")
    return TestClient(app)


def test_public_summary(client):
    s = client.get("/api/public/summary").json()
    # 已揭晓且公开：q1 + q3 = 2；q4（回测 is_public=False）不计入
    assert s["resolved"] == 2
    assert s["brier_mean"] == pytest.approx(0.09)
    assert "buckets" in s


def test_public_resolved_whitelist(client):
    items = client.get("/api/public/resolved").json()["items"]
    assert len(items) == 2
    by_title = {i["title"]: i for i in items}
    assert by_title["已揭晓题"]["brier_score"] == pytest.approx(0.09)
    assert set(by_title["已揭晓题"]) == {
        "id",
        "title",
        "closes_at",
        "probability",
        "outcome",
        "brier_score",
        "resolved_at",
    }
    assert "回测题" not in by_title


def test_public_board_uses_baseline_arm_row(client):
    # I-1：同题双臂预测 → 对外榜取「最后一条 baseline 行」（0.7 + resolve 计分 0.09），
    # 而非最新一条 experiment 行（0.9 / brier_score NULL）
    items = client.get("/api/public/resolved").json()["items"]
    q = next(i for i in items if i["title"] == "双臂已揭晓题")
    assert q["probability"] == pytest.approx(0.7)
    assert q["brier_score"] == pytest.approx(0.09)


def test_public_board_excludes_backtest_questions(client):
    # I-2：回测题（is_public=False）已揭晓也不进对外榜 items / summary.resolved
    items = client.get("/api/public/resolved").json()["items"]
    assert all(i["title"] != "回测题" for i in items)
    s = client.get("/api/public/summary").json()
    assert s["resolved"] == 2


def test_public_mode_internal_api_404(client):
    assert client.get("/api/questions").status_code == 404
    assert client.get("/api/scoreboard").status_code == 404
    assert client.get("/api/system").status_code == 404
    # 前端 HEAD 探测在 public 模式同样 404 → 正确锁定对外视图（Task 8 回归）
    assert client.head("/api/questions").status_code == 404


def test_public_mode_still_serves_health_and_index(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200


def test_public_mode_ops_endpoints_404(client):
    assert client.get("/api/ops/log").status_code == 404
    assert client.get("/api/ops/health").status_code == 404
    assert client.post("/api/ops/health/refresh").status_code == 404
    assert client.get("/api/ops/log-files", params={"name": "daily"}).status_code == 404
