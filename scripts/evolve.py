"""python scripts/evolve.py [predict|resolve|all] [--db path]
自我进化闭环编排（单入口）：
  预测轮（09:05）：① 未到期题补预测/7天更新（臂A，臂B按候选杠杆触发）② 题族补充 ③ 战绩快照
  揭晓轮（16:30）：① 宽限过期 A 类兜底降级人工（T4 遗留，编排层职责，每题只记一次日志）
                  ② auto_resolve（A/B 类）③ 到期未揭晓 C 类 → resolutions.template.csv
                  ④ 周一额外生成周报骨架（data/weekly_review/YYYY-WW.md）
文件锁：data/evolve.lock，6 小时 stale 接管。"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    # 任务重定向管道下 stdout 默认块缓冲：原生崩溃（0xc0000005）时整段缓冲丢失、
    # 日志零痕迹（8-12 实锤）——行缓冲保证心跳行能落盘
    sys.stdout.reconfigure(line_buffering=True)
from predictor.config import Settings
from predictor.data.storage import Storage
from predictor.memory.levers import get_active_candidate
from predictor.ops.lock import acquire_lock
from predictor.pipeline import run_prediction
from predictor.resolution.auto_resolve import auto_resolve
from predictor.resolution.spec import validate_resolution_spec
from predictor.selection.families import difficulty_tier, generate_families


def _data_dir(st) -> Path:
    """输出落盘目录：跟随 DB 路径（tmp 测试库 → 测试目录；生产 → data/）。
    旧 Storage 对象无 path 属性时回退 data/（brief 行为）。"""
    p = getattr(st, "path", None)
    return Path(p).parent if p else Path("data")


# 难度三档 base_rates 的中文子串 key（difficulty_tier 约定，见 families.py docstring）
_BASE_RATE_KEYS = ["标普", "上证", "布伦特", "黄金", "人民币"]


def _build_base_rates(now: datetime | None = None) -> dict:
    """尽力组装 generate_families 的 base_rates。

    key 约定为中文子串（difficulty_tier 匹配用，与族 key 同构）：标普/上证/布伦特/
    黄金/人民币；值 = baselines.compute_baseline 产出的真实历史频率。无匹配算法或
    数据不足的 key 不出现（difficulty_tier 判 blind）；历史数据层任何失败 → 空 dict
    （调用方容错，不阻塞题族补充）。P0 仅"标普"有基线算法（sp500_high），其余族
    盲档待 P1 补基线——接线结构即本函数。"""
    try:
        from predictor.stats.baselines import compute_baseline
        from predictor.stats.historical import fetch_series_map

        sm = fetch_series_map(now=now)
    except Exception:
        return {}
    rates = {}
    for key in _BASE_RATE_KEYS:
        try:
            b = compute_baseline(key, sm)
        except Exception:
            b = None
        if b is not None and b.get("base_rate") is not None:
            rates[key] = b["base_rate"]
    return rates


def predict_round(st, *, now: datetime, client, sources, base_rates: dict | None = None) -> dict:
    """预测轮：① 未到期未预测/≥7 天未更新 → 臂 A 预测（臂 B 仅在候选杠杆存在时
    触发，P0 恒无）② 题族补充（base_rates 注入难度三档，尽力而为）③ 战绩快照。"""
    from scripts.daily import _log_event, _predict_safely

    stats = {"predicted": 0, "skipped": 0, "families_added": 0}
    candidate = get_active_candidate(st)
    for q in st.list_unresolved():
        if q.closes_at <= now:
            continue
        last = st.last_prediction_at(q.id)
        if last is not None and (now - last).days < 7:
            continue
        pa = _predict_safely(q.id, st, client, sources, now)
        if pa is not None:
            stats["predicted"] += 1
            if candidate is not None:
                # 臂 B（实验臂）：先验 + 配对写路径，P0 恒不触发（候选杠杆无）
                try:
                    run_prediction(
                        q.id,
                        st,
                        client,
                        sources,
                        now=now,
                        prior=candidate.get("prior_offset"),
                        arm="experiment",
                        arm_group=pa.id,
                    )
                except Exception as e:
                    try:
                        st.log_evolution(
                            "prediction_skipped",
                            json.dumps(
                                {"qid": q.id, "detail": f"arm B exception: {e}"}, ensure_ascii=False
                            ),
                        )
                    except Exception:
                        pass
        else:
            stats["skipped"] += 1
    if base_rates is None:
        base_rates = _build_base_rates(now)
    specs = generate_families(st, now, base_rates=base_rates)
    for spec in specs:
        # T3 契约：validate_resolution_spec 返回错误列表，空列表=合法 → 只入合法题
        # （brief 代码块写 `if validate_resolution_spec(...)` 会把合法 spec 全跳过，
        #  与 T5 在 daily.py 修过的同一 bug；此处按契约翻转）
        if not validate_resolution_spec(spec.resolution_spec):
            qid = st.add_question(
                spec.title,
                spec.closes_at,
                resolution_class=spec.resolution_class,
                resolution_spec=spec.resolution_spec,
            )
            stats["families_added"] += 1
            # spec §4.1：题族循环也写 question_added 事件（daily 缺席的备援日
            # 时间线不缺出题事件；格式与 daily._ensure_question_families 一致）
            _log_event(
                st,
                "question_added",
                json.dumps(
                    {"qid": qid, "title": spec.title, "closes": spec.closes_at.isoformat()},
                    ensure_ascii=False,
                ),
            )
            # 出题即预测（8-13 预演发现）：predict_round 的题循环快照不含当轮新题；
            # 超短题 closes=次日，错过本轮则下轮 closes 已过、永远无预测。
            # client 为 None（测试/禁用场景）→ 只出题不预测；预测失败（LLM 故障/
            # 存储异常）由 _predict_safely 兜底（skip 单题记日志），不中断本轮。
            if client is not None:
                pa = _predict_safely(qid, st, client, sources, now, label="（题族新题）")
                if pa is not None:
                    stats["predicted"] += 1
    # 难度三档分布观测（T5 裁定：三档纪律由调用方注入 base_rates 负责）
    stats["difficulty_tiers"] = {}
    for spec in specs:
        t = difficulty_tier(spec.title, base_rates)
        stats["difficulty_tiers"][t] = stats["difficulty_tiers"].get(t, 0) + 1
    write_scoreboard(st, now)
    return stats


def resolve_round(st, *, now: datetime, data_dir: Path | None = None) -> dict:
    """揭晓轮：① 宽限过期（closes + grace_days）仍无法揭晓的 A/B 类 → 标 C 降级人工，
    每题只记一次 resolution_timeout + resolution_archived（spec.class 置 C 幂等去重；
    之后轮次 auto_resolve 见 class=C 直接 pending，不再反复重试刷日志——T4 遗留的
    编排层兜底）
    ② auto_resolve ③ 到期未揭晓 C 类 → resolutions.template.csv ④ 周一周报骨架。

    归档策略（T7 审查裁定）：延迟 >7 天未揭晓的题——已由 closes+grace_days 宽限降级
    人工覆盖（超时降级分支记 resolution_archived）；未揭晓题天然不进技能桶
    （brier_by_horizon_bucket 只计 brier_score IS NOT NULL）；P0 不实现 inactive 列，
    P1 如需从活跃池剔除再加。"""
    dd = _data_dir(st) if data_dir is None else data_dir
    timeouts = 0
    for q in st.list_open_questions(by=now):
        try:
            spec = st.question_resolution(q.id)
        except Exception as e:
            # 对称防护（auto_resolve 同款 try/except → degraded）：resolution_spec
            # JSON 损坏 → 记 resolution_failed 后跳过本路径，不中断整轮
            st.log_evolution(
                "resolution_failed",
                json.dumps(
                    {"qid": q.id, "detail": f"spec broken in timeout check: {e}"},
                    ensure_ascii=False,
                ),
            )
            continue
        if spec is None or spec.get("class") not in ("A", "B"):
            continue  # 无 spec / 已降级 C / 其他（P1）不在此路径
        try:
            grace = int(spec.get("grace_days", 3))
        except (TypeError, ValueError):
            # grace_days 非数值（如 "abc"）→ 回退默认 3 天继续走超时判定，不 crash
            # （对称防护同 auto_resolve；该题仍会进宽限超时降级路径）
            grace = 3
        if now > q.closes_at + timedelta(days=grace):
            # 宽限已过仍无法揭晓 → 降级人工；置于 auto_resolve 之前执行，
            # 本轮即避免 resolution_failed 刷屏，且 auto_resolve 对已超窗题
            # 本就恒 None（market_resolver 超窗直接返回 None），无揭晓损失
            degraded = dict(spec)
            # 按 spec.degrade_to 降级（约定恒为 C 人工；缺省回退 C）——
            # class 是保留字，不能用 dict(spec, class=...) 语法
            degraded["class"] = spec.get("degrade_to") or "C"
            st.set_resolution(q.id, degraded["class"], degraded)
            st.log_evolution(
                "resolution_timeout",
                json.dumps(
                    {
                        "qid": q.id,
                        "detail": f"grace expired (>{grace}d), degraded to {degraded['class']}",
                    },
                    ensure_ascii=False,
                ),
            )
            # 归档语义（T7 审查裁定）：超时降级 → 明确记归档事件，与 resolution_timeout
            # 同轮各一次；策略见 docstring"归档策略"段
            st.log_evolution(
                "resolution_archived",
                json.dumps({"qid": q.id, "reason": "timeout_manual_degrade"}, ensure_ascii=False),
            )
            timeouts += 1
    stats = auto_resolve(st, now)
    # 人工清单语义与 daily.py 共用（8-14 预演前对抗审计）：合法 A 类由 auto_resolve
    # 自动揭晓不列；B 类超宽限已降级 C（同上①）、未超宽限但判定失败/无 client 的 B
    # 与非法 spec 题一并列入待人工；推导内 question_resolution 的 json.loads 由
    # helper 防护。
    from predictor.ops.manual import manual_candidates

    manual = manual_candidates(st, now)
    dd.mkdir(exist_ok=True)
    tmpl = dd / "resolutions.template.csv"
    with tmpl.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "outcome", "source"])
        for q in manual:
            w.writerow([q.id, "", "官方来源待填"])
    stats["manual_c"] = len(manual)
    stats["timeouts"] = timeouts
    # 揭晓后即时刷新战绩快照（旧实现只在预测轮写：8-14 16:30 揭晓后要等次日 09:05）
    write_scoreboard(st, now, data_dir=dd)
    if now.weekday() == 0:
        write_weekly_review(st, now, data_dir=dd)
    return stats


def write_scoreboard(st, now, data_dir: Path | None = None) -> None:
    dd = _data_dir(st) if data_dir is None else data_dir
    buckets = st.brier_by_horizon_bucket()
    dd.mkdir(exist_ok=True)
    (dd / "latest_scoreboard.json").write_text(
        json.dumps(
            {"date": now.date().isoformat(), "buckets": buckets}, indent=2, ensure_ascii=False
        )
    )


def write_weekly_review(st, now, data_dir: Path | None = None) -> None:
    dd = _data_dir(st) if data_dir is None else data_dir
    buckets = st.brier_by_horizon_bucket()
    iso = now.isocalendar()
    lines = [
        f"# 周报 {iso.year}-W{iso.week:02d}",
        f"生成时间: {now.isoformat(timespec='minutes')}",
        "",
        "## 分桶战绩",
        "| 桶 | n | Brier |",
        "|---|---|---|",
    ]
    for b in buckets:
        lines.append(
            f"| {b['bucket']} | {b['n']} | {b['brier_mean']:.4f}{' (样本不足)' if b['unreliable'] else ''} |"
        )
    lines.append("")
    lines.append("## 基线矩阵")
    lines.append("- 常数 base rate：待 P1 补全")
    lines.append("- 族内 base rate：待 P1 补全")
    lines.append("- 简单启发式：待 P1 补全")
    lines.append("- 臂 A（当前系统）：见分桶战绩")
    lines.append("")
    lines.append("## 待人工揭晓")
    for q in st.list_open_questions(by=now):
        lines.append(f"- #{q.id} {q.title} (closes {q.closes_at.date()})")
    out = dd / "weekly_review"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{iso.year}-W{iso.week:02d}.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("round", nargs="?", default="all", choices=["predict", "resolve", "all"])
    ap.add_argument("--db", default=Settings().db_path)
    args = ap.parse_args()
    settings = Settings()
    lock = Path("data/evolve.lock")
    # 双轨并发纪律（8-14 预演前对抗审计）：daily 09:00 主入口先起并持有锁；
    # evolve 后到时等待其完成——备援语义：daily 原生崩溃 → acquire_lock 的 pid
    # 存活检查即时接管，不等 6h stale。预测轮等待上限 90 分钟（8-18 起 7 天更新
    # 轮 daily 可能跑 ~30 分钟）；揭晓轮 5 分钟（16:30 还有活着的 predict 属异常
    # → 快速失败退出码 1 报警）。
    import time as _time

    wait_secs = 90 * 60 if args.round in ("predict", "all") else 5 * 60
    deadline = _time.time() + wait_secs
    print(f"evolve {args.round} started pid={os.getpid()}", flush=True)
    while True:
        try:
            with acquire_lock(lock):
                st = Storage(args.db)
                st.create_schema()
                from scripts.daily import _log_event

                now = datetime.now()
                if args.round in ("predict", "all"):
                    from predictor.llm.client import LLMClient
                    from scripts.daily import _build_sources

                    client = LLMClient(**settings.llm_client_kwargs)
                    _log_event(st, "round_started", json.dumps({"round": "evolve_predict"}))
                    stats = predict_round(st, now=now, client=client, sources=_build_sources())
                    _log_event(
                        st,
                        "round_completed",
                        json.dumps({"round": "evolve_predict", "stats": stats}),
                    )
                    print(f"预测轮: {stats}")
                if args.round in ("resolve", "all"):
                    _log_event(st, "round_started", json.dumps({"round": "evolve_resolve"}))
                    stats = resolve_round(st, now=now)
                    _log_event(
                        st,
                        "round_completed",
                        json.dumps({"round": "evolve_resolve", "stats": stats}),
                    )
                    print(f"揭晓轮: {stats}")
                print(f"evolve {args.round} completed", flush=True)
            break
        except SystemExit as e:
            if _time.time() >= deadline:
                print(f"{e}——等待超时（{wait_secs // 60} 分钟），本轮放弃", flush=True)
                sys.exit(1 if args.round == "resolve" else 0)
            print(f"{e}——等待持有者完成后重试…", flush=True)
            _time.sleep(20)


if __name__ == "__main__":
    main()
