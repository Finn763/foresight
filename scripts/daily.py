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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    # 任务重定向管道下 stdout 默认块缓冲：原生崩溃（0xc0000005）时整段缓冲丢失、
    # 日志零痕迹（8-12 实锤）——行缓冲保证心跳行能落盘
    sys.stdout.reconfigure(line_buffering=True)
from predictor.config import Settings
from predictor.data.gdelt_source import GDELTSource
from predictor.data.newsapi_source import NewsAPISource
from predictor.data.storage import Storage
from predictor.resolution.spec import validate_resolution_spec
from predictor.selection.families import generate_families

try:
    from predictor.data.crawler_source import CrawlerSource
except ImportError:
    # 兜底：crawler_source 缺失时降级（正常安装必然存在；仅防半成品环境）。
    CrawlerSource = None
from predictor.llm.client import LLMClient
from predictor.websearch_predictor import predict_with_websearch
from scripts.evolve import acquire_lock


def _build_sources() -> list:
    """构造数据源列表。FORESIGHT_DISABLE_SOURCES=gdelt,newsapi 可禁用（网络不可用时用）。"""
    import os

    disabled = {
        s.strip() for s in os.getenv("FORESIGHT_DISABLE_SOURCES", "").split(",") if s.strip()
    }
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
        print(
            "warning: CrawlerSource 未就绪（crawler_source.py 缺失），降级为 [GDELTSource, NewsAPISource]"
        )
    return sources


def _log_event(st, event_type: str, detail: str) -> None:
    """事件写入兜底：日志失败永远不得影响业务操作（预测已入库时 log 失败不能让轮次崩溃）。"""
    try:
        st.log_evolution(event_type, detail)
    except Exception:
        pass


def _ensure_question_families(st: Storage, now: datetime) -> list[int]:
    """按题族生成器补题（四时间档 × 配额纪律 × 难度分档，T5）。

    题族生成器内部已做去重（未揭晓同标题/同族 ≤3）与配额（同日 closes ≤3、
    难度三档各 ≥25%）；此处再经 resolution_spec 校验兜底，非法 spec 不入库。
    返回新增题 id 列表——调用方需"出题即预测"（8-13 预演发现：超短题
    closes=次日，题族补充在预测循环之后，不立即预测则下轮 closes 已过、
    永远无预测；daily 09:00 是双轨下的主出题入口）。
    """
    added_ids: list[int] = []
    for spec in generate_families(st, now):
        # T3 契约：validate_resolution_spec 返回错误列表，空列表=合法 → 只入合法题
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
        _log_event(
            st,
            "question_added",
            json.dumps(
                {"qid": qid, "title": spec.title, "closes": spec.closes_at.isoformat()},
                ensure_ascii=False,
            ),
        )
        print(f"  [题族] #{qid} {spec.title}（closes {spec.closes_at.date()}）")
    return added_ids


def _predict_safely(qid: int, st: Storage, client, sources, now: datetime, label: str = ""):
    """每题预测兜底（2026-08-13 起走 websearch 引擎）：任何异常（DB 写冲突、报告生成、
    LLM 故障等）记 evolution_log 后 skip 单题，不击垮整轮。返回 Prediction 或 None。
    sources 参数保留（签名兼容），websearch 引擎不使用。"""
    try:
        pred = predict_with_websearch(qid, st, client, now)
        if pred is not None:
            _log_event(
                st, "prediction_added", json.dumps({"qid": qid, "prob": round(pred.probability, 4)})
            )
        return pred
    except Exception as e:
        _log_event(
            st,
            "prediction_skipped",
            json.dumps({"qid": qid, "detail": f"pipeline exception: {e}"}, ensure_ascii=False),
        )
        print(f"  跳过(异常): #{qid}{label}: {type(e).__name__}: {e}")
        return None


def _manual_candidates(st: Storage, now: datetime) -> list:
    """到期待揭晓题中需要人工处理的部分：无 spec / class B / class C / A 类 spec 非法
    （自动揭晓必失败）。合法 A 类由 16:30 auto_resolve 自动揭晓——不进人工清单，
    防止把自动题提前人工判死（8-14 预演前对抗审计：daily 09:00 清单曾列出 A 类 #67，
    照提示在美股收盘前填写即永久错判，16:30 自动揭晓被跳过）。"""
    out = []
    for q in st.list_open_questions(by=now):
        try:
            spec = st.question_resolution(q.id)
        except Exception:
            out.append(q)  # spec JSON 损坏 → 无法自动 → 人工（与 auto_resolve 对称防护）
            continue
        if spec is None or spec.get("class") != "A":
            out.append(q)
            continue
        if validate_resolution_spec(spec):
            out.append(q)  # 非法 A spec → 自动揭晓必失败 → 人工
    return out


