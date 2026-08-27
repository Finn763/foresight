"""揭晓轮入口：扫描到期未揭晓 → 按 class 分派 → 回填 → 记 evolution_log。"""

import json
from datetime import datetime, timedelta

from predictor.calibration.calibrate import refresh_from_storage
from predictor.resolution.registry import get_resolver


def auto_resolve(storage, now: datetime | None = None) -> dict:
    """扫描 outcome IS NULL AND closes_at <= now 的题，A/B 类走 resolver，其余记 pending。

    返回 {"resolved": n, "degraded": n, "pending": n}：
    - resolved: 判定成功并回填（含 evolution_log.resolution_ok，3-tuple extra 合并进 detail）
    - degraded: resolver 无法判定 / resolver 内部抛异常 / resolution_spec JSON 损坏 →
      记录 resolution_failed（异常路径含异常类型与消息）
    - pending:  无 spec 或 class C（人工）或 B 类 client 构造失败（get_resolver → None）

    本轮 resolved > 0 时尾部刷新校准器（data/calibrator.json）：16:30 自动揭晓是每日
    最大宗揭晓路径，人工路径（scripts/resolve.py）已有此步；刷新失败只降级记日志。
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
        resolve_error: str | None = None
        try:
            outcome = resolver.resolve(q, q_spec, now)
        except Exception as e:
            # resolver 内部异常 ≠ 业务性 None：记录异常类型/消息（CC §2.7③），
            # 与其它路径 resolution_failed 日志口径一致；整轮继续不 crash
            outcome = None
            resolve_error = f"{type(e).__name__}: {e}"
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
            if resolve_error is not None:
                # resolver 抛异常（非业务性 None）：直接记异常，不做数据窗口分类
                detail = resolve_error
            else:
                # 区分三类失败（运维排障用）：T+1/数据窗口等待、数据窗口已过（停止重试，
                # 降级交 resolve_round 宽限超时分支）、真失败（数据不足/取价异常/双源分歧）。
                # 与 market_resolver 窗口纪律同构。
                detail = "resolver None（数据不足/取价失败/双源分歧）"
                try:
                    tz = q_spec.get("close_timezone")
                    lo = (
                        q.closes_at + timedelta(days=1)
                        if tz and tz != "Asia/Shanghai"
                        else q.closes_at
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
    if stats["resolved"] > 0:
        # 校准闭环（CC §2.4）：16:30 自动揭晓是每日最大宗揭晓路径，回填后必须
        # 重新 fit 并落盘 data/calibrator.json（人工路径 scripts/resolve.py 已有此步，
        # 自动路径此前缺失——跨过 30 样本后生产 websearch 概率将用陈旧校准器）。
        # 刷新失败只降级记日志，不阻塞本轮结果（样本不足时静默返回 False，不写盘）。
        try:
            refresh_from_storage(storage)
        except Exception as e:
            storage.log_evolution(
                "calibrator_refresh_failed",
                json.dumps(
                    {"detail": f"calibrator refresh failed: {type(e).__name__}: {e}"},
                    ensure_ascii=False,
                ),
            )
    return stats
