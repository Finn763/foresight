"""健康引擎纯函数单测：双轨并集/时间感知/崩溃签名/积压/风暴/锁/探测/汇总。"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from predictor.ops.health import assess


def _facts(**kw):
    base = {
        "rounds": {
            k: {"started": False, "completed": False}
            for k in ("daily_predict", "evolve_predict", "evolve_resolve")
        },
        "backlog": {
            "past_grace_a": 0,
            "dead_ids": [],
            "manual_pending": 0,
            "manual_oldest_days": None,
        },
        "storm": 0,
        "lock": "none",
        "scoreboard_date": "2026-08-13",
        "probes": {"quotes": None, "llm": None, "scheduler": None, "refreshing": False},
    }
    base.update(kw)
    return base


def _check(checks, key):
    return next(c for c in checks if c["key"] == key)


def test_all_ok_when_rounds_completed():
    f = _facts()
    f["rounds"]["daily_predict"] = {"started": True, "completed": True}
    f["rounds"]["evolve_predict"] = {"started": True, "completed": True}
    f["rounds"]["evolve_resolve"] = {"started": True, "completed": True}
    out = assess(f, datetime(2026, 8, 13, 17, 0))
    assert out["status"] == "ok"
    assert _check(out["checks"], "predict_rounds")["status"] == "ok"


def test_pending_before_grace():
    # 早上 8 点：所有轮次未到宽限 → pending 不假红
    out = assess(_facts(), datetime(2026, 8, 13, 8, 0))
    assert out["status"] == "ok"
    assert _check(out["checks"], "predict_rounds")["status"] == "pending"


def test_union_ok_with_absent_track_warns():
    f = _facts()
    f["rounds"]["daily_predict"] = {"started": True, "completed": True}
    out = assess(f, datetime(2026, 8, 13, 17, 0))  # evolve_predict 宽限后缺席
    assert _check(out["checks"], "predict_rounds")["status"] == "ok"  # 并集
    assert _check(out["checks"], "predict_evolve")["status"] == "warn"  # 备援缺失
    assert out["status"] == "warn"  # 缺席 warn 计入汇总


def test_crash_signature_error():
    f = _facts()
    f["rounds"]["daily_predict"] = {"started": True, "completed": False}
    out = assess(f, datetime(2026, 8, 13, 17, 0))
    assert _check(out["checks"], "predict_daily")["status"] == "error"
    assert out["status"] == "error"


def test_both_tracks_absent_is_error_after_grace():
    out = assess(_facts(), datetime(2026, 8, 13, 17, 0))
    assert _check(out["checks"], "predict_rounds")["status"] == "error"


def test_resolve_missing_after_grace_is_error():
    f = _facts()
    f["rounds"]["daily_predict"] = {"started": True, "completed": True}
    f["rounds"]["evolve_predict"] = {"started": True, "completed": True}
    out = assess(f, datetime(2026, 8, 13, 19, 0))
    assert _check(out["checks"], "resolve")["status"] == "error"


def test_backlog_levels():
    f = _facts(
        backlog={"past_grace_a": 2, "dead_ids": [5], "manual_pending": 1, "manual_oldest_days": 9}
    )
    out = assess(f, datetime(2026, 8, 13, 17, 0))
    assert _check(out["checks"], "backlog_a")["status"] == "error"
    assert _check(out["checks"], "dead_questions")["status"] == "warn"
    assert _check(out["checks"], "manual_pending")["status"] == "warn"  # >7 天
    f2 = _facts(
        backlog={"past_grace_a": 0, "dead_ids": [], "manual_pending": 2, "manual_oldest_days": 3}
    )
    for k in ("daily_predict", "evolve_predict", "evolve_resolve"):
        f2["rounds"][k] = {"started": True, "completed": True}  # 隔离轮次因素
    out2 = assess(f2, datetime(2026, 8, 13, 17, 0))
    assert _check(out2["checks"], "manual_pending")["status"] == "info"  # info 不报警
    assert out2["status"] == "ok"  # info 不参与汇总


def test_storm_warning_threshold():
    out = assess(_facts(storm=10), datetime(2026, 8, 13, 17, 0))
    assert _check(out["checks"], "storm")["status"] == "warn"
    out2 = assess(_facts(storm=9), datetime(2026, 8, 13, 17, 0))
    assert _check(out2["checks"], "storm")["status"] == "ok"


def test_lock_and_scoreboard():
    f = _facts(lock="stale", scoreboard_date="2026-08-12")
    out = assess(f, datetime(2026, 8, 13, 17, 0))
    assert _check(out["checks"], "lock")["status"] == "warn"
    assert _check(out["checks"], "scoreboard")["status"] == "warn"
    out2 = assess(_facts(lock="active", scoreboard_date="2026-08-13"), datetime(2026, 8, 13, 17, 0))
    assert _check(out2["checks"], "lock")["status"] == "info"  # 轮次运行中不报警
    # 清晨战绩陈旧不假红（时间感知）
    out3 = assess(_facts(scoreboard_date="2026-08-12"), datetime(2026, 8, 13, 8, 0))
    assert _check(out3["checks"], "scoreboard")["status"] == "pending"


def test_probes_mapping():
    f = _facts()
    f["probes"] = {
        "quotes": {"ok": False, "detail": "sina 超时"},
        "llm": {"ok": True, "detail": ""},
        "scheduler": {"ok": False, "detail": "某任务已禁用", "level": "error"},
        "refreshing": False,
    }
    out = assess(f, datetime(2026, 8, 13, 17, 0))
    assert _check(out["checks"], "probe_quotes")["status"] == "error"
    assert _check(out["checks"], "probe_llm")["status"] == "ok"
    assert _check(out["checks"], "probe_scheduler")["status"] == "error"
    f2 = _facts()  # 全未检测
    for k in ("daily_predict", "evolve_predict", "evolve_resolve"):
        f2["rounds"][k] = {"started": True, "completed": True}  # 隔离轮次因素
    out2 = assess(f2, datetime(2026, 8, 13, 17, 0))
    assert _check(out2["checks"], "probe_quotes")["status"] == "info"
    assert out2["status"] == "ok"  # 未检测不报警