def _run(args, settings: Settings) -> None:
    print(f"daily started pid={os.getpid()}", flush=True)
    st = Storage(args.db)
    st.create_schema()
    _log_event(st, "round_started", json.dumps({"round": "daily_predict"}))
    client = LLMClient(**settings.llm_client_kwargs)
    sources = _build_sources()
    now = datetime.now()

    # ① 未到期题 → 无预测则首次预测；距上次预测 ≥7 天则更新预测（持续更新纪律：
    # 新证据→新概率；resolve 只认最后一条，旧行作废）
    predicted, skipped = 0, 0
    for q in st.list_unresolved():
        if q.closes_at <= now:
            continue  # 到期题走②
        last = st.last_prediction_at(q.id)
        if last is not None and (now - last).days < 7:
            continue  # 7 天内已预测：短周期题不重复打扰，长周期题每周更新
        if _predict_safely(q.id, st, client, sources, now) is not None:
            predicted += 1
        else:
            skipped += 1

    # ② 到期未揭晓 → 人工清单（可自动揭晓的 A 类不列，防收盘前人工错判）+ 自动待定提示
    due = st.list_open_questions(by=now)
    manual = _manual_candidates(st, now)
    manual_ids = {q.id for q in manual}
    auto_due = [q for q in due if q.id not in manual_ids]
    # 模板总是重写（空清单也写表头），避免残留旧清单误导人工流程
    Path("data").mkdir(exist_ok=True)
    tmpl = Path("data/resolutions.template.csv")
    with tmpl.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "outcome", "source"])
        for q in manual:
            w.writerow([q.id, "", "官方来源待填"])
    if manual:
        print(
            f"需人工揭晓 {len(manual)} 题 → 查官方结果后编辑 data/resolutions.csv 再跑 resolve.py"
        )
        for q in manual:
            print(f"  #{q.id} {q.title} (closes {q.closes_at.date()})")
        print("  注：B 类题由 LLM 自动判定中（宽限 3 天），无需人工填表；超宽限仍未揭晓才需人工")
    if auto_due:
        print(f"自动揭晓待定 {len(auto_due)} 题（16:30 evolve resolve 自动处理，勿人工填表）:")
        for q in auto_due:
            print(f"  #{q.id} {q.title} (closes {q.closes_at.date()})")

    # ②.5 题族自动补充（四时间档：超短≤3 天每天滚动、短/中档每周、长档月度）
    # 出题即预测：题族在预测循环之后，新题不在循环快照；超短题 closes=次日，
    # 不立即预测则下轮 closes 已过、永远无预测（8-13 预演发现，与 evolve 对称）。
    added_ids = _ensure_question_families(st, now)
    for qid in added_ids:
        if _predict_safely(qid, st, client, sources, now, label="（题族新题）") is not None:
            predicted += 1
        else:
            skipped += 1

    # ③ 战绩快照
    buckets = st.brier_by_horizon_bucket()
    Path("data").mkdir(exist_ok=True)
    Path("data/latest_scoreboard.json").write_text(
        json.dumps(
            {
                "date": now.date().isoformat(),
                "buckets": buckets,
                "predicted_today": predicted,
                "skipped": skipped,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(f"今日新增预测 {predicted}，跳过 {skipped}")
    for b in buckets:
        flag = " (样本不足)" if b["unreliable"] else ""
        print(f"  {b['bucket']}: n={b['n']} Brier={b['brier_mean']:.4f}{flag}")
    _log_event(
        st,
        "round_completed",
        json.dumps(
            {
                "round": "daily_predict",
                "stats": {
                    "predicted": predicted,
                    "skipped": skipped,
                    "families_added": len(added_ids),
                },
            }
        ),
    )
    print("daily completed", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=Settings().db_path)
    args = ap.parse_args()
    settings = Settings()
    # 与 evolve 共用一把锁：daily 先起（09:00），evolve 后到（09:05）拿不到锁时
    # 优雅跳过（对称轨道已覆盖）；反过来 daily 在 evolve 手动运行期间也不会撞库
    with acquire_lock(Path("data/evolve.lock"), caller="daily"):
        _run(args, settings)


if __name__ == "__main__":
    main()
