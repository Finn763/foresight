"""内部 API：列表/筛选/详情/scoreboard/system。"""

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
    now = datetime.now()
    q1 = st.add_question("已揭晓题", datetime(2026, 8, 1), resolution_class="A")
    st.add_prediction(q1, 0.7, evidence_ids=[1], model_runs={"m": [0.7]})
    st.resolve_question(q1, outcome=True, resolution_source="sina")
    # 动态日期（now+2d）保持「进行中 open」语义：硬编码 2026-09-01 过点后 q2 会
    # 从 open 漂移成 pending（时间炸弹，项目已有两次教训）
    q2 = st.add_question("进行中题", now + timedelta(days=2), resolution_class="C")
    st.add_prediction(q2, 0.5, evidence_ids=[2], model_runs={"m": [0.5]})
    # pending 态题（closes 已过、未揭晓），与 test_web_api_public 动态 seed 模式一致
    q3 = st.add_question("已过期未揭晓题", now - timedelta(days=1), resolution_class="D")
    st.add_prediction(q3, 0.3, evidence_ids=[3], model_runs={"m": [0.3]})
    # q1 已揭晓题 closes_at 距今 >7 天 → 命中「延迟归档」规则，resolve 不写 model_stats；
    # 直接 INSERT 造一条，保证 /api/system 的 model_stats 非空可断言（last_updated 字段）。
    st._conn.execute(
        "INSERT INTO model_stats (model_name, predictions, brier_ema, last_updated) "
        "VALUES ('m', 1, 0.09, CURRENT_TIMESTAMP)"
    )
    st.close()
    app = create_app(mode="internal")
    app.state.db_path = str(tmp_path / "e.db")
    return TestClient(app)


def test_list_questions(client):
    r = client.get("/api/questions")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    by_status = {i["status"]: i for i in items}
    assert set(by_status) == {"resolved", "open", "pending"}
    assert "probability" in by_status["open"]
    assert by_status["pending"]["title"] == "已过期未揭晓题"


def test_head_probe_supported(client):
    # 前端 init 用 HEAD 探测 /api/questions 判定视图（Task 8 浏览器手测发现：
    # FastAPI APIRoute 不自动注册 HEAD → 405 → 前端误锁对外视图，内部看板白屏）
    assert client.head("/api/questions").status_code == 200


def test_filters(client):
    assert len(client.get("/api/questions", params={"status": "resolved"}).json()["items"]) == 1
    assert len(client.get("/api/questions", params={"status": "open"}).json()["items"]) == 1
    assert len(client.get("/api/questions", params={"status": "pending"}).json()["items"]) == 1
    assert len(client.get("/api/questions", params={"class": "C"}).json()["items"]) == 1
    assert len(client.get("/api/questions", params={"q": "进行"}).json()["items"]) == 1


def test_detail_with_documents(client):
    r = client.get("/api/questions/2")
    assert r.status_code == 200
    assert r.json()["title"] == "进行中题"
    assert "documents" in r.json()
    # Task 8 手测回归：详情响应须含 status（前端弹层 badgeStatus 依赖）
    assert r.json()["status"] == "open"
    assert client.get("/api/questions/1").json()["status"] == "resolved"
    assert client.get("/api/questions/999").status_code == 404


def test_scoreboard(client):
    s = client.get("/api/scoreboard").json()
    assert s["resolved"] == 1
    assert "buckets" in s


def test_system(client):
    s = client.get("/api/system").json()
    assert set(s) == {"levers", "lessons", "evolution_log", "model_stats", "arm_stats"}
    # 字段级断言：model_stats 项含 last_updated（前端「模型统计」更新时间列，fix 见 storage.model_stats）
    assert s["model_stats"] and all("last_updated" in m for m in s["model_stats"])


def test_ops_endpoints(client, tmp_path, monkeypatch):
    import predictor.ops.probes as probes

    # log 端点（client fixture 库无事件 → 空列表形状正确）
    r = client.get("/api/ops/log")
    assert r.status_code == 200 and r.json() == {"items": []}
    # health 端点：形状 + 状态枚举（不断言具体状态——真实时钟下随时刻变化，避免时间炸弹）
    h = client.get("/api/ops/health").json()
    assert h["status"] in ("ok", "warn", "error")
    assert {"status", "checked_at", "checks"} <= set(h)
    keys = [c["key"] for c in h["checks"]]
    assert "predict_rounds" in keys and "resolve" in keys and "probe_quotes" in keys
    # 探测未检测 → info 不报警（同进程测试未写过 probes 缓存）
    assert next(c for c in h["checks"] if c["key"] == "probe_quotes")["status"] == "info"
    # refresh 202：TestClient 同步跑 BackgroundTasks → 替换探测体避免真实网络
    monkeypatch.setattr(probes, "_probe_quotes", lambda: {"ok": True, "detail": ""})
    monkeypatch.setattr(probes, "_probe_llm", lambda: {"ok": True, "detail": ""})
    monkeypatch.setattr(probes, "_probe_scheduler", lambda: {"ok": True, "detail": ""})
    assert client.post("/api/ops/health/refresh").status_code == 202
    probes._cache.clear()  # 还原，避免污染后续测试
    # log-files 白名单
    assert client.get("/api/ops/log-files", params={"name": "daily"}).status_code in (200, 404)
    assert client.get("/api/ops/log-files", params={"name": "../../etc/passwd"}).status_code == 400
