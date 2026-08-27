"""python scripts/backtest.py --sample 50
用真实 deepseek 跑零样本基线，打印 Brier + 置信区间。结果存 data/backtest_baseline.json。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predictor.config import Settings
from predictor.data.benchmarks import fetch_forecastbench_questions
from predictor.eval.backtest import json_safe, run_zero_shot_backtest
from predictor.llm.client import LLMClient


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=50)
    args = ap.parse_args()
    settings = Settings()
    client = LLMClient(**settings.llm_client_kwargs)
    questions = fetch_forecastbench_questions(limit=args.sample)
    rep = run_zero_shot_backtest(client, questions, sample_size=args.sample)
    report = {
        "n": rep.n,
        "skipped_outcome_missing": rep.skipped_outcome_missing,
        "brier_mean": rep.brier_mean,
        "brier_sd": rep.brier_sd,
        "ci95_low": rep.ci95_low,
        "ci95_high": rep.ci95_high,
        "baseline_name": rep.baseline_name,
    }
    # json_safe 把 NaN（无样本臂）转 null；allow_nan=False 兜底不写非法 JSON 字面量
    payload = json.dumps(json_safe(report), indent=2, ensure_ascii=False, allow_nan=False)
    Path("data").mkdir(exist_ok=True)
    Path("data/backtest_baseline.json").write_text(payload, encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
