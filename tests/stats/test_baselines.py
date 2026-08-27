"""历史数据层 + 统计基线测试。

用合成序列验证算法（真实 API 拉取已在主 agent 手工验证：标普/汇率日线 2016 至今可用）。
"""

import random
from datetime import datetime, timedelta

import pytest

from predictor.stats.baselines import (
    baseline_breakout,
    baseline_cny_below,
    baseline_next_day_up,
    baseline_sp500_high,
    baseline_usdcnh_7,
    compute_baseline,
)
from predictor.stats.historical import build_series_context, fetch_series_map


def _gen_series(
    n: int = 2600, start: float = 2000.0, drift: float = 0.0004, vol: float = 0.01, seed: int = 42
) -> list[dict]:
    """合成日线：几何随机游走，2016-01-01 起（约 2600 交易日 = 10 年）。"""
    rng = random.Random(seed)
    rows = []
    price = start
    d = datetime(2016, 1, 1)
    step = __import__("datetime").timedelta(days=1)
    while len(rows) < n:
        if d.weekday() < 5:
            price = price * (1 + drift + rng.gauss(0, vol))
            rows.append(
                {
                    "date": d.strftime("%Y-%m-%d"),
                    "open": price * 0.999,
                    "close": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                }
            )
        d += step
    return rows[:n]


def test_fetch_series_map_failure_returns_empty(monkeypatch):
    """网络失败 → 空 dict，不抛异常（调用方降级路径）。"""
    from predictor.stats import historical

    def boom(*a, **kw):
        raise RuntimeError("net down")

    monkeypatch.setattr(historical, "fetch_series", boom)
    monkeypatch.setattr(historical, "fetch_fred_series", boom)
    monkeypatch.setattr(historical, "fetch_eia_series", boom)
    assert fetch_series_map() == {
        "sp500": [],
        "usdcnh": [],
        "gold": [],
        "brent": [],
        "shanghai": [],
        "dow": [],
        "cpi_cn": [],
        "ffr": [],
        "wti_price": [],
        "wti_stock": [],
    }


def test_build_series_context_shape():
    rows = _gen_series(600)
    ctx = build_series_context({"sp500": rows, "usdcnh": rows})
    assert "标普500" in ctx
    assert "最新收盘" in ctx
    assert "历史最高" in ctx
    assert "近 5 年" in ctx


def test_baseline_sp500_high_in_range():
    """漂移市场（缓慢上涨）里"未来 7 天创新高"频率应显著 > 0 且 < 1。"""
    rows = _gen_series(drift=0.0004, vol=0.008, seed=7)
    b = baseline_sp500_high(rows, window_days=7)
    assert b is not None
    assert 0.0 < b["base_rate"] < 1.0
    assert b["n_obs"] > 100
    assert b["method"].startswith("历史滚动统计")


def test_baseline_longer_window_higher_rate():
    """窗口越长，创新高频率越高（单调性检查）。"""
    rows = _gen_series(drift=0.0002, vol=0.005, seed=9)
    b7 = baseline_sp500_high(rows, 7)
    b30 = baseline_sp500_high(rows, 30)
    assert b7 is not None and b30 is not None
    assert b30["base_rate"] >= b7["base_rate"]


def test_baseline_usdcnh_7():
    rows = _gen_series(start=6.5, drift=0.0001, vol=0.003, seed=11)  # 汇率从 6.5 缓升
    b = baseline_usdcnh_7(rows, 30)
    assert b is not None
    assert 0.0 <= b["base_rate"] <= 1.0


def test_compute_baseline_classify():
    rows = _gen_series(600)
    sm = {"sp500": rows, "usdcnh": rows}
    b = compute_baseline("未来 7 天内标普 500 会创新高吗", sm)
    assert b is not None and b["kind"] == "sp500_high" and b["window_days"] == 7
    # "升破"题由 cny_below（阈值参数化）接管；threshold=7.0 时与 usdcnh_7 数学等价
    b2 = compute_baseline("未来 30 天内人民币兑美元会升破 7.0 吗", sm)
    assert b2 is not None and b2["kind"] == "cny_below"
    assert b2["threshold"] == 7.0 and b2["window_days"] == 30
    # 题族真实标题（含"人民币兑美元"前缀）：必须先命中 cny_below 而非 usdcnh_7 的固定 7.0
    b2b = compute_baseline("未来30天内离岸人民币兑美元会升破6.90吗", sm)
    assert b2b is not None and b2b["kind"] == "cny_below" and b2b["threshold"] == 6.90
    # 无匹配类型 → None（不阻塞）
    assert compute_baseline("2026 年苹果秋季发布会会在 9 月 30 日前举行吗", sm) is None
    # 数据不足 → None
    assert compute_baseline("未来 7 天内标普 500 会创新高吗", {"sp500": [], "usdcnh": []}) is None


