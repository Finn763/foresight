import pytest

from predictor.calibration.postprocess import clamp, extremize


def test_extremize_and_clamp():
    assert extremize(0.6) == pytest.approx(0.63)
    assert clamp(1.5) == 0.99
    assert clamp(-1.0) == 0.01


def test_extremize_pulls_away_from_0_5():
    # 外推方向：低于 0.5 往 0 走，高于 0.5 往 1 走
    assert extremize(0.3, alpha=0.5) == pytest.approx(0.2)
    assert extremize(0.3, alpha=0.5) < 0.3
    assert extremize(0.7, alpha=0.5) == pytest.approx(0.8)
    assert extremize(0.7, alpha=0.5) > 0.7


def test_extremize_zero_alpha_is_identity():
    assert extremize(0.42, alpha=0.0) == pytest.approx(0.42)
