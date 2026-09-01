"""健康引擎（纯函数）：facts → checks → 红黄绿总状态。

facts 契约见 tests/ops/test_health.py 头部注释与 Task 5 build_facts；
本模块只做判定，不碰 DB/文件/网络 → 可独立单测。
"""

from datetime import datetime, timedelta
from datetime import time as dtime

SCHEDULE = {"daily_predict": (9, 0), "evolve_predict": (9, 5), "evolve_resolve": (16, 30)}
GRACE_HOURS = 2


def _due(round_key: str, now: datetime) -> datetime:
    return datetime.combine(now.date(), dtime(*SCHEDULE[round_key])) + timedelta(hours=GRACE_HOURS)


def _c(key, status, summary, detail=None):
    return {"key": key, "status": status, "summary": summary, "detail": detail or {}}


def _track_check(key: str, round_key: str, label: str, started: bool, completed: bool, now: datetime) -> dict:
    if completed:
        return _c(key, "ok", f"{label}：今日轮次完成")
    if started:
        return _c(key, "error", f"{label}：已开始但未完成（崩溃签名：无 round_completed）")
    if now < _due(round_key, now):
        hh, mm = SCHEDULE[round_key]
        return _c(key, "pending", f"{label}：待运行（{hh:02d}:{mm:02d} + {GRACE_HOURS}h 宽限）")
    return _c(key, "warn", f"{label}：该轨道今日未运行")


def assess(facts: dict, now: datetime) -> dict:
    rounds = facts["rounds"]
    checks = []
    daily = _track_check("predict_daily", "daily_predict", "daily 轨道（09:00）", rounds["daily_predict"]["started"], rounds["daily_predict"]["completed"], now)
    evolve = _track_check("predict_evolve", "evolve_predict", "evolve 轨道（09:05）", rounds["evolve_predict"]["started"], rounds["evolve_predict"]["completed"], now)
    # inline _union_predict (6L): 双轨任一 ok→ok, 任一 error→error, 任一 pending→pending, 否则 error(双缺席)
    ds, es = daily["status"], evolve["status"]
    if "ok" in (ds, es):
        checks += [daily, evolve, _c("predict_rounds", "ok", "预测轮：完成（双轨任一）")]
    elif "error" in (ds, es):
        checks += [daily, evolve, _c("predict_rounds", "error", "预测轮：崩溃（started 无 completed）")]
    elif "pending" in (ds, es):
        checks += [daily, evolve, _c("predict_rounds", "pending", "预测轮：待运行")]
    else:
        checks += [daily, evolve, _c("predict_rounds", "error", "预测轮：今日全部缺席（双轨均未运行）")]
    resolve = _track_check("resolve", "evolve_resolve", "揭晓轮（16:30，唯一轨道）", rounds["evolve_resolve"]["started"], rounds["evolve_resolve"]["completed"], now)
    if resolve["status"] == "warn":
        resolve = _c("resolve", "error", "揭晓轮：今日未运行（唯一轨道，到期题将无法揭晓）")
    checks.append(resolve)

    b = facts["backlog"]
    checks.append(_c("backlog_a", "error" if b["past_grace_a"] else "ok", f"A/B 类超宽限未降级 {b['past_grace_a']} 题（揭晓轮失能信号）" if b["past_grace_a"] else "A/B 类积压：正常", {"past_grace_a": b["past_grace_a"]}))
    checks.append(_c("dead_questions", "warn" if b["dead_ids"] else "ok", f"无预测题 {len(b['dead_ids'])} 道（永不预测的死题）：{b['dead_ids']}" if b["dead_ids"] else "无预测题：0", {"dead_ids": b["dead_ids"]}))
    if b["manual_pending"]:
        oldest = b["manual_oldest_days"]
        checks.append(_c("manual_pending", "warn" if (oldest or 0) > 7 else "info", f"待人工揭晓 {b['manual_pending']} 题（最久 {oldest} 天）", b))
    else:
        checks.append(_c("manual_pending", "ok", "待人工揭晓：0"))

    checks.append(_c("storm", "warn" if facts["storm"] >= 10 else "ok", f"未来 48h 触发 7 天更新 {facts['storm']} 题（预计 ~{facts['storm'] * 4} 分钟，注意 API 成本）" if facts["storm"] >= 10 else "7 天更新风暴：无", {"count": facts["storm"]}))

    lock = facts["lock"]
    checks.append(_c("lock", {"none": "ok", "active": "info", "stale": "warn"}[lock], {"none": "锁文件：无（空闲）", "active": "锁文件：轮次运行中", "stale": "锁文件：陈旧残留（将被 pid 检查接管）"}[lock], {"state": lock}))

    # scoreboard table-driven
    today = now.date().isoformat()
    for cond, status, summary in (
        (facts["scoreboard_date"] == today, "ok", "战绩快照：今日已刷新"),
        (now < _due("evolve_predict", now), "pending", "战绩快照：待今日预测轮刷新"),
    ):
        if cond:
            checks.append(_c("scoreboard", status, summary))
            break
    else:
        checks.append(_c("scoreboard", "warn", f"战绩快照：停留在 {facts['scoreboard_date']}"))

    checks += _probe_checks(facts.get("probes") or {})

    status = "error" if any(c["status"] == "error" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    return {"status": status, "checked_at": now.isoformat(timespec="seconds"), "checks": checks}


def _probe_checks(probes: dict) -> list[dict]:
    def _one(key, label, p, ok_msg="正常"):
        if not p or p.get("ok") is None:
            return _c(f"probe_{key}", "info", f"{label}：尚未检测（点击「立即检测」）" if key != "scheduler" else "任务计划器：尚未检测")
        if p["ok"]:
            detail = p.get("detail", "")
            summary = f"{label}：{ok_msg}" if key != "scheduler" else "任务计划器：三任务正常"
            return _c(f"probe_{key}", "ok", summary, {"detail": detail} if detail else {"detail": detail})
        return _c(f"probe_{key}", p.get("level", "error") if key == "scheduler" else "error", f"{label}：异常" if key != "scheduler" else "任务计划器：异常", {"detail": p.get("detail", "")})

    checks = [_one(k, lbl, probes.get(k)) for k, lbl in (("quotes", "行情源（新浪/腾讯）"), ("llm", "LLM API"))]
    checks.append(_one("scheduler", "任务计划器", probes.get("scheduler"), ok_msg="三任务正常"))
    if probes.get("refreshing"):
        checks.append(_c("probe_refreshing", "info", "探测刷新中…"))
    return checks
