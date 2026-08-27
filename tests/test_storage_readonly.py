"""Storage 只读改造：read_only 连接 + web 展示用只读查询。"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predictor.data.storage import Storage


def _seed(st: Storage) -> None:
    """造数（Storage 真实签名核对于 2026-08-12：add_question 无概率参数、
    resolve_question 无 resolved_at 参数、add_document 需 content、
    lessons/lever_registry 无写方法 → 直接 SQL）。"""
    st.create_schema()
    q1 = st.add_question("已揭晓题", datetime(2026, 8, 1), resolution_class="A")
    st.add_prediction(q1, 0.7, evidence_ids=[1], model_runs={"m": [0.7]})
    # 注：q1 的 closes_at(08-01) 距今(08-12) > 7 天，会触发 resolve_question 的
    # 延迟归档规则（>7 天不写 brier_score）→ force_score=True 豁免（该参数即为此设计）。
    st.resolve_question(q1, outcome=True, resolution_source="sina", force_score=True)
    # q2 需在测试 now(2026-08-12 12:00) 前 closes 才是 pending（brief 原 08-13 会被判 open）
    q2 = st.add_question("未揭晓待决题", datetime(2026, 8, 11), resolution_class="B")
    st.add_prediction(q2, 0.4, evidence_ids=[2], model_runs={"m": [0.4]})
    q3 = st.add_question("未揭晓进行中", datetime(2026, 9, 1), resolution_class="C")
    st.add_prediction(q3, 0.5, evidence_ids=[3], model_runs={"m": [0.5]})
    st.add_document(q2, "gdelt", "https://x", "证据一", "内容", published_at=datetime(2026, 8, 10))
    st._conn.execute(
        "INSERT INTO lessons (question_type, question_id, precondition, "
        "prediction_summary, outcome, attribution, confidence, scope, status, "
        "testable_criteria, hits, misses, evidence_refs) "
        "VALUES ('macro', ?, 'p', 's', TRUE, 'info_gap', 0.6, 'spx', 'validating', "
        "'c', 0, 0, NULL)",
        [q2],
    )
    st._conn.execute(
        "INSERT INTO lever_registry (lever_key, lever_type, status, effect_size, "
        "n_validated, threshold_n, threshold_delta, created_at, activated_at) "
        "VALUES ('arm_l1', 'prior', 'validating', 0.01, 2, 10, 0.05, "
        "CURRENT_TIMESTAMP, NULL)"
    )
    st.log_evolution("predict_round", '{"n": 1}')
    return q1, q2, q3


@pytest.fixture
def db(tmp_path):
    st = Storage(str(tmp_path / "e.db"))
    _seed(st)
    st.close()
    return str(tmp_path / "e.db")


def _open_readonly(path):
    return Storage(path, read_only=True)


def test_readonly_connection_is_actually_readonly(db):
    st = _open_readonly(db)
    with pytest.raises(Exception):
        st._conn.execute("INSERT INTO questions (title, closes_at) VALUES ('x', now())")
    st.close()


def test_readonly_can_query(db):
    st = _open_readonly(db)
    rows = st.list_questions_all()
    assert len(rows) == 3
    st.close()


def test_list_questions_status_and_filters(db):
    st = _open_readonly(db)
    now = datetime(2026, 8, 12, 12, 0)
    rows = st.list_questions_all(now=now)
    by_status = {r["status"]: r for r in rows}
    assert by_status["resolved"]["title"] == "已揭晓题"
    assert by_status["pending"]["title"] == "未揭晓待决题"
    assert by_status["open"]["title"] == "未揭晓进行中"
    assert len(st.list_questions_all(status="resolved", now=now)) == 1
    assert len(st.list_questions_all(status="pending", now=now)) == 1
    assert len(st.list_questions_all(status="open", now=now)) == 1
    assert len(st.list_questions_all(resolution_class="B", now=now)) == 1
    assert len(st.list_questions_all(q="待决", now=now)) == 1
    st.close()


def test_get_question_detail_and_documents(db):
    st = _open_readonly(db)
    detail = st.get_question_detail(2)
    assert detail["title"] == "未揭晓待决题"
    assert detail["resolution_class"] == "B"
    assert detail["arm"] == "baseline"
    docs = st.list_question_documents(2)
    assert docs[0]["title"] == "证据一"
    assert st.get_question_detail(999) is None
    st.close()


def test_system_panels(db):
    st = _open_readonly(db)
    assert len(st.list_levers()) == 1
    assert st.list_levers()[0]["lever_key"] == "arm_l1"
    assert len(st.list_lessons()) == 1
    assert len(st.list_evolution_log()) == 1
    arms = {a["arm"]: a for a in st.arm_stats()}
    assert arms["baseline"]["resolved"] == 1
    assert arms["baseline"]["brier_mean"] is not None
    st.close()


def test_latest_prediction_only_in_show_queries(db):
    """预测可更新语义（旧行作废，最后一条计分）：展示查询只取每题最新一条预测。
    回归：原 LEFT JOIN 每题多行（Task 1 遗留顾虑 1，coordinator 裁定为正确性缺口）。"""
    st = Storage(db)  # 写连接补数
    # 动态日期：closes 距今 >7 天会命中延迟归档（不写 brier_score）——
    # 硬编码日期是时间炸弹（2026-08-27 同文件 :173 已引爆，项目已有教训）
    q = st.add_question("可更新题", datetime.now() - timedelta(days=1))
    st.add_prediction(q, 0.4, evidence_ids=[1], model_runs={"m": [0.4]})
    second = st.add_prediction(q, 0.9, evidence_ids=[2], model_runs={"m": [0.9]})
    st.resolve_question(q, outcome=True, resolution_source="sina")
    st.close()
    st = _open_readonly(db)
    rows = st.list_questions_all(q="可更新题")
    assert len(rows) == 1
    assert rows[0]["probability"] == 0.9
    detail = st.get_question_detail(q)
    assert detail["prediction_id"] == second
    assert detail["probability"] == 0.9
    resolved = [r for r in st.list_resolved_public() if r["id"] == q]
    assert len(resolved) == 1
    assert resolved[0]["probability"] == 0.9
    st.close()


def test_count_metrics_are_question_deduplicated(tmp_path):
    """战绩计数按题口径：1 题 2 条预测（可更新语义）只计 1 道已揭晓题。
    回归：scoreboard_summary 的 COUNT(q.id) / arm_stats 的 COUNT(q.outcome)
    在多行预测下 resolved 虚高。"""
    path = str(tmp_path / "e2.db")
    st = Storage(path)
    st.create_schema()
    q = st.add_question("计数回归题", datetime.now() - timedelta(days=1))
    st.add_prediction(q, 0.4, evidence_ids=[1], model_runs={"m": [0.4]})
    st.add_prediction(q, 0.9, evidence_ids=[2], model_runs={"m": [0.9]})
    st.resolve_question(q, outcome=True, resolution_source="sina")
    st.close()
    st = _open_readonly(path)
    assert st.scoreboard_summary()["resolved"] == 1
    assert st.public_summary()["resolved"] == 1
    arms = {a["arm"]: a for a in st.arm_stats()}
    assert arms["baseline"]["resolved"] == 1
    assert arms["baseline"]["n"] == 2  # n 仍为预测数口径（brief 定义），仅 resolved 按题去重
    st.close()


def test_websearch_arm_appears_on_public_scoreboard(tmp_path):
    """生产臂出榜：websearch 臂（daily/evolve 生产入口）揭晓后必须出现在对外榜，
    旧口径（只认 baseline）会让生产战绩永远隐形。"""
    path = str(tmp_path / "e3.db")
    st = Storage(path)
    st.create_schema()
    # 动态日期（now-1d）：延迟归档规则「closes 距今 >7 天不写 brier_score」
    # 是时间炸弹——硬编码 2026-08-20 在 8-27 引爆使 :173 断言红（评审报告 §5.1）
    q = st.add_question("生产臂出榜题", datetime.now() - timedelta(days=1))
    st.add_prediction(
        q, 0.8, evidence_ids=[1], model_runs={"deepseek-flash-websearch": [0.8]}, arm="websearch"
    )
    st.resolve_question(q, outcome=True, resolution_source="pm")
    st.close()
    st = _open_readonly(path)
    assert st.scoreboard_summary()["resolved"] == 1
    rows = st.list_resolved_public()
    assert len(rows) == 1
    assert rows[0]["probability"] == 0.8
    assert rows[0]["brier_score"] is not None
    st.close()


def test_scoreboard_and_public_whitelist(db):
    st = _open_readonly(db)
    s = st.scoreboard_summary()
    assert s["resolved"] == 1
    p = st.public_summary()
    assert p["resolved"] == 1
    resolved = st.list_resolved_public()
    assert len(resolved) == 1
    assert {
        "id",
        "title",
        "closes_at",
        "probability",
        "outcome",
        "brier_score",
        "resolved_at",
    } == set(resolved[0].keys())
    st.close()
