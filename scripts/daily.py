"""python scripts/daily.py [--db path]
每日编排：① 未到期且无预测的题 → 跑管线补预测（跳过失败题）
② 到期未揭晓 → 生成 data/resolutions.template.csv 提示人工填（自动可揭晓的 A 类除外）
③ 写 data/latest_scoreboard.json 并打印分桶战绩。

并发纪律：与 evolve.py 共用 data/evolve.lock（daily 09:00 是双轨主入口；evolve 09:05
拿到锁失败会优雅跳过，防止两个进程同时写 DuckDB——Windows 上第二个写连接直接
IOException 崩溃）。"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
from predictor.config import Settings
from predictor.data.gdelt_source import GDELTSource
from predictor.data.newsapi_source import NewsAPISource
from predictor.data.storage import Storage
from predictor.resolution.spec import validate_resolution_spec
from predictor.selection.families import generate_families
from predictor.llm.client import LLMClient, dump_daily_usage
from predictor.ops.lock import acquire_lock
from predictor.ops.manual import manual_candidates
from predictor.websearch_predictor import predict_with_websearch

def _build_sources() -> list:
    """构造数据源列表。FORESIGHT_DISABLE_SOURCES=gdelt,newsapi 可禁用。"""
    disabled = {s.strip() for s in os.getenv("FORESIGHT_DISABLE_SOURCES", "").split(",") if s.strip()}
    sources = []
    if "gdelt" not in disabled:
        sources.append(GDELTSource())
    if "newsapi" not in disabled:
        sources.append(NewsAPISource())
    try:
        from predictor.data.crawler_source import CrawlerSource
        if "crawler" not in disabled:
            sources.append(CrawlerSource())
    except ImportError:
        print("warning: CrawlerSource 未就绪（crawler_source.py 缺失），降级为 [GDELTSource, NewsAPISource]")
    return sources

def _log_event(st, event_type: str, detail: str) -> None:
    try:
        st.log_evolution(event_type, detail)
    except Exception:
        pass

def _within_update_window(now: datetime, last: datetime) -> bool:
    return (now - last) < timedelta(hours=168)

def _ensure_question_families(st: Storage, now: datetime) -> list[int]:
    """按题族生成器补题（四时间档 × 配额纪律 × 难度分档，T5）。
    返回新增题 id 列表——调用方需"出题即预测"。
    """
    added_ids: list[int] = []
    for spec in generate_families(st, now):
        if validate_resolution_spec(spec.resolution_spec):
            print(f"  跳过(非法spec): {spec.title}")
            continue
        qid = st.add_question(
            spec.title,
            spec.closes_at,
            is_public=spec.is_public,
            resolution_class=spec.resolution_class,
            resolution_spec=spec.resolution_spec,
        )
        added_ids.append(qid)
        _log_event(st, "question_added", json.dumps({"qid": qid, "title": spec.title, "closes": spec.closes_at.isoformat()}, ensure_ascii=False))
        print(f"  [题族] #{qid} {spec.title}（closes {spec.closes_at.date()}）")
    return added_ids

def _predict_safely(qid: int, st: Storage, client, *args, label: str = ""):
    """每题预测兜底：任何异常记 evolution_log 后 skip 单题，不击垮整轮。ponytail: sources param trimmed, compat shim keeps legacy calls."""
    # 兼容旧签名 _predict_safely(qid,st,client,sources,now) 与新签名 (qid,st,client,now)
    now = None
    if len(args) == 1 and isinstance(args[0], datetime):
        now = args[0]
    elif len(args) == 2:
        # 旧：sources, now
        now = args[1]
    elif len(args) == 1:
        now = args[0]
    else:
        raise TypeError("_predict_safely args mismatch")
    try:
        pred = predict_with_websearch(qid, st, client, now)
        if pred is not None:
            _log_event(st, "prediction_added", json.dumps({"qid": qid, "prob": round(pred.probability, 4)}))
        return pred
    except Exception as e:
        _log_event(st, "prediction_skipped", json.dumps({"qid": qid, "detail": f"pipeline exception: {e}"}, ensure_ascii=False))
        print(f"  跳过(异常): #{qid}{label}: {type(e).__name__}: {e}")
        return None

def _run(args, settings: Settings) -> None:
    print(f"daily started pid={os.getpid()}", flush=True)
    st = Storage(args.db)
    st.create_schema()
    _log_event(st, "round_started", json.dumps({"round": "daily_predict"}))
    client = LLMClient(**settings.llm_client_kwargs)
    sources = _build_sources()
    now = datetime.now()

    predicted, skipped = 0, 0
    for q in st.list_unresolved():
        if q.closes_at <= now:
            continue
        last = st.last_prediction_at(q.id)
        if last is not None and _within_update_window(now, last):
            continue
        if _predict_safely(q.id, st, client, now) is not None:
            predicted += 1
        else:
            skipped += 1

    due = st.list_open_questions(by=now)
    manual = manual_candidates(st, now)
    manual_ids = {q.id for q in manual}
    auto_due = [q for q in due if q.id not in manual_ids]
    Path("data").mkdir(exist_ok=True)
    tmpl = Path("data/resolutions.template.csv")
    with tmpl.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "outcome", "source"])
        for q in manual:
            w.writerow([q.id, "", "官方来源待填"])
    if manual:
        print(f"需人工揭晓 {len(manual)} 题 → 查官方结果后编辑 data/resolutions.csv 再跑 resolve.py")
        for q in manual:
            print(f"  #{q.id} {q.title} (closes {q.closes_at.date()})")
        print("  注：B 类题由 LLM 自动判定中（宽限 3 天），无需人工填表；超宽限仍未揭晓才需人工")
    if auto_due:
        print(f"自动揭晓待定 {len(auto_due)} 题（16:30 evolve resolve 自动处理，勿人工填表）:")
        for q in auto_due:
            print(f"  #{q.id} {q.title} (closes {q.closes_at.date()})")

    added_ids = _ensure_question_families(st, now)
    for qid in added_ids:
        if _predict_safely(qid, st, client, now, label="（题族新题）") is not None:
            predicted += 1
        else:
            skipped += 1

    buckets = st.brier_by_horizon_bucket()
    Path("data").mkdir(exist_ok=True)
    Path("data/latest_scoreboard.json").write_text(json.dumps({"date": now.date().isoformat(), "buckets": buckets, "predicted_today": predicted, "skipped": skipped}, indent=2, ensure_ascii=False))
    print(f"今日新增预测 {predicted}，跳过 {skipped}")
    for b in buckets:
        flag = " (样本不足)" if b["unreliable"] else ""
        print(f"  {b['bucket']}: n={b['n']} Brier={b['brier_mean']:.4f}{flag}")
    _log_event(st, "round_completed", json.dumps({"round": "daily_predict", "stats": {"predicted": predicted, "skipped": skipped, "families_added": len(added_ids)}}))
    dump_daily_usage(Path("data") / "daily.log")
    print("daily completed", flush=True)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=Settings().db_path)
    args = ap.parse_args()
    settings = Settings()
    with acquire_lock(Path("data/evolve.lock"), caller="daily"):
        _run(args, settings)

if __name__ == "__main__":
    main()
