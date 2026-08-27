"""daily 编排冒烟：无预测的未到期题会被补预测；到期题进待揭晓清单。"""

from datetime import datetime, timedelta

from predictor.config import Settings
from predictor.data.storage import Storage


def ap_args(*a):
    from argparse import Namespace

    return Namespace(db=a[a.index("--db") + 1])


def test_daily_flags_due_and_unpredicted():
    st = Storage(":memory:")
    st.create_schema()
    q_due = st.add_question("已到期题", datetime.now() - timedelta(days=1))
    q_future = st.add_question("未到期未预测题", datetime.now() + timedelta(days=5))
    assert st.list_unresolved() and len(st.list_unresolved()) == 2
    assert st.count_predictions(q_due) == 0
    assert st.count_predictions(q_future) == 0


def test_ensure_question_families_returns_new_ids_for_immediate_prediction():
    """出题即预测契约：_ensure_question_families 返回新增题 id 列表（daily 09:00
    是双轨主出题入口——超短题 closes=次日，不立即预测则永远无预测，8-13 预演发现）。"""
    from datetime import datetime

    from scripts.daily import _ensure_question_families

    st = Storage(":memory:")
    st.create_schema()
    now = datetime(2026, 8, 17, 9, 0)  # 周一
    ids = _ensure_question_families(st, now)
    assert len(ids) >= 1, "空库周一应补充题族"
    for qid in ids:
        assert st.get_question(qid) is not None, "返回的 id 必须是真实入库的题"
        assert st.count_predictions(qid) == 0, "新题尚无预测（由调用方立即预测）"
    # 幂等：同一 now 再跑不重复出题
    ids2 = _ensure_question_families(st, now)
    assert ids2 == []


def test_manual_candidates_excludes_valid_auto_a_and_includes_b_c_invalid():
    """8-14 预演前对抗审计：到期未揭晓的人工清单只收"无法自动揭晓"的题——
    合法 A 类由 16:30 auto_resolve 处理，若列进人工清单会被在行情出现前填表判死。"""
    from predictor.ops.manual import manual_candidates

    st = Storage(":memory:")
    st.create_schema()
    now = datetime(2026, 8, 14, 9, 0)
    valid_a = {
        "class": "A",
        "instrument": "spx",
        "source_primary": "sina",
        "compare_symbol": "gb_$inx",
        "source_backup": "tencent",
        "condition": "gt_prev_close",
        "close_timezone": "America/New_York",
        "grace_days": 3,
        "degrade_to": "C",
    }
    qa = st.add_question(
        "自动题A", now - timedelta(days=1), resolution_class="A", resolution_spec=valid_a
    )
    qb = st.add_question(
        "B类题", now - timedelta(days=1), resolution_class="B", resolution_spec={"class": "B"}
    )
    qc = st.add_question(
        "C类题", now - timedelta(days=1), resolution_class="C", resolution_spec={"class": "C"}
    )
    qn = st.add_question("无spec题", now - timedelta(days=1))
    invalid_a = dict(valid_a)
    invalid_a.pop("condition")
    qi = st.add_question(
        "非法A题", now - timedelta(days=1), resolution_class="A", resolution_spec=invalid_a
    )
    ids = {q.id for q in manual_candidates(st, now)}
    assert qa not in ids, "合法 A 类自动题不得进人工清单"
    assert {qb, qc, qn, qi} <= ids


def test_predict_safely_swallows_pipeline_exception_and_logs(monkeypatch):
    """每题兜底契约：predict_with_websearch 抛任何异常（DB 写冲突等）→ 记日志 skip 单题，
    不击垮整轮（与 LLM 故障兜底同构，覆盖存储/编排面）。"""
    import scripts.daily as daily

    st = Storage(":memory:")
    st.create_schema()
    qid = st.add_question("题", datetime(2026, 8, 20, 9, 0))

    def boom(*a, **kw):
        raise RuntimeError("duckdb IOException: 另一个程序正在使用此文件")

    monkeypatch.setattr(daily, "predict_with_websearch", boom)
    assert daily._predict_safely(qid, st, None, [], datetime(2026, 8, 13, 9, 0)) is None
    evs = st._conn.execute("SELECT event_type FROM evolution_log").fetchall()
    assert [e[0] for e in evs] == ["prediction_skipped"]


def test_shared_lock_with_evolve():
    """双轨互斥：daily 与 evolve 必须用同一个锁对象（同一把 data/evolve.lock）。"""
    import scripts.daily as daily
    import scripts.evolve as evolve

    assert daily.acquire_lock is evolve.acquire_lock


def test_run_logs_round_and_prediction_events(tmp_path, monkeypatch):
    """round_started/round_completed/prediction_added 事件写入（daily 全链路）。"""
    import json

    import scripts.daily as daily
    from predictor.pipeline import Prediction

    monkeypatch.chdir(tmp_path)  # _run 写 Path("data") → tmp 隔离
    st = Storage(str(tmp_path / "e.db"))
    st.create_schema()
    qid = st.add_question("待预测题", datetime.now() + timedelta(days=7))
    monkeypatch.setattr(
        daily,
        "predict_with_websearch",
        lambda *a, **kw: Prediction(
            id=1,
            question_id=qid,
            probability=0.55,
            rationale="",
            evidence_ids=[1],
            model_runs={},
            report_md="",
        ),
    )
    daily._run(ap_args("--db", str(tmp_path / "e.db")), Settings())
    rows = st._conn.execute("SELECT event_type, detail FROM evolution_log ORDER BY id").fetchall()
    types = [r[0] for r in rows]
    assert types[0] == "round_started" and types[-1] == "round_completed"
    assert "prediction_added" in types
    started = json.loads(next(r[1] for r in rows if r[0] == "round_started"))
    assert started["round"] == "daily_predict"
    added = json.loads(next(r[1] for r in rows if r[0] == "prediction_added"))
    assert added["qid"] == qid and abs(added["prob"] - 0.55) < 1e-9
    assert (
        json.loads(next(r[1] for r in rows if r[0] == "round_completed"))["round"]
        == "daily_predict"
    )


def test_family_add_logs_question_added(tmp_path, monkeypatch):
    import json

    from scripts.daily import _ensure_question_families

    st = Storage(str(tmp_path / "e.db"))
    st.create_schema()
    ids = _ensure_question_families(st, datetime(2026, 8, 17, 9, 0))
    assert ids
    evs = st._conn.execute(
        "SELECT detail FROM evolution_log WHERE event_type='question_added'"
    ).fetchall()
    assert any(json.loads(e[0])["qid"] == ids[0] for e in evs)
    added = next(json.loads(e[0]) for e in evs if json.loads(e[0])["qid"] == ids[0])
    assert added["title"] and added["closes"]
