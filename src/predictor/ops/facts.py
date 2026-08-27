"""健康事实组装：DB 事实（轮次/积压/风暴）+ 本地文件事实（锁/战绩快照）。

轮次事实读 Task 1 写入的 round_started/round_completed 事件；锁状态判定复用
ops.lock.lock_state（与 acquire_lock 接管判定同源：GetExitCodeProcess STILL_ACTIVE
判活，8-13 实测 OpenProcess 成功≠存活；另含 6h 超龄→stale 判定）。
"""

import json
from datetime import datetime
from pathlib import Path

from predictor.ops import lock
from predictor.ops.manual import manual_candidates


def build_facts(st, now: datetime, *, lock_state: str | None = None) -> dict:
    """组装健康事实。

    lock_state 传值（"none"/"active"/"stale"）时跳过锁文件读取、直接采用
    （health_check 探测路径复用已判定结果）；None 时按 ops.lock.lock_state
    现场判定。位置调用 build_facts(st, now) 向后兼容。
    """
    rounds = {
        k: {"started": False, "completed": False}
        for k in ("daily_predict", "evolve_predict", "evolve_resolve")
    }
    today_start = datetime.combine(now.date(), datetime.min.time())
    for ev in st.list_events(types=["round_started", "round_completed"], limit=1000):
        if ev["ts"] < today_start:
            break  # id 倒序：更早的事件不用再看
        try:
            key = json.loads(ev["detail"] or "{}").get("round")
        except Exception:
            continue
        if key in rounds:
            rounds[key]["completed" if ev["event_type"] == "round_completed" else "started"] = True

    backlog = st.ops_backlog(now)
    manual = manual_candidates(st, now)
    backlog["manual_pending"] = len(manual)
    if manual:
        oldest = min(q.closes_at for q in manual)
        backlog["manual_oldest_days"] = max((now - oldest).days, 0)
    else:
        backlog["manual_oldest_days"] = None

    data_dir = Path(st.path).parent
    lock_path = data_dir / "evolve.lock"
    if lock_state is None:
        # 与 acquire_lock 接管判定同源（ops.lock.lock_state）——行为小改进：
        # 新增 6h 超龄→stale 判定（原实现只看 pid 死活，超龄锁的 pid 若被
        # 系统复用会误判 active）
        lock_state = lock.lock_state(lock_path)

    scoreboard_date = None
    sb_path = data_dir / "latest_scoreboard.json"
    if sb_path.exists():
        try:
            scoreboard_date = json.loads(sb_path.read_text(encoding="utf-8")).get("date")
        except Exception:
            scoreboard_date = None

    return {
        "rounds": rounds,
        "backlog": backlog,
        "storm": st.ops_storm(now),
        "lock": lock_state,
        "scoreboard_date": scoreboard_date,
    }
