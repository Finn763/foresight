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


def test_isotonic_tie_weighted_by_block_size():
    # 评审报告 §2.5 复现：4 个同分样本 1 真 → 频率 0.25。
    # 旧实现同 x 聚合每块贡献 2 个端点取简单均值 → 1/6 ≈ 0.1667（偏差 33%）。
    cal = fit_isotonic([0.5, 0.5, 0.5, 0.5], [False, False, True, False])
    assert cal.steps == [(0.5, pytest.approx(0.25))]
    assert cal.apply(0.5) == pytest.approx(0.25)


def test_isotonic_tie_weighted_across_multiple_blocks():
    # 同 x 多块且块大小不同：按块 n 加权 sum(y·n)/sum(n)。
    # PAVA 终态块 = [v=0(n=1), v=0(n=1), v=0.25(n=4), v=0.5(n=2)]
    #   → (0·1+0·1+0.25·4+0.5·2)/8 = 0.25（旧实现端点均值 = 0.1875）
    cal = fit_isotonic(
        [0.5] * 8,
        [False, False, True, False, False, False, True, False],
    )
    assert cal.steps == [(0.5, pytest.approx(0.25))]
    assert cal.apply(0.5) == pytest.approx(0.25)


def test_isotonic_tie_weighted_monotonicity():
    # 加权修复回归：同分跨块 + 多区间混合场景仍单调不减、值域 [0,1]。
    # 终态块 = [0.1:v0(n1), 0.5:v0(n1), 0.5:v0(n1), 0.5-0.9:v1/3(n3), 0.9:v1(n1), 0.9:v1(n1)]
    # 同 x 聚合：x=0.5 → (0·1+0·1+1/3·3)/5 = 0.2；x=0.9 → (1/3·3+1·1+1·1)/5 = 0.6
    cal = fit_isotonic(
        [0.1, 0.5, 0.5, 0.5, 0.5, 0.9, 0.9, 0.9],
        [False, False, False, True, False, False, True, True],
    )
    ps = [cal.apply(p) for p in (0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0)]
    assert ps == sorted(ps)  # 单调不减
    assert all(0.0 <= p <= 1.0 for p in ps)
    assert cal.steps == [
        (0.1, pytest.approx(0.0)),
        (0.5, pytest.approx(0.2)),
        (0.9, pytest.approx(0.6)),
    ]
