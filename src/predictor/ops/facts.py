"""健康事实组装：DB 事实（轮次/积压/风暴）+ 本地文件事实（锁/战绩快照）。

轮次事实读 Task 1 写入的 round_started/round_completed 事件；锁存活判定复用
scripts.evolve._pid_alive（GetExitCodeProcess STILL_ACTIVE，8-13 实测 OpenProcess
成功≠存活）。"""

import json
from datetime import datetime
from pathlib import Path

from scripts.daily import _manual_candidates
from scripts.evolve import _pid_alive


def build_facts(st, now: datetime) -> dict:
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
    manual = _manual_candidates(st, now)
    backlog["manual_pending"] = len(manual)
    if manual:
        oldest = min(q.closes_at for q in manual)
        backlog["manual_oldest_days"] = max((now - oldest).days, 0)
    else:
        backlog["manual_oldest_days"] = None

    data_dir = Path(st.path).parent
    lock_path = data_dir / "evolve.lock"
    lock = "none"
    if lock_path.exists():
        try:
            pid, _ = lock_path.read_text().strip().split("|")
            lock = "active" if _pid_alive(int(pid)) else "stale"
        except Exception:
            lock = "stale"

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
        "lock": lock,
        "scoreboard_date": scoreboard_date,
    }
