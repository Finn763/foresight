"""泄漏受控对比：同一批已揭晓题，零样本 vs 完整管线。检索只取揭晓前文档。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predictor.config import Settings
from predictor.data.benchmarks import fetch_forecastbench_questions
from predictor.data.gdelt_source import GDELTSource
from predictor.data.storage import Storage
from predictor.eval.backtest import constant_baseline_brier, run_zero_shot_backtest
from predictor.llm.client import LLMClient
from predictor.pipeline import run_prediction


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--db", default=":memory:")
    args = ap.parse_args()
    settings = Settings()
    client = LLMClient(**settings.llm_client_kwargs)
    questions = fetch_forecastbench_questions(limit=args.sample)

    zero = run_zero_shot_backtest(client, questions, sample_size=args.sample)

    st = Storage(args.db)
    st.create_schema()
    # 回测假设：揭晓前 30 天做预测（保守且可复现；避免"检索截至揭晓日"的乐观偏差）。
    # GDELT 用 [pred_date-7d, pred_date] 历史窗口查当时新闻（DOC API start/enddatetime 参数）。
    from datetime import timedelta

    pipeline_briers = []
    for i, q in enumerate(questions):
        pred_date = q.closes_at - timedelta(days=30)
        qid = st.add_question(q.title, q.closes_at, is_public=False)  # 回测题不进公开战绩
        pred = run_prediction(
            qid,
            st,
            client,
            [GDELTSource(start=pred_date - timedelta(days=7), end=pred_date)],
            now=pred_date,
        )
        if pred is None:
            continue
        # force_score=True：历史回填题 closes 远早于"今天"，默认延迟归档（>7 天不写
        # brier_score）会令本脚本拿不到分；回测题 is_public=False 不进公开技能桶，
        # 此处计分仅用于内部对比，豁免延迟归档
        st.resolve_question(qid, bool(q.outcome), "benchmark ground truth", force_score=True)
        b = st.brier_latest(qid)
        if b is not None:
            pipeline_briers.append(b)

    import statistics

    # 第三臂：常数基线。用揭晓前 30 天的历史统计基准率（无 LLM、无检索），
    # 网络行情不可达时跳过该题并计数，不让脚本崩。
    baseline_pairs = []
    baseline_skipped = 0
    for q in questions:
        pred_date = q.closes_at - timedelta(days=30)
        try:
            from predictor.stats.baselines import compute_baseline
            from predictor.stats.historical import fetch_series_map

            series_map = fetch_series_map(now=pred_date)  # 已内置防泄漏 period 截断
            br = compute_baseline(q.title, series_map)
            # 无匹配类型/数据不足时 br 为 None，传 None 让纯函数 fallback 0.5
            baseline_pairs.append((br["base_rate"] if br else None, bool(q.outcome)))
        except Exception:
            baseline_skipped += 1
    cb = constant_baseline_brier(baseline_pairs)

    mean = statistics.fmean(pipeline_briers) if pipeline_briers else float("nan")
    report = {
        "zero_shot": {
            "n": zero.n,
            "brier_mean": zero.brier_mean,
            "ci95": [zero.ci95_low, zero.ci95_high],
        },
        "pipeline": {"n": len(pipeline_briers), "brier_mean": mean},
        "constant_base_rate": cb,
        "constant_base_rate_skipped": baseline_skipped,
        "delta_brier": (zero.brier_mean - mean) if pipeline_briers else None,
        "delta_brier_zero_shot_vs_base_rate": (
            (zero.brier_mean - cb["brier_mean"]) if baseline_pairs else None
        ),
        "delta_brier_pipeline_vs_base_rate": (
            (mean - cb["brier_mean"]) if baseline_pairs else None
        ),
        "note": "历史回填题，仅调试，不计入公开战绩；检索截至预测日(closes-30d)，非揭晓日",
    }
    Path("data").mkdir(exist_ok=True)
    Path("data/compare_backtest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
