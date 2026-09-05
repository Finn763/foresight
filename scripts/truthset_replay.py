"""TruthSet 重放：对 v1.jsonl 每题按 pred_date=max(opens_at, closes_at-30天) 重跑经典管线。

GDELT 取 [pred_date-7d, pred_date] 历史窗口（与 compare_backtest.py 同款防泄漏口径）；
Storage(':memory:') 不落生产库；Brier 与 horizon 分桶脚本内现算，口径对齐
生产 scoreboard 的 brier_by_horizon_bucket（<=7 / 7-30 / 30-90 / >=90，按揭晓前天数）。
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predictor.config import Settings
from predictor.data.gdelt_source import GDELTSource
from predictor.data.storage import Storage
from predictor.eval.backtest import require_outcome
from predictor.llm.client import LLMClient
from predictor.pipeline import run_prediction

BUCKET_ORDER = ("<=7", "7-30", "30-90", ">=90")


def horizon_bucket(days: int) -> str:
    if days <= 7:
        return "<=7"
    if days <= 30:
        return "7-30"
    if days <= 90:
        return "30-90"
    return ">=90"


def load_truthset(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def replay(rows: list[dict], client, limit: int | None) -> dict:
    st = Storage(":memory:")
    st.create_schema()
    briers: list[float] = []
    buckets: dict[str, list[float]] = {}
    skipped = 0
    for row in rows[:limit]:
        if row.get("outcome") is None:
            skipped += 1
            continue
        opens_at = datetime.fromisoformat(row["opens_at"])
        closes_at = datetime.fromisoformat(row["closes_at"])
        # 揭晓前 30 天做预测（compare_backtest 同款假设）；题开得晚就以 opens_at 为准
        pred_date = max(opens_at, closes_at - timedelta(days=30))
        spec = row.get("resolution_spec") or {}
        qid = st.add_question(
            row["title"],
            closes_at,
            opens_at=opens_at,
            resolution_class=spec.get("class"),
            resolution_spec=spec,
        )
        pred = run_prediction(
            qid,
            st,
            client,
            [GDELTSource(start=pred_date - timedelta(days=7), end=pred_date)],
            now=pred_date,
        )
        if pred is None:
            skipped += 1
            continue
        b = (pred.probability - int(require_outcome(row["outcome"]))) ** 2
        briers.append(b)
        buckets.setdefault(horizon_bucket((closes_at - pred_date).days), []).append(b)
    bucket_rows = [
        {
            "bucket": b,
            "n": len(v),
            "brier_mean": round(sum(v) / len(v), 4),
        }
        for b in BUCKET_ORDER
        if (v := buckets.get(b))
    ]
    return {
        "n": len(briers),
        "skipped": skipped,
        "brier_mean": round(sum(briers) / len(briers), 4) if briers else None,
        "buckets": bucket_rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--truthset", default="data/truthset/v1.jsonl")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    settings = Settings()
    client = LLMClient(**settings.llm_client_kwargs)
    rows = load_truthset(Path(args.truthset))
    out = replay(rows, client, args.limit)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