def _flat_rows(
    n: int,
    base: float = 100.0,
    high_fn=None,
    low_fn=None,
    close_fn=None,
) -> list[dict]:
    """确定性 rows：2020-01-01 起的工作日，各价格字段可传 callable(i) 定制。"""
    rows = []
    d = datetime(2020, 1, 1)
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "open": base,
                "close": close_fn(i) if close_fn else base,
                "high": high_fn(i) if high_fn else base,
                "low": low_fn(i) if low_fn else base,
            }
        )
        d += timedelta(days=1)
    return rows


def test_baseline_breakout_exact_rate():
    """high 分段：索引 ≥355 破阈值 → t≥350 的 245 个窗口破，t<350 的 250 个不破。"""
    threshold = 100.0
    rows = _flat_rows(600, high_fn=lambda i: threshold + 1 if i >= 355 else threshold - 1)
    b = baseline_breakout(rows, threshold=threshold, window_days=7)
    assert b is not None
    # window_days=7 → n_trade=5；t ∈ [100, 600-5)，共 495 个窗口
    assert b["base_rate"] == pytest.approx(245 / 495)
    assert b["n_obs"] == 495
    assert b["kind"] == "breakout"
    assert b["threshold"] == threshold
    assert b["window_days"] == 7
    assert b["method"].startswith("历史滚动统计")


def test_baseline_next_day_up_exact():
    """close 在 100/101 交替：101 行 → 100 对相邻，恰好 50 次上涨 → 0.5。"""
    rows = _flat_rows(101, close_fn=lambda i: 101.0 if i % 2 else 100.0)
    b = baseline_next_day_up(rows)
    assert b is not None
    assert b["base_rate"] == 0.5
    assert b["n_obs"] == 100
    assert b["kind"] == "next_day_up"


def test_baseline_cny_below_exact_rate():
    """low 分段：索引 ≥360 为 6.9（≤7.0）→ t≥339 的 240 个窗口破，其余 239 个不破。"""
    rows = _flat_rows(600, low_fn=lambda i: 6.9 if i >= 360 else 7.2)
    b = baseline_cny_below(rows, threshold=7.0, window_days=30)
    assert b is not None
    # window_days=30 → n_trade=21；t ∈ [100, 600-21)，共 479 个窗口
    assert b["base_rate"] == pytest.approx(240 / 479)
    assert b["n_obs"] == 479
    assert b["threshold"] == 7.0
    assert b["window_days"] == 30


def test_baseline_new_insufficient_data_returns_none():
    """样本不足 → None，不抛异常。"""
    assert baseline_breakout([], threshold=100.0, window_days=7) is None
    assert baseline_breakout(_flat_rows(499), threshold=100.0, window_days=7) is None
    assert baseline_cny_below([], threshold=7.0, window_days=30) is None
    assert baseline_cny_below(_flat_rows(499), threshold=7.0, window_days=30) is None
    assert baseline_next_day_up([]) is None
    assert baseline_next_day_up(_flat_rows(99)) is None
    assert baseline_next_day_up(_flat_rows(100)) is not None


def test_compute_baseline_new_kinds():
    """新题目类型分类：标题 → kind + 阈值/窗口提取。"""
    rows = _gen_series(600)
    sm = {
        "gold": rows,
        "brent": rows,
        "shanghai": rows,
        "dow": rows,
        "usdcnh": rows,
        "sp500": rows,
    }
    b = compute_baseline("未来7天内COMEX黄金会突破5150美元/盎司吗", sm)
    assert b is not None
    assert b["kind"] == "gold_break"
    assert b["threshold"] == 5150
    assert b["window_days"] == 7

    b2 = compute_baseline("未来30天内布伦特原油会突破90美元吗", sm)
    assert b2 is not None
    assert b2["kind"] == "brent_break"
    assert b2["threshold"] == 90
    assert b2["window_days"] == 30

    b3 = compute_baseline("上证指数明天会收涨吗", sm)
    assert b3 is not None and b3["kind"] == "shanghai_up"

    b4 = compute_baseline("道琼斯指数明天会上涨吗", sm)
    assert b4 is not None and b4["kind"] == "dow_up"
    # 反向标题（无"收涨/上涨"）不得命中 dow_up
    assert compute_baseline("道琼斯指数明天会下跌吗", sm) is None
    assert compute_baseline("未来30天内道琼斯会突破50000点吗", sm) is None

    b5 = compute_baseline("未来30天内人民币会升破6.95吗", sm)
    assert b5 is not None
    assert b5["kind"] == "cny_below"
    assert b5["threshold"] == 6.95
    assert b5["window_days"] == 30

    # 反向句式"美元兑人民币升破7.0"（汇率数值上涨）不得命中 cny_below（方向相反）
    b_rev = compute_baseline("美元兑人民币会升破7.0吗", sm)
    assert b_rev is None or b_rev["kind"] != "cny_below"

    # 序列缺失/样本不足 → None（降级，不阻塞）
    assert compute_baseline("未来7天内COMEX黄金会突破5150美元吗", {}) is None
    assert compute_baseline("上证指数明天会收涨吗", {"shanghai": []}) is None
    assert compute_baseline("未来30天内人民币会升破6.95吗", {}) is None


