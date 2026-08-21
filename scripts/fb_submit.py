"""python scripts/fb_submit.py [--limit 20] [--dry-run] [--db data/foresight.db]

ForecastBench 官方提交（Task 22，已查证通道：邮件注册 + GCP bucket 上传 Forecast Set）：
  拉官方最新一期 question set 未解决题 → 复制进本地题池（is_public=FALSE）→
  跑预测管线 → 生成 forecast set JSON 落盘 data/forecast_sets/（本地记账）→
  配置 FORECASTBENCH_GCS_BUCKET 且有 gcloud 时自动上传。

--dry-run   只跑管线，不生成 forecast set、不记账。
真实提交不再需要 API token（官方通道无 token 机制）；上传需先邮件注册拿到 bucket：
  forecastbench@forecastingresearch.org（详见 docs/forecastbench提交渠道调研.md）。
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predictor.config import Settings
from predictor.data.forecastbench_official import (
    LEDGER_SOURCE_TAG,
    fetch_open_questions,
    load_ledger,
    record_submissions,
    resolve_api_token,
    submit_predictions,
)
from predictor.data.gdelt_source import GDELTSource
from predictor.data.newsapi_source import NewsAPISource
from predictor.data.storage import Storage
from predictor.llm.client import LLMClient
from predictor.pipeline import run_prediction

try:
    from predictor.data.crawler_source import CrawlerSource
except ImportError:
    CrawlerSource = None  # 未实现时降级，同 daily.py


def main() -> None:
    ap = argparse.ArgumentParser(description="ForecastBench 官方提交通道")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true", help="只跑管线，不生成 forecast set、不记账")
    ap.add_argument("--db", default=Settings().db_path)
    ap.add_argument(
        "--ledger", default=None, help="记账文件路径（默认 data/forecastbench_ledger.json）"
    )
    args = ap.parse_args()

    settings = Settings()
    st = Storage(args.db)
    st.create_schema()
    client = LLMClient(**settings.llm_client_kwargs)
    sources = [GDELTSource(), NewsAPISource()]
    if CrawlerSource is not None:
        sources.append(CrawlerSource())

    # ① 拉官方未解决题（本地 fb_seed 最新一期；网络恢复后可走 raw 降级）
    try:
        questions = fetch_open_questions(limit=args.limit)
    except Exception as e:
        print(f"[fb_submit] 拉题失败：{e}", file=sys.stderr)
        print("[fb_submit] 提示：端点/字段见 docs/forecastbench提交渠道调研.md", file=sys.stderr)
        sys.exit(1)
    if not questions:
        print("[fb_submit] 当前没有可提交的未解决题")
        return

    # ② 去重：ledger 里已提交过（未揭晓）的题跳过
    ledger = load_ledger(args.ledger) if args.ledger else load_ledger()
    submitted_ids = {
        str(e.get("question_id")) for e in ledger if e.get("question_id") and not e.get("resolved")
    }
    todo = [q for q in questions if q.id not in submitted_ids]
    skipped = len(questions) - len(todo)
    if skipped:
        print(f"[fb_submit] 已提交过/待揭晓，跳过 {skipped} 题")

    # ③ 入本地题池（is_public=FALSE）+ 跑管线
    predictions = []  # [{question_id(官方), probability, local_question_id, title, closes_at}]
    for q in todo:
        local_id = st.add_question(q.title, q.closes_at, is_public=False)
        pred = run_prediction(local_id, st, client, sources)
        if pred is None:
            print(f"  跳过(无证据): {q.title}")
            continue
        predictions.append(
            {
                "question_id": q.id,
                "probability": pred.probability,
                "local_question_id": local_id,
                "title": q.title,
                "closes_at": q.closes_at.isoformat(),
            }
        )
        print(f"  #{q.id} p={pred.probability:.3f}  {q.title}")

    if not predictions:
        print("[fb_submit] 无题可提交")
        return

    if args.dry_run:
        print(
            f"[fb_submit] dry-run：管线产出 {len(predictions)} 个预测，未生成 forecast set、未记账"
        )
        return

    # ④ 生成 Forecast Set（官方通道：邮件注册 + GCP bucket 上传，无 token 机制；
    #    api_token 按计划签名保留，当前忽略）
    try:
        submitted = submit_predictions(predictions, api_token=resolve_api_token())
    except Exception as e:
        print(f"[fb_submit] 生成 forecast set 失败：{e}", file=sys.stderr)
        sys.exit(1)

    # ⑤ 本地记账
    now = datetime.now(UTC).isoformat()
    entries = [
        {
            "question_id": str(p["question_id"]),
            "local_question_id": p["local_question_id"],
            "title": p["title"],
            "probability": p["probability"],
            "closes_at": p["closes_at"],
            "submitted_at": now,
            "source": LEDGER_SOURCE_TAG,
            "resolved": False,
            "outcome": None,
        }
        for p in predictions
    ]
    n = (
        record_submissions(entries, path=args.ledger)
        if args.ledger
        else record_submissions(entries)
    )
    print(
        f"[fb_submit] 已生成 forecast set {submitted} 条 → "
        f"data/forecast_sets/；记账 {n} 条 → {args.ledger or 'data/forecastbench_ledger.json'}"
    )
    print(
        "[fb_submit] 上传：配置 FORECASTBENCH_GCS_BUCKET（邮件注册后）且有 gcloud 时自动执行；"
        "否则人工上传 data/forecast_sets/"
    )


if __name__ == "__main__":
    main()
