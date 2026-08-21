"""storage 事实方法单测（list_events 分页/过滤、积压、风暴计数）。"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from predictor.data.storage import Storage


def _mk():
    st = Storage(":memory:")
    st.create_schema()
    return st


def test_list_events_order_and_pagination():
    st = _mk()
    st.log_evolution("a", '{"n": 1}')
    st.log_evolution("b", '{"n": 2}')
    st.log_evolution("c", '{"n": 3}')
    evs = st.list_events()
    assert [e["event_type"] for e in evs] == ["c", "b", "a"]
    evs = st.list_events(limit=2)
    assert len(evs) == 2 and evs[0]["event_type"] == "c"
    before = evs[0]["id"]  # 最新一条的 id → 游标取更早
    evs2 = st.list_events(before_id=before)
    assert [e["event_type"] for e in evs2] == ["b", "a"]
    evs3 = st.list_events(types=["a", "c"])
    assert [e["event_type"] for e in evs3] == ["c", "a"]


def test_ops_backlog_flags_past_grace_a_and_dead():
    st = _mk()
    now = datetime(2026, 8, 20, 16, 30)
    spec_a = {
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
        "超宽限A", now - timedelta(days=10), resolution_class="A", resolution_spec=spec_a
    )
    st.add_prediction(qa, 0.5, evidence_ids=[1], model_runs={})
    qb = st.add_question("无预测死题", now + timedelta(days=5))
    qc = st.add_question(
        "正常B", now - timedelta(days=1), resolution_class="B", resolution_spec={"class": "B"}
    )
    st.add_prediction(qc, 0.5, evidence_ids=[2], model_runs={})  # 有预测 → 非死题
    b = st.ops_backlog(now)
    assert b["past_grace_a"] == 1
    assert b["dead_ids"] == [qb]
    # grace_days 损坏回退 3 天（与 evolve 超时分支同语义）
    spec_bad = dict(spec_a)
    spec_bad["grace_days"] = "abc"
    st.add_question(
        "坏宽限A", now - timedelta(days=10), resolution_class="A", resolution_spec=spec_bad
    )
    assert st.ops_backlog(now)["past_grace_a"] == 2


def test_ops_storm_counts_upcoming_7day_updates():
    st = _mk()
    # now 取动态（全局约束）：q2 的 created_at 是库 CURRENT_TIMESTAMP（真机时间），
    # brief 硬编码 now=2026-08-20 时「今天预测的不算」断言会随机器日期漂移（6~7 天间翻转）
    now = datetime.now()
    q = st.add_question("将触发更新题", now + timedelta(days=3))
    st.add_prediction(q, 0.5, evidence_ids=[1], model_runs={})
    # 手工把预测时间拨到 6 天前
    st._conn.execute(
        "UPDATE predictions SET created_at = ? WHERE question_id = ?", [now - timedelta(days=6), q]
    )
    assert st.ops_storm(now) == 1
    q2 = st.add_question("近期已预测题", now + timedelta(days=3))
    st.add_prediction(q2, 0.5, evidence_ids=[1], model_runs={})
    assert st.ops_storm(now) == 1  # 今天预测的不算


def test_build_facts_rounds_and_files(tmp_path, monkeypatch):
    import json
    from datetime import datetime

    from predictor.ops.facts import build_facts

    st = Storage(str(tmp_path / "e.db"))
    st.create_schema()
    now = datetime.now()
    st.log_evolution("round_started", json.dumps({"round": "daily_predict"}))
    st.log_evolution("round_completed", json.dumps({"round": "daily_predict"}))
    # 锁文件（active 态用存活 pid；stale 用不存在 pid）
    (tmp_path / "evolve.lock").write_text("99999999|0")
    (tmp_path / "latest_scoreboard.json").write_text(
        json.dumps({"date": now.date().isoformat(), "buckets": []}), encoding="utf-8"
    )
    f = build_facts(st, now)
    assert f["rounds"]["daily_predict"] == {"started": True, "completed": True}
    assert f["rounds"]["evolve_predict"] == {"started": False, "completed": False}
    assert f["lock"] == "stale"
    assert f["scoreboard_date"] == now.date().isoformat()
    assert set(f) == {"rounds", "backlog", "storm", "lock", "scoreboard_date"}
