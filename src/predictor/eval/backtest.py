"""泄漏受控回测：只对已揭晓题算 Brier，输出均值/标准差/95% 正态 CI。
仅供调试基线与建先验，永不计入公开战绩。"""

import math
import statistics
from dataclasses import dataclass
from typing import Any

from predictor.data.benchmarks import BenchQuestion
from predictor.llm.prompts import SUPERFORECASTER_SYSTEM

ZERO_SHOT_TEMPLATE = (
    "Predict whether this claim will be true. Output JSON: "
    '{{"probability": 0.0-1.0, "rationale": "..."}}\n'
    "Claim: {title}"
)


@dataclass
class BacktestReport:
    n: int
    brier_mean: float
    brier_sd: float
    ci95_low: float
    ci95_high: float
    baseline_name: str


def run_zero_shot_backtest(
    client: Any, questions: list[BenchQuestion], *, sample_size: int = 50
) -> BacktestReport:
    """零样本基线：题目直接给 LLM，无检索。返回 Brier 统计。
    只对已揭晓题（outcome 可判定）计分；未解决题跳过（官方提交流程走 Task 22）。"""
    sample = questions[:sample_size]
    scores: list[float] = []
    for q in sample:
        if q.outcome is None:
            continue  # 无答案的题无法计分，跳过
        out = client.chat_json(
            [
                {"role": "system", "content": SUPERFORECASTER_SYSTEM},
                {"role": "user", "content": ZERO_SHOT_TEMPLATE.format(title=q.title)},
            ]
        )
        p = float(out.get("probability", 0.5))
        p = max(0.01, min(0.99, p))
        scores.append((p - int(bool(q.outcome))) ** 2)
    if not scores:
        return BacktestReport(
            0, float("nan"), float("nan"), float("nan"), float("nan"), "zero-shot"
        )
    mean = statistics.fmean(scores)
    sd = statistics.stdev(scores) if len(scores) > 1 else 0.0
    se = sd / math.sqrt(len(scores))
    return BacktestReport(
        n=len(scores),
        brier_mean=mean,
        brier_sd=sd,
        ci95_low=mean - 1.96 * se,
        ci95_high=mean + 1.96 * se,
        baseline_name="zero-shot deepseek-chat",
    )


def constant_baseline_brier(pairs: list[tuple[float | None, bool]]) -> dict:
    """常数基线臂：对每对 (base_rate, outcome) 算 Brier。

    base_rate 为 None 时用 0.5 替代（均匀先验）；空列表 brier_mean 为 NaN。
    纯函数，无网络、无 LLM 依赖，供回测对比第三臂使用。"""
    if not pairs:
        return {"n": 0, "brier_mean": float("nan")}
    scores = [
        ((0.5 if base_rate is None else base_rate) - int(outcome)) ** 2
        for base_rate, outcome in pairs
    ]
    return {"n": len(pairs), "brier_mean": statistics.fmean(scores)}
