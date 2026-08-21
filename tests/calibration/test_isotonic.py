import pytest

from predictor.calibration.isotonic import fit_isotonic


def test_isotonic_monotonic():
    cal = fit_isotonic([0.1, 0.3, 0.3, 0.9], [False, True, False, True])
    ps = [cal.apply(p) for p in (0.1, 0.2, 0.3, 0.5, 0.9)]
    assert ps == sorted(ps)  # 单调不减
    assert all(0.0 <= p <= 1.0 for p in ps)


def test_isotonic_identity_on_perfect_data():
    cal = fit_isotonic([0.2, 0.5, 0.8], [False, True, True])
    assert cal.apply(0.2) == pytest.approx(0.0)
    assert cal.apply(0.8) == pytest.approx(1.0)


def test_isotonic_handles_out_of_range_input():
    cal = fit_isotonic([0.1, 0.3, 0.9], [False, True, True])
    assert 0.0 <= cal.apply(-5.0) <= 1.0
    assert 0.0 <= cal.apply(5.0) <= 1.0


def test_isotonic_single_point():
    cal = fit_isotonic([0.5], [True])
    assert cal.apply(0.5) == pytest.approx(1.0)


def test_isotonic_duplicate_probability_points():
    # 重复概率点不同 outcome（同 x 不同块）：应输出组内频率 0.5，而非首个块的 0.0
    cal = fit_isotonic([0.5, 0.5], [False, True])
    assert cal.apply(0.5) == pytest.approx(0.5)
