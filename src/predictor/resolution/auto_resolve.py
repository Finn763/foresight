"""揭晓轮入口：扫描到期未揭晓 → 按 class 分派 → 回填 → 记 evolution_log。"""

import json
from datetime import datetime, timedelta

from predictor.resolution.registry import get_resolver


def auto_resolve(storage, now: datetime | None = None) -> dict:
    """扫描 outcome IS NULL AND closes_at <= now 的题，A/B 类走 resolver，其余记 pending。

    返回 {"resolved": n, "degraded": n, "pending": n}：
    - resolved: 判定成功并回填（含 evolution_log.resolution_ok，3-tuple extra 合并进 detail）
    - degraded: resolver 无法判定 / resolution_spec JSON 损坏 → 记录 resolution_failed
    - pending:  无 spec 或 class C（人工）或 B 类 client 构造失败（get_resolver → None）
    """
    now = now or datetime.now()
    stats = {"resolved": 0, "degraded": 0, "pending": 0}
    for q in storage.list_open_questions(by=now):
        try:
            q_spec = storage.question_resolution(q.id)
        except Exception as e:
            # resolution_spec JSON 损坏 → 计 degraded，不 crash（整轮继续）
            stats["degraded"] += 1
            storage.log_evolution(
                "resolution_failed",
                json.dumps({"qid": q.id, "detail": f"spec broken: {e}"}, ensure_ascii=False),
            )
            continue
        if q_spec is None or q_spec.get("class") == "C":
            stats["pending"] += 1  # C 类人工揭晓
            continue
        if q_spec.get("source") == "polymarket":
            # Polymarket 题由 scripts/pm_resolve.py 混合揭晓（市场决议优先 + 独占窗口
            # + LLM 兜底）；16:30 轮显式跳过并记日志，防未来给 spec 补 class 后被
            # 本轮的 LLMResolver 在独占窗口内抢先揭晓、永久屏蔽市场决议
            storage.log_evolution(
                "resolution_skipped_polymarket",
                json.dumps(
                    {
                        "qid": q.id,
                        "detail": f"polymarket 题交 pm_resolve 揭晓（market_id="
                        f"{q_spec.get('market_id')}）",
                    },
                    ensure_ascii=False,
                ),
            )
            stats["pending"] += 1
            continue
        resolver = get_resolver(q_spec.get("class"), storage)
        if resolver is None:
            stats["pending"] += 1  # C 类人工 / B 类 client 构造失败
            continue
        try:
            outcome = resolver.resolve(q, q_spec, now)
        except Exception:
            outcome = None
        if outcome is not None:
            outcome_bool, source = outcome[0], outcome[1]
            extra = outcome[2] if len(outcome) > 2 else {}
            try:
                storage.resolve_question(q.id, outcome_bool, source)
            except Exception as e:
                # 回填异常（DB 层）→ 计 degraded 并记日志，不击垮整轮；
                # outcome 未落库，次日宽限窗口内会重试（同题不重复计分）
                stats["degraded"] += 1
                storage.log_evolution(
                    "resolution_failed",
                    json.dumps(
                        {"qid": q.id, "detail": f"resolve write failed: {e}"}, ensure_ascii=False
                    ),
                )
                continue
            stats["resolved"] += 1
            storage.log_evolution(
                "resolution_ok",
                json.dumps({"qid": q.id, "source": source, **extra}, ensure_ascii=False),
            )
        else:
            stats["degraded"] += 1
            # 区分三类失败（运维排障用）：T+1/数据窗口等待、数据窗口已过（停止重试，
            # 降级交 resolve_round 宽限超时分支）、真失败（数据不足/取价异常/双源分歧）。
            # 与 market_resolver 窗口纪律同构。
            detail = "resolver None（数据不足/取价失败/双源分歧）"
            try:
                tz = q_spec.get("close_timezone")
                lo = (
                    q.closes_at + timedelta(days=1) if tz and tz != "Asia/Shanghai" else q.closes_at
                )
                if now < lo:
                    detail = "waiting data window（未到可判定时点）"
                elif now >= lo + timedelta(days=1):
                    detail = (
                        "data window passed（数据窗口已过，停止重试；"
                        "超宽限后由 resolve_round 降级人工）"
                    )
            except Exception:
                pass
            storage.log_evolution(
                "resolution_failed", json.dumps({"qid": q.id, "detail": detail}, ensure_ascii=False)
            )
    return stats
