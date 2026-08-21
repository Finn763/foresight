import math

import pytest

from predictor.calibration.scoring import brier, log_score


def test_brier():
    assert brier(1.0, True) == 0.0
    assert brier(0.5, True) == 0.25
    assert brier(0.0, False) == 0.0
    assert brier(0.3, False) == 0.09


def test_log_score_perfect_and_wrong():
    assert log_score(0.9, True) == pytest.approx(-math.log(0.9))
    assert log_score(0.9, False) == pytest.approx(-math.log(0.1))


def test_log_score_clamps_extremes():
    # 0/1 被 clamp 到 [1e-9, 1-1e-9]，不产生 inf
    assert math.isfinite(log_score(0.0, True))
    assert math.isfinite(log_score(1.0, False))
    assert log_score(0.0, True) == pytest.approx(-math.log(1e-9))