# ---------- CC §2.2 修复回归测试（2026-08-27） ----------

NOW = datetime(2026, 8, 27, 9, 0)


def _monthly_rows(closes: list[float]) -> list[dict]:
    """确定性月频序列：2020-01 起逐月一个观测（date/close/open/high/low）。"""
    rows = []
    y, m = 2020, 1
    for v in closes:
        rows.append(
            {
                "date": f"{y:04d}-{m:02d}-28",
                "open": v,
                "close": v,
                "high": v,
                "low": v,
            }
        )
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return rows


def test_cpi_requires_china():
    """CC §2.2 #1：「美国 CPI 同比」不得注入中国 CPI 序列；「中国 CPI」照常。"""
    closes = [100.0 + (i % 2) for i in range(36)]  # 100/101 交替 → 18/35 上升
    sm = {"cpi_cn": _monthly_rows(closes)}
    b = compute_baseline("中国08月 CPI 同比会高于上月吗", sm)
    assert b is not None and b["kind"] == "cpi_mom"
    assert b["base_rate"] == pytest.approx(18 / 35)
    # 美国 CPI 题（#54）→ None（原 bug：命中 cpi_mom 注入中国序列 0.486）
    assert compute_baseline("2026 年 9 月美国 CPI 同比会高于 8 月吗", sm) is None
    assert compute_baseline("美国 CPI 同比会超预期吗", sm) is None


def test_parse_title_window_units():
    from predictor.stats.baselines import _parse_title_window

    assert _parse_title_window("未来 7 天内标普 500 会创新高吗", NOW) == 7
    assert _parse_title_window("未来1个月内人民币会升破6.95吗", NOW) == 30
    assert _parse_title_window("未来2周内布伦特会突破90美元吗", NOW) == 14
    assert _parse_title_window("未来3个月内人民币会升破6.95吗", NOW) == 90
    assert _parse_title_window("未来1年内人民币会升破6.95吗", NOW) == 365


def test_parse_title_window_calendar():
    from predictor.stats.baselines import _parse_title_window

    # 年底：#6 口径 2026-12-31 - 2026-08-27 = 126 天
    assert _parse_title_window("2026 年底前人民币兑美元会升破 7.0 吗", NOW) == 126
    # 绝对日期：#64 口径 2027-01-31 - 2026-08-27 = 157 天
    assert _parse_title_window("2027 年 1 月 31 日前 标普 500 会首次站上 8500 点吗", NOW) == 157
    # 带年份的月底：#56 口径 2026-10-31 - 2026-08-27 = 65 天
    assert _parse_title_window("2026 年 10 月底前 布伦特原油价格会突破 95 美元/桶吗", NOW) == 65
    # 不带年份的月-日（未过 → 今年；已过 → 明年）
    assert _parse_title_window("9 月 30 日前会完成吗", NOW) == 34
    assert _parse_title_window("8 月 31 日前会完成吗", NOW) == 4
    assert _parse_title_window("7 月 1 日前会完成吗", NOW) == 308
    # 不带年份的月底 / 本月底
    assert _parse_title_window("10 月底前会完成吗", NOW) == 65
    assert _parse_title_window("月底前会完成吗", NOW) == 4
    # 无窗口措辞 → None
    assert _parse_title_window("美元兑人民币会升破7.0吗", NOW) is None


