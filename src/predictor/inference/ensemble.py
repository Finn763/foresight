# ensemble.py
from predictor.calibration.postprocess import clamp
from predictor.inference.forecast import ForecastResult


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    pairs.sort()
    total = sum(w for _, w in pairs)
    acc = 0.0
    for p, w in pairs:
        acc += w
        if acc >= total / 2:
            return p
    return pairs[-1][0]


def weights_from_model_stats(stats: list[dict]) -> dict[str, float] | None:
    """把 storage.model_stats() 的行转成模型权重：只保留 brier_ema 非 None 且
    predictions > 0 的行，w = 1/(brier_ema + 0.01) 后归一化使 sum=1。
    无任何有效行时返回 None（调用方退化为等权）。"""
    weights = {
        row["model_name"]: 1.0 / (row["brier_ema"] + 0.01)
        for row in stats
        if row.get("brier_ema") is not None and row.get("predictions", 0) > 0
    }
    if not weights:
        return None
    total = sum(weights.values())
    return {name: w / total for name, w in weights.items()}


def ensemble(runs: list[ForecastResult], weights: dict[str, float] | None = None) -> float:
    if not runs:
        return 0.5
    if weights:
        # 未收录模型 = 无战绩：用已收录中的最小权重兜底（default 1.0 会压过归一化权重，
        # 让无战绩的新模型反而话语权最大）
        floor = min(weights.values())
        pairs = [(r.probability, weights.get(r.model, floor)) for r in runs]
    else:
        pairs = [(r.probability, 1.0) for r in runs]
    return clamp(_weighted_median(pairs))
