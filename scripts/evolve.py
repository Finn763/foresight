"""python scripts/evolve.py [predict|resolve|all] [--db path]
自我进化闭环编排（单入口）：
 预测轮（09:05）：① 未到期题补预测/7×24h 更新 ② 题族补充 ③ 战绩快照
 揭晓轮（16:30）：① 宽限过期 A 类兜底降级人工 ② auto_resolve ③ 待人工清单
文件锁：data/evolve.lock，6 小时 stale 接管。"""

import argparse
import csv
import json
import os
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
from scripts.daily import _build_sources, _log_event, _predict_safely, _within_update_window
from predictor.config import Settings
from predictor.data.storage import Storage
from predictor.ops.lock import acquire_lock
from predictor.resolution.auto_resolve import auto_resolve
from predictor.resolution.spec import validate_resolution_spec
from predictor.selection.families import generate_families

def _data_dir(st) -> Path:
    p = getattr(st, "path", None)
    return Path(p).parent if p else Path("data")

_CANONICAL_TITLE = "未来7天内标普500会创新高吗"

def _build_base_rates(now: datetime | None = None) -> dict:
    try:
        from predictor.stats.baselines import compute_baseline
        from predictor.stats.historical import fetch_series_map
        sm = fetch_series_map(now=now)
        b = compute_baseline(_CANONICAL_TITLE, sm, now=now)
        if b and b.get("base_rate") is not None:
            return {"标普": b["base_rate"]}
    except Exception:
        pass
    return {}

def predict_round(st, *, now: datetime, client, sources, base_rates: dict | None = None) -> dict:
    """预测轮：① 未到期未预测/满 7×24h 未更新 → 预测 ② 题族补充 ③ 战绩快照。"""
    stats = {"predicted": 0, "skipped": 0, "families_added": 0}
    for q in st.list_unresolved():
        if q.closes_at <= now:
            continue
        last = st.last_prediction_at(q.id)
        if last is not None and _within_update_window(now, last):
            continue
        pa = _predict_safely(q.id, st, client, now)
        if pa is not None:
            stats["predicted"] += 1
        else:
            stats["skipped"] += 1
    if base_rates is None:
        base_rates = _build_base_rates(now)
    specs = generate_families(st, now, base_rates=base_rates)
    for spec in specs:
        if not validate_resolution_spec(spec.resolution_spec):
            qid = st.add_question(spec.title, spec.closes_at, resolution_class=spec.resolution_class, resolution_spec=spec.resolution_spec)
            stats["families_added"] += 1
            _log_event(st, "question_added", json.dumps({"qid": qid, "title": spec.title, "closes": spec.closes_at.isoformat()}, ensure_ascii=False))
            if client is not None:
                pa = _predict_safely(qid, st, client, now, label="（题族新题）")
                if pa is not None:
                    stats["predicted"] += 1
    write_scoreboard(st, now)
    return stats

def resolve_round(st, *, now: datetime, data_dir: Path | None = None) -> dict:
    """揭晓轮：① 宽限过期 A/B 类 → 标 C 降级人工 ② auto_resolve ③ 人工清单。"""
    dd = _data_dir(st) if data_dir is None else data_dir
    timeouts = 0
    for q in st.list_open_questions(by=now):
        try:
            spec = st.question_resolution(q.id)
        except Exception as e:
            st.log_evolution("resolution_failed", json.dumps({"qid": q.id, "detail": f"spec broken in timeout check: {e}"}, ensure_ascii=False))
            continue
        if spec is None or spec.get("class") not in ("A", "B"):
            continue
        try:
            grace = int(spec.get("grace_days", 3))
        except (TypeError, ValueError):
            grace = 3
        if now > q.closes_at + timedelta(days=grace):
            degraded = dict(spec)
            degraded["class"] = spec.get("degrade_to") or "C"
            st.set_resolution(q.id, degraded["class"], degraded)
            st.log_evolution("resolution_timeout", json.dumps({"qid": q.id, "detail": f"grace expired (>{grace}d), degraded to {degraded['class']}"}, ensure_ascii=False))
            st.log_evolution("resolution_archived", json.dumps({"qid": q.id, "reason": "timeout_manual_degrade"}, ensure_ascii=False))
            timeouts += 1
    stats = auto_resolve(st, now)
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
    write_scoreboard(st, now, data_dir=dd)
    return stats

def write_scoreboard(st, now, data_dir: Path | None = None) -> None:
    dd = _data_dir(st) if data_dir is None else data_dir
    buckets = st.brier_by_horizon_bucket()
    dd.mkdir(exist_ok=True)
    (dd / "latest_scoreboard.json").write_text(json.dumps({"date": now.date().isoformat(), "buckets": buckets}, indent=2, ensure_ascii=False))

def write_weekly_review(st, now, data_dir: Path | None = None) -> None:
    # ponytail: trimmed 40L → 10L keeps import compat + file existence for legacy weekly test
    dd = _data_dir(st) if data_dir is None else data_dir
    buckets = st.brier_by_horizon_bucket()
    iso = now.isocalendar()
    lines = [f"# 周报 {iso.year}-W{iso.week:02d}", f"生成时间: {now.isoformat(timespec='minutes')}", "", "## 分桶战绩", "| 桶 | n | Brier |", "|---|---|---|"]
    for b in buckets:
        lines.append(f"| {b['bucket']} | {b['n']} | {b['brier_mean']:.4f}{' (样本不足)' if b['unreliable'] else ''} |")
    lines += ["", "## 基线矩阵", "- 常数 base rate：待 P1 补全", "- 族内 base rate：待 P1 补全", "- 简单启发式：待 P1 补全", "- 臂 A（当前系统）：见分桶战绩", "", "## 待人工揭晓"]
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
    wait_secs = 90 * 60 if args.round in ("predict", "all") else 5 * 60
    deadline = _time.time() + wait_secs
    print(f"evolve {args.round} started pid={os.getpid()}", flush=True)
    while True:
        try:
            with acquire_lock(lock):
                st = Storage(args.db)
                st.create_schema()
                now = datetime.now()
                if args.round in ("predict", "all"):
                    from predictor.llm.client import LLMClient
                    client = LLMClient(**settings.llm_client_kwargs)
                    _log_event(st, "round_started", json.dumps({"round": "evolve_predict"}))
                    stats = predict_round(st, now=now, client=client, sources=_build_sources())
                    _log_event(st, "round_completed", json.dumps({"round": "evolve_predict", "stats": stats}))
                    print(f"预测轮: {stats}")
                if args.round in ("resolve", "all"):
                    _log_event(st, "round_started", json.dumps({"round": "evolve_resolve"}))
                    stats = resolve_round(st, now=now)
                    _log_event(st, "round_completed", json.dumps({"round": "evolve_resolve", "stats": stats}))
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
