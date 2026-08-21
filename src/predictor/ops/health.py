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


def _track_check(
    key: str, round_key: str, label: str, started: bool, completed: bool, now: datetime
) -> dict:
    if completed:
        return {"key": key, "status": "ok", "summary": f"{label}：今日轮次完成", "detail": {}}
    if started:
        return {
            "key": key,
            "status": "error",
            "summary": f"{label}：已开始但未完成（崩溃签名：无 round_completed）",
            "detail": {},
        }
    if now < _due(round_key, now):
        hh, mm = SCHEDULE[round_key]
        return {
            "key": key,
            "status": "pending",
            "summary": f"{label}：待运行（{hh:02d}:{mm:02d} + {GRACE_HOURS}h 宽限）",
            "detail": {},
        }
    return {"key": key, "status": "warn", "summary": f"{label}：该轨道今日未运行", "detail": {}}


def _union_predict(daily: str, evolve: str) -> dict:
    if "ok" in (daily, evolve):
        return {
            "key": "predict_rounds",
            "status": "ok",
            "summary": "预测轮：完成（双轨任一）",
            "detail": {},
        }
    if "error" in (daily, evolve):
        return {
            "key": "predict_rounds",
            "status": "error",
            "summary": "预测轮：崩溃（started 无 completed）",
            "detail": {},
        }
    if "pending" in (daily, evolve):
        return {
            "key": "predict_rounds",
            "status": "pending",
            "summary": "预测轮：待运行",
            "detail": {},
        }
    return {
        "key": "predict_rounds",
        "status": "error",
        "summary": "预测轮：今日全部缺席（双轨均未运行）",
        "detail": {},
    }


def assess(facts: dict, now: datetime) -> dict:
    rounds = facts["rounds"]
    checks = []
    daily = _track_check(
        "predict_daily",
        "daily_predict",
        "daily 轨道（09:00）",
        rounds["daily_predict"]["started"],
        rounds["daily_predict"]["completed"],
        now,
    )
    evolve = _track_check(
        "predict_evolve",
        "evolve_predict",
        "evolve 轨道（09:05）",
        rounds["evolve_predict"]["started"],
        rounds["evolve_predict"]["completed"],
        now,
    )
    checks += [daily, evolve, _union_predict(daily["status"], evolve["status"])]
    resolve = _track_check(
        "resolve",
        "evolve_resolve",
        "揭晓轮（16:30，唯一轨道）",
        rounds["evolve_resolve"]["started"],
        rounds["evolve_resolve"]["completed"],
        now,
    )
    if resolve["status"] == "warn":
        resolve = {
            "key": "resolve",
            "status": "error",
            "summary": "揭晓轮：今日未运行（唯一轨道，到期题将无法揭晓）",
            "detail": {},
        }
    checks.append(resolve)

    b = facts["backlog"]
    checks.append(
        {
            "key": "backlog_a",
            "status": "error" if b["past_grace_a"] else "ok",
            "summary": (
                f"A/B 类超宽限未降级 {b['past_grace_a']} 题（揭晓轮失能信号）"
                if b["past_grace_a"]
                else "A/B 类积压：正常"
            ),
            "detail": {"past_grace_a": b["past_grace_a"]},
        }
    )
    checks.append(
        {
            "key": "dead_questions",
            "status": "warn" if b["dead_ids"] else "ok",
            "summary": (
                f"无预测题 {len(b['dead_ids'])} 道（永不预测的死题）：{b['dead_ids']}"
                if b["dead_ids"]
                else "无预测题：0"
            ),
            "detail": {"dead_ids": b["dead_ids"]},
        }
    )
    if b["manual_pending"]:
        oldest = b["manual_oldest_days"]
        checks.append(
            {
                "key": "manual_pending",
                "status": "warn" if (oldest or 0) > 7 else "info",
                "summary": f"待人工揭晓 {b['manual_pending']} 题（最久 {oldest} 天）",
                "detail": b,
            }
        )
    else:
        checks.append(
            {"key": "manual_pending", "status": "ok", "summary": "待人工揭晓：0", "detail": {}}
        )

    checks.append(
        {
            "key": "storm",
            "status": "warn" if facts["storm"] >= 10 else "ok",
            "summary": (
                f"未来 48h 触发 7 天更新 {facts['storm']} 题（预计 ~{facts['storm'] * 4} 分钟，"
                "注意 API 成本）"
                if facts["storm"] >= 10
                else "7 天更新风暴：无"
            ),
            "detail": {"count": facts["storm"]},
        }
    )

    lock = facts["lock"]
    checks.append(
        {
            "key": "lock",
            "status": {"none": "ok", "active": "info", "stale": "warn"}[lock],
            "summary": {
                "none": "锁文件：无（空闲）",
                "active": "锁文件：轮次运行中",
                "stale": "锁文件：陈旧残留（将被 pid 检查接管）",
            }[lock],
            "detail": {"state": lock},
        }
    )

    today = now.date().isoformat()
    if facts["scoreboard_date"] == today:
        checks.append(
            {"key": "scoreboard", "status": "ok", "summary": "战绩快照：今日已刷新", "detail": {}}
        )
    elif now < _due("evolve_predict", now):
        checks.append(
            {
                "key": "scoreboard",
                "status": "pending",
                "summary": "战绩快照：待今日预测轮刷新",
                "detail": {},
            }
        )
    else:
        checks.append(
            {
                "key": "scoreboard",
                "status": "warn",
                "summary": f"战绩快照：停留在 {facts['scoreboard_date']}",
                "detail": {},
            }
        )

    checks += _probe_checks(facts.get("probes") or {})

    status = (
        "error"
        if any(c["status"] == "error" for c in checks)
        else "warn"
        if any(c["status"] == "warn" for c in checks)
        else "ok"
    )
    return {"status": status, "checked_at": now.isoformat(timespec="seconds"), "checks": checks}


def _probe_checks(probes: dict) -> list[dict]:
    checks = []
    for key, label in (("quotes", "行情源（新浪/腾讯）"), ("llm", "LLM API")):
        p = probes.get(key)
        if not p or p.get("ok") is None:
            checks.append(
                {
                    "key": f"probe_{key}",
                    "status": "info",
                    "summary": f"{label}：尚未检测（点击「立即检测」）",
                    "detail": {},
                }
            )
        elif p["ok"]:
            checks.append(
                {
                    "key": f"probe_{key}",
                    "status": "ok",
                    "summary": f"{label}：正常",
                    "detail": {"detail": p.get("detail", "")},
                }
            )
        else:
            checks.append(
                {
                    "key": f"probe_{key}",
                    "status": "error",
                    "summary": f"{label}：异常",
                    "detail": {"detail": p.get("detail", "")},
                }
            )
    s = probes.get("scheduler")
    if not s or s.get("ok") is None:
        checks.append(
            {
                "key": "probe_scheduler",
                "status": "info",
                "summary": "任务计划器：尚未检测",
                "detail": {},
            }
        )
    elif s["ok"]:
        checks.append(
            {
                "key": "probe_scheduler",
                "status": "ok",
                "summary": "任务计划器：三任务正常",
                "detail": {"detail": s.get("detail", "")},
            }
        )
    else:
        checks.append(
            {
                "key": "probe_scheduler",
                "status": s.get("level", "warn"),
                "summary": "任务计划器：异常",
                "detail": {"detail": s.get("detail", "")},
            }
        )
    if probes.get("refreshing"):
        checks.append(
            {"key": "probe_refreshing", "status": "info", "summary": "探测刷新中…", "detail": {}}
        )
    return checks
