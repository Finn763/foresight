"""回测统计：均值/标准差/95%CI；零样本提示词命中 JSON；常数基线臂。"""

import math
from datetime import UTC, datetime

import pytest

from predictor.data.benchmarks import BenchQuestion
from predictor.eval.backtest import BacktestReport, constant_baseline_brier, run_zero_shot_backtest


def _q(title: str, outcome: bool) -> BenchQuestion:
    return BenchQuestion(
        id=title,
        title=title,
        closes_at=datetime(2026, 12, 1, tzinfo=UTC),
        resolved=True,
        outcome=outcome,
        category="x",
    )


class FakeClient:
    """永远报 0.9 的假 LLM（用于统计链路验证）。"""

    def chat_json(self, messages, **kw):
        return {"probability": 0.9, "rationale": "base rate"}


def test_report_math():
    questions = [_q("a", True), _q("b", False), _q("c", True)]
    rep = run_zero_shot_backtest(FakeClient(), questions)
    assert isinstance(rep, BacktestReport)
    assert rep.n == 3
    # Brier: 0.9 vs [1,0,1] → 0.01, 0.81, 0.01 → mean 0.2767, sd>0
    assert rep.brier_mean == pytest.approx((0.01 + 0.81 + 0.01) / 3)
    assert rep.ci95_low < rep.brier_mean < rep.ci95_high


def test_constant_baseline_none_fallback_to_half():
    rep = constant_baseline_brier([(None, True)])
    assert rep["n"] == 1
    assert rep["brier_mean"] == pytest.approx(0.25)


def test_constant_baseline_mean():
    rep = constant_baseline_brier([(0.8, True), (0.2, False)])
    assert rep["n"] == 2
    assert rep["brier_mean"] == pytest.approx(((0.8 - 1) ** 2 + (0.2 - 0) ** 2) / 2)


def test_constant_baseline_empty_is_nan():
    rep = constant_baseline_brier([])
    assert rep["n"] == 0
    assert math.isnan(rep["brier_mean"])
