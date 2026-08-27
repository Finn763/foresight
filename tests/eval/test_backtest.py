"""回测统计：均值/标准差/95%CI；零样本提示词命中 JSON；常数基线臂。
另覆盖 §2.1 修复：outcome=None 显式跳过+计数、require_outcome 防御、NaN 安全序列化。"""

import json
import math
from datetime import UTC, datetime

import pytest

from predictor.data.benchmarks import BenchQuestion
from predictor.eval.backtest import (
    BacktestReport,
    build_compare_report,
    constant_baseline_brier,
    json_safe,
    require_outcome,
    run_zero_shot_backtest,
)


def _q(title: str, outcome: bool | None) -> BenchQuestion:
    return BenchQuestion(
        id=title,
        title=title,
        closes_at=datetime(2026, 12, 1, tzinfo=UTC),
        resolved=outcome is not None,
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
    assert rep.skipped_outcome_missing == 0
    # Brier: 0.9 vs [1,0,1] → 0.01, 0.81, 0.01 → mean 0.2767, sd>0
    assert rep.brier_mean == pytest.approx((0.01 + 0.81 + 0.01) / 3)
    assert rep.ci95_low < rep.brier_mean < rep.ci95_high


def test_zero_shot_skips_none_outcome_and_counts():
    questions = [_q("a", None), _q("b", None), _q("c", True)]
    rep = run_zero_shot_backtest(FakeClient(), questions)
    assert rep.n == 1
    assert rep.skipped_outcome_missing == 2
    # 只对 outcome 已知的 c 计分：0.9 vs True
    assert rep.brier_mean == pytest.approx((0.9 - 1) ** 2)


def test_zero_shot_all_none_outcome_is_nan():
    rep = run_zero_shot_backtest(FakeClient(), [_q("a", None), _q("b", None)])
    assert rep.n == 0
    assert rep.skipped_outcome_missing == 2
    assert math.isnan(rep.brier_mean)


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


def test_require_outcome_rejects_none():
    # 禁止 None→False 静默转换：未揭晓题必须显式跳过
    with pytest.raises(ValueError, match="None"):
        require_outcome(None)


def test_require_outcome_passthrough():
    assert require_outcome(True) is True
    assert require_outcome(False) is False


def test_json_safe_replaces_nonfinite_floats_with_null():
    obj = {
        "brier_mean": float("nan"),
        "ci95": [float("nan"), float("-inf")],
        "n": 3,
        "finite": 0.2767,
        "nested": {"inf": float("inf")},
        "s": "x",
        "none": None,
    }
    out = json_safe(obj)
    assert out["brier_mean"] is None
    assert out["ci95"] == [None, None]
    assert out["n"] == 3
    assert out["finite"] == pytest.approx(0.2767)
    assert out["nested"] == {"inf": None}
    assert out["s"] == "x"
    assert out["none"] is None


def test_json_safe_output_is_valid_json_round_trip():
    report = {"brier_mean": float("nan"), "ci95": [float("nan"), float("nan")], "n": 0}
    dumped = json.dumps(json_safe(report), allow_nan=False)
    # json.loads 失败即证明修复前会写出非法 JSON 字面量（NaN）
    assert json.loads(dumped) == {"brier_mean": None, "ci95": [None, None], "n": 0}


def test_json_dumps_allow_nan_false_raises_on_nan():
    # 兜底语义：未清洗的非有限值必须炸出来，而不是静默产出坏 JSON
    with pytest.raises(ValueError):
        json.dumps({"x": float("nan")}, allow_nan=False)


def test_compare_backtest_report_counts_and_valid_json():
    zero = BacktestReport(
        n=0,
        brier_mean=float("nan"),
        brier_sd=float("nan"),
        ci95_low=float("nan"),
        ci95_high=float("nan"),
        baseline_name="zero-shot",
        skipped_outcome_missing=30,
    )
    report = build_compare_report(
        zero=zero,
        pipeline_briers=[],
        cb={"n": 0, "brier_mean": float("nan")},
        pipeline_skipped=30,
        baseline_skipped=0,
        baseline_outcome_skipped=30,
    )
    payload = json.dumps(json_safe(report), allow_nan=False)
    loaded = json.loads(payload)
    assert loaded["zero_shot"]["skipped_outcome_missing"] == 30
    assert loaded["pipeline"]["n"] == 0
    assert loaded["pipeline"]["skipped_outcome_missing"] == 30
    assert loaded["constant_base_rate"]["skipped_outcome_missing"] == 30
    assert loaded["constant_base_rate_skipped"] == 0
    # NaN 臂全部转 null
    assert loaded["zero_shot"]["brier_mean"] is None
    assert loaded["zero_shot"]["ci95"] == [None, None]
    assert loaded["pipeline"]["brier_mean"] is None
    assert loaded["constant_base_rate"]["brier_mean"] is None
    # 任一侧无样本 → 对比项显式 None
    assert loaded["delta_brier"] is None
    assert loaded["delta_brier_zero_shot_vs_base_rate"] is None
    assert loaded["delta_brier_pipeline_vs_base_rate"] is None


def test_compare_backtest_report_delta_with_samples():
    zero = BacktestReport(
        n=2,
        brier_mean=0.3,
        brier_sd=0.1,
        ci95_low=0.1,
        ci95_high=0.5,
        baseline_name="zero-shot",
        skipped_outcome_missing=0,
    )
    report = build_compare_report(
        zero=zero,
        pipeline_briers=[0.1, 0.2],
        cb={"n": 2, "brier_mean": 0.4},
        pipeline_skipped=0,
        baseline_skipped=0,
        baseline_outcome_skipped=0,
    )
    assert report["pipeline"]["brier_mean"] == pytest.approx(0.15)
    assert report["delta_brier"] == pytest.approx(0.3 - 0.15)
    assert report["delta_brier_zero_shot_vs_base_rate"] == pytest.approx(0.3 - 0.4)
    assert report["delta_brier_pipeline_vs_base_rate"] == pytest.approx(0.15 - 0.4)
