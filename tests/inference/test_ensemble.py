import pytest

from predictor.inference.ensemble import ensemble, weights_from_model_stats
from predictor.inference.forecast import ForecastResult


def test_ensemble_median():
    runs = [
        ForecastResult(0.4, "a", "m"),
        ForecastResult(0.6, "b", "m"),
        ForecastResult(0.8, "c", "m"),
    ]
    assert ensemble(runs) == pytest.approx(0.6)


def test_ensemble_clamped():
    assert ensemble([ForecastResult(0.001, "x", "m")]) >= 0.01


def test_ensemble_empty_returns_0_5():
    assert ensemble([]) == 0.5


def test_ensemble_weighted_median():
    runs = [ForecastResult(0.4, "a", "m1"), ForecastResult(0.8, "b", "m2")]
    assert ensemble(runs, weights={"m1": 1.0, "m2": 3.0}) == pytest.approx(0.8)


def test_ensemble_unknown_model_weight_defaults_floor():
    """未收录模型用已收录最小权重兜底（default 1.0 会压过归一化权重）。"""
    runs = [ForecastResult(0.4, "a", "m1"), ForecastResult(0.8, "b", "m2")]
    # m2 未收录 → 拿 m1 权重 0.5（最小值）；0.4 权重 0.5 vs 0.8 权重 0.5 → 中位数 0.4
    assert ensemble(runs, weights={"m1": 0.5, "m3": 0.9}) == pytest.approx(0.4)


def _stats_row(model: str, predictions: int, brier_ema: float | None) -> dict:
    return {
        "model_name": model,
        "predictions": predictions,
        "brier_ema": brier_ema,
        "last_updated": "2026-01-01 00:00:00",
    }


def test_weights_lower_brier_ema_gets_higher_weight():
    stats = [
        _stats_row("good", 10, 0.10),
        _stats_row("bad", 10, 0.30),
    ]
    weights = weights_from_model_stats(stats)
    assert weights is not None
    assert weights["good"] > weights["bad"]


def test_weights_ignore_none_brier_ema():
    stats = [
        _stats_row("unrated", 10, None),
        _stats_row("rated", 10, 0.20),
    ]
    weights = weights_from_model_stats(stats)
    assert weights is not None
    assert "unrated" not in weights
    assert set(weights) == {"rated"}


def test_weights_ignore_zero_predictions():
    stats = [
        _stats_row("newcomer", 0, 0.20),
        _stats_row("veteran", 50, 0.20),
    ]
    weights = weights_from_model_stats(stats)
    assert weights is not None
    assert set(weights) == {"veteran"}


def test_weights_all_invalid_returns_none():
    assert weights_from_model_stats([]) is None
    assert weights_from_model_stats([_stats_row("m", 10, None)]) is None
    assert weights_from_model_stats([_stats_row("m", 0, 0.20)]) is None


def test_weights_sum_to_one():
    stats = [
        _stats_row("a", 10, 0.10),
        _stats_row("b", 20, 0.25),
        _stats_row("c", 5, 0.40),
    ]
    weights = weights_from_model_stats(stats)
    assert weights is not None
    assert sum(weights.values()) == pytest.approx(1.0)


def test_ensemble_uses_weights_from_model_stats():
    stats = [
        _stats_row("good", 10, 0.10),
        _stats_row("bad", 10, 0.30),
    ]
    weights = weights_from_model_stats(stats)
    assert weights is not None
    runs = [ForecastResult(0.4, "a", "good"), ForecastResult(0.8, "b", "bad")]
    # good 权重更高 → 加权中位数落在 0.4
    assert ensemble(runs, weights=weights) == pytest.approx(0.4)