def test_window_over_90_days_returns_none():
    """CC §2.2 #2：月/年底/绝对日期口径 >90 天 → None（宁缺毋滥，不注入错频率）。"""
    rows = _gen_series(600)
    sm = {"sp500": rows, "usdcnh": rows}
    # #6：年底前升破 7.0（126 天）→ None（原 bug：注入 30 天窗口频率）
    b = compute_baseline(
        "2026 年底前人民币兑美元会升破 7.0 吗",
        sm,
        now=NOW,
        closes_at=datetime(2026, 12, 31, 9, 0),
    )
    assert b is None
    # #64：绝对日期 157 天 → None（原 bug：注入 7 天创新高频率）
    b = compute_baseline(
        "2027 年 1 月 31 日前 标普 500 会首次站上 8500 点吗",
        sm,
        now=NOW,
        closes_at=datetime(2027, 1, 29, 9, 0),
    )
    assert b is None
    # 题面无窗口措辞 → closes_at-now 兜底（30 天）
    b = compute_baseline(
        "美元兑人民币会升破7.0吗", sm, now=NOW, closes_at=NOW + timedelta(days=30)
    )
    assert b is not None and b["kind"] == "usdcnh_7" and b["window_days"] == 30
    # closes_at 兜底同样受 90 天上限约束
    b = compute_baseline(
        "美元兑人民币会升破7.0吗", sm, now=NOW, closes_at=NOW + timedelta(days=120)
    )
    assert b is None
    # 无窗口措辞且无 closes_at → None
    assert compute_baseline("美元兑人民币会升破7.0吗", sm, now=NOW) is None
    # 明确 30 天窗口不受 closes_at 影响（题面优先）
    b = compute_baseline(
        "未来30天内人民币兑美元会升破7.0吗",
        sm,
        now=NOW,
        closes_at=NOW + timedelta(days=60),
    )
    assert b is not None and b["kind"] == "cny_below" and b["window_days"] == 30


def test_ffr_three_way_direction():
    """CC §2.2 #3：加息/降息/维持三向分支；方向不明 → None；非美联储语境不注入 DFF。"""
    diffs = [0.0] * 20 + [0.25] * 6 + [-0.25] * 10  # hold=20 up=6 down=10, total=36
    vals = [2.0]
    for d in diffs:
        vals.append(vals[-1] + d)
    sm = {"ffr": _monthly_rows(vals)}
    # #72/#73/#74 类：加息 → 历史上调频率（原 bug：一律注入"维持" 0.764）
    b_up = compute_baseline("美联储9月会加息吗", sm)
    assert b_up is not None and b_up["kind"] == "ffr_meeting"
    assert b_up["base_rate"] == pytest.approx(6 / 36)
    assert "上调" in b_up["method"]
    b_down = compute_baseline("2026年9月FOMC会议会降息吗", sm)
    assert b_down["base_rate"] == pytest.approx(10 / 36)
    assert "下调" in b_down["method"]
    b_hold = compute_baseline("2026 年 9 月 FOMC 会议会维持利率不变吗", sm)
    assert b_hold["base_rate"] == pytest.approx(20 / 36)
    assert "维持" in b_hold["method"]
    # 方向不明（只提会议不提方向）→ None
    assert compute_baseline("美联储9月会议会宣布什么", sm) is None
    # 非美联储语境（某国央行降息）→ None（原 bug：`降息` 分支命中 ffr 注入 DFF）
    assert compute_baseline("中国人民银行会降息吗", sm) is None


def test_sp500_threshold_uses_breakout():
    """CC §2.2 #4：标普「站上/突破 N 点」走 breakout 算法，非创新高算法。"""
    threshold = 6500.0
    rows = _flat_rows(600, high_fn=lambda i: threshold + 1 if i >= 355 else threshold - 1)
    sm = {"sp500": rows}
    b = compute_baseline(
        "未来30天内标普500会站上6500点吗", sm, now=NOW, closes_at=NOW + timedelta(days=30)
    )
    assert b is not None and b["kind"] == "sp500_break"
    assert b["threshold"] == threshold
    assert b["window_days"] == 30
    # window_days=30 → n_trade=21；t ∈ [100, 579) 共 479 个窗口；t≥334（t+21≥355）的
    # 245 个窗口命中 high 突破
    assert b["base_rate"] == pytest.approx(245 / 479)
    # 真正的"创新高"题仍走 sp500_high（不受影响）
    b2 = compute_baseline("未来 7 天内标普 500 会创新高吗", {"sp500": _gen_series(600)}, now=NOW)
    assert b2 is not None and b2["kind"] == "sp500_high" and b2["window_days"] == 7
    # 裸"创新高"（非标普题，如淘宝成交额）不再映射到标普序列
    assert compute_baseline("2026 年淘宝双 11 全网成交额会创历史新高吗", sm, now=NOW) is None
