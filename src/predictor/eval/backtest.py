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


def require_outcome(outcome: bool | None) -> bool:
    """outcome 显式降为 bool；None（未揭晓/无地面真值）直接抛错。

    防御 None→False 静默转换：bool(None)=False 会把全部回测题按「未发生」
    计分，回测对比整体失真。调用方必须先显式跳过再计分。
    """
    if outcome is None:
        raise ValueError(
            "outcome 为 None（未揭晓/无地面真值）：禁止按 False 计分，须先显式跳过"
        )
    return bool(outcome)


def json_safe(obj: Any) -> Any:
    """递归把非有限 float（NaN/±Inf）替换为 None（null）。

    json.dumps 默认把 NaN 写成非法 JSON 字面量（json.loads 解析失败）。
    报告序列化前先过此函数，再配合 allow_nan=False：任何残留非有限值
    都会在 dumps 时抛 ValueError，而不是静默产出坏 JSON。
    """
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


@dataclass
class BacktestReport:
    n: int
    brier_mean: float
    brier_sd: float
    ci95_low: float
    ci95_high: float
    baseline_name: str
    skipped_outcome_missing: int = 0


def run_zero_shot_backtest(
    client: Any, questions: list[BenchQuestion], *, sample_size: int = 50
) -> BacktestReport:
    """零样本基线：题目直接给 LLM，无检索。返回 Brier 统计。
    只对已揭晓题（outcome 可判定）计分；未解决题显式跳过并计数（官方提交流程走 Task 22）。"""
    sample = questions[:sample_size]
    scores: list[float] = []
    skipped = 0
    for q in sample:
        if q.outcome is None:
            skipped += 1
            continue  # 无答案的题无法计分，跳过
        outcome = require_outcome(q.outcome)  # 防御：禁止 None→False 静默转换
        out = client.chat_json(
            [
                {"role": "system", "content": SUPERFORECASTER_SYSTEM},
                {"role": "user", "content": ZERO_SHOT_TEMPLATE.format(title=q.title)},
            ]
        )
        p = float(out.get("probability", 0.5))
        p = max(0.01, min(0.99, p))
        scores.append((p - int(outcome)) ** 2)
    if not scores:
        return BacktestReport(
            0,
            float("nan"),
            float("nan"),
            float("nan"),
            float("nan"),
            "zero-shot",
            skipped_outcome_missing=skipped,
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
        skipped_outcome_missing=skipped,
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


def build_compare_report(
    *,
    zero: BacktestReport,
    pipeline_briers: list[float],
    cb: dict,
    pipeline_skipped: int,
    baseline_skipped: int,
    baseline_outcome_skipped: int,
) -> dict:
    """组装三臂回测对比报告（纯函数，供 scripts/compare_backtest.py 使用，便于单测）。

    无样本臂的 brier_mean 为 NaN，序列化时由 json_safe 转 null；对比项在任一侧
    无样本时显式置 None，避免 NaN 差值混入报告。
    """
    mean = statistics.fmean(pipeline_briers) if pipeline_briers else float("nan")
    return {
        "zero_shot": {
            "n": zero.n,
            "skipped_outcome_missing": zero.skipped_outcome_missing,
            "brier_mean": zero.brier_mean,
            "ci95": [zero.ci95_low, zero.ci95_high],
        },
        "pipeline": {
            "n": len(pipeline_briers),
            "skipped_outcome_missing": pipeline_skipped,
            "brier_mean": mean,
        },
        "constant_base_rate": {**cb, "skipped_outcome_missing": baseline_outcome_skipped},
        "constant_base_rate_skipped": baseline_skipped,
        "delta_brier": (zero.brier_mean - mean) if (zero.n and pipeline_briers) else None,
        "delta_brier_zero_shot_vs_base_rate": (
            (zero.brier_mean - cb["brier_mean"]) if (zero.n and cb["n"]) else None
        ),
        "delta_brier_pipeline_vs_base_rate": (
            (mean - cb["brier_mean"]) if (pipeline_briers and cb["n"]) else None
        ),
        "note": (
            "历史回填题，仅调试，不计入公开战绩；检索截至预测日(closes-30d)，非揭晓日；"
            "outcome 缺失（fb_seed 官方题集无 resolution 字段，无地面真值）的题显式跳过，"
            "计数见各臂 skipped_outcome_missing，禁止按 bool(None)=False 计分"
        ),
    }
