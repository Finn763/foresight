"""MarketResolver 单测（brief Step 1 4 例保留形态 + 补充用例）。

修复轮裁定：brief 自测与 Step-3 规则原文的 T+1 冲突 → 规则原文优先（自测时点错）。
1. 非 Asia/Shanghai 时区 → now < closes_at + 1 天（= closes 次日 09:00 北京）恒 None：
   事件（美股收盘）发生在 closes 次日凌晨（美股 16:00 ET = 次日 04:00 北京），
   开闸前判定只会用前一日收盘数据 → 错日错判永久入账。故美股/UTC 用例的判定时点
   统一取 closes 次日 16:30（开闸后）；专测门控的用例保留开闸前时点断言 None。
2. 双源用例的 spec 无 backup_symbol 字段，备源 symbol 由 SYMBOL_MAP[instrument][provider]
   推导（test_dual_source_disagree_degrades 意图：双源不一致 → 降级不猜）。

修复轮 2（T4 裁定）：双源都可用但价差 ≥ tolerance → 无条件降级 None（不做合理性检查）；
合理性兜底（含 sanity_exempt 免责）只保留在"备源获取失败 → 单源"路径。
"""

from datetime import datetime, timedelta

from predictor.resolution.market_resolver import MarketResolver
from predictor.resolution.quotes import QuoteError


class _FakeQuotes:
    """按 (provider, symbol) 注入价格表；昨收用 key ('prev', symbol)。"""

    def __init__(self, prices: dict[tuple[str, str], float], errors: set = None):
        self.prices = prices
        self.errors = errors or set()

    def __call__(self, provider, symbol):
        if (provider, symbol) in self.errors:
            raise QuoteError("boom")
        if symbol not in [s for p, s in self.prices if p != "prev"]:
            return self.prices[("prev", symbol)]  # prev 查询
        return self.prices[(provider, symbol)]


def _fake_prev(fetch):
    def f(provider, symbol):
        return fetch("prev", symbol)

    return f


def _spec(**kw) -> dict:
    base = {
        "class": "A",
        "instrument": "spx",
        "source_primary": "sina",
        "source_backup": "tencent",
        "condition": "gt_prev_close",
        "compare_symbol": "gb_$inx",
        "close_timezone": "America/New_York",
        "grace_days": 3,
        "degrade_to": "C",
    }
    base.update(kw)
    return base


def _q(closes_at: datetime):
    from predictor.data.storage import Question

    return Question(
        id=1,
        title="t",
        opens_at=closes_at - timedelta(days=1),
        closes_at=closes_at,
        outcome=None,
        resolved_at=None,
        is_public=True,
    )


# ---------- brief 权威 4 例 ----------
# 美股 spec 判定时点统一取 closes+1 天后（8-14 09:00 截止 → 8-15 09:00 开闸，8-15 16:30 判定）


def test_dual_source_agree():
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("tencent", "usINX"): 5498.0, ("prev", "gb_$inx"): 5400.0}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    out = r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 15, 16, 30))
    assert out == (True, "sina")  # 今收5500 > 昨收5400


def test_dual_source_disagree_degrades():
    # 差 2% → 双源不一致 → 无条件降级 None（T4 裁定：双源分歧不猜，不依赖昨收/合理性）
    fq = _FakeQuotes({("sina", "gb_$inx"): 5500.0, ("tencent", "usINX"): 5610.0})
    r = MarketResolver(fq, _fake_prev(fq))
    assert (
        r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 15, 16, 30)) is None
    )


def test_t_plus_1_confirmation_for_us_market():
    # 美股：closes 当日 16:30（closes+1 天 09:00 开闸前）→ T+1 门控拒绝 → None
    # 注：本 spec 无昨收数据 → None 由"gt_prev_close 缺昨收 → 无法判定"兜住
    fq = _FakeQuotes({("sina", "gb_$inx"): 5500.0, ("tencent", "usINX"): 5499.0})
    r = MarketResolver(fq, _fake_prev(fq))
    assert (
        r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 13, 16, 30)) is None
    )


def test_gt_threshold():
    spec = _spec(condition="gt_threshold", value=5000.0, compare_symbol="gc", instrument="gold")
    fq = _FakeQuotes({("sina", "gc"): 5100.0})
    r = MarketResolver(fq, _fake_prev(fq))
    out = r.resolve(_q(datetime(2026, 8, 14, 9, 0)), spec, datetime(2026, 8, 15, 16, 30))
    assert out == (True, "sina")


# ---------- 补充：T+1 门控 / 宽限窗口 ----------


def test_t_plus_1_blocks_morning_attempt():
    # 开闸前（closes 当日 10:00 < closes+1 天 09:00）→ 拒绝（事件未发生，数据未定）
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("tencent", "usINX"): 5498.0, ("prev", "gb_$inx"): 5400.0}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    assert r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 14, 10, 0)) is None


def test_us_market_none_until_closes_plus_1_day():
    # 语义护栏（防止 T+1 再被放宽）：美股题在 closes+1 天 09:00（北京）之前恒 None。
    # 事件（8-13 美股收盘）发生在 8-14 04:00 北京，此前判定只能用 8-12 收盘
    # 数据判 8-13 的题 → 错日错判永久入账。即使双源一致 + 昨收齐备也不得提前揭晓。
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("tencent", "usINX"): 5498.0, ("prev", "gb_$inx"): 5400.0}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    q = _q(datetime(2026, 8, 14, 9, 0))  # 标普次日题：closes 8-14 09:00 北京
    gate = q.closes_at + timedelta(days=1)  # 8-15 09:00 北京 = 开闸时点
    for probe in [
        datetime(2026, 8, 14, 9, 0),  # 截止当下
        datetime(2026, 8, 14, 16, 30),  # 当日 16:30（旧放宽门控点）
        datetime(2026, 8, 14, 23, 59),  # 当日深夜
        datetime(2026, 8, 15, 8, 59),
    ]:  # 开闸前 1 分钟
        assert r.resolve(q, _spec(), probe) is None, f"T+1 门控失效 @ {probe}"
    assert r.resolve(q, _spec(), gate) == (True, "sina")  # 开闸后正常判定


def test_before_deadline_returns_none():
    fq = _FakeQuotes({("sina", "gb_$inx"): 5500.0, ("prev", "gb_$inx"): 5400.0})
    r = MarketResolver(fq, _fake_prev(fq))
    assert r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 14, 8, 0)) is None


def test_grace_window_expired_degrades():
    # now 超 closes+3 天 → 宽限已过 → None（调用方降级）
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("tencent", "usINX"): 5498.0, ("prev", "gb_$inx"): 5400.0}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    assert r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 20, 10, 0)) is None


def test_window_upper_bound_blocks_wrong_day_retry():
    # 8-14 预演前对抗审计 P1（#67 场景）：closes 8-13 09:00，8-15 16:30 宽限内但
    # 数据窗口已过 → 拒绝判定。此时快照是 8-14 收盘 vs 8-13 收盘，判"8-13 收 >
    # 8-12 收"的题必错日错判且永久入账——即使双源一致、昨收齐备也不得揭晓。
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("tencent", "usINX"): 5498.0, ("prev", "gb_$inx"): 5400.0}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    assert (
        r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 15, 16, 30)) is None
    )


def test_window_resolves_on_first_valid_day():
    # 8-14 16:30 = 首个合法判定时点（快照为 8-13 收盘冻结期，开盘 21:30 前不变）
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("tencent", "usINX"): 5498.0, ("prev", "gb_$inx"): 5400.0}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    assert r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30)) == (
        True,
        "sina",
    )


def test_asia_window_blocks_next_day_retry():
    # A 股：当日 16:30（收盘后快照期）可判定；次日 16:30 快照已滚动到次日行情 → 拒绝
    spec = _spec(
        close_timezone="Asia/Shanghai",
        compare_symbol="s_sh000001",
        instrument="sh000001",
        source_primary="tencent",
    )
    fq = _FakeQuotes({("tencent", "s_sh000001"): 4000.0, ("prev", "s_sh000001"): 3900.0})
    r = MarketResolver(fq, _fake_prev(fq))
    assert r.resolve(_q(datetime(2026, 8, 14, 9, 0)), spec, datetime(2026, 8, 14, 16, 30)) == (
        True,
        "tencent",
    )
    assert r.resolve(_q(datetime(2026, 8, 14, 9, 0)), spec, datetime(2026, 8, 15, 16, 30)) is None


def test_asia_close_no_t_plus_1():
    # A 股：Asia/Shanghai 无 T+1 门控，截止后即可判定
    spec = _spec(
        close_timezone="Asia/Shanghai",
        compare_symbol="s_sh000001",
        instrument="sh000001",
        source_primary="tencent",
    )
    fq = _FakeQuotes({("tencent", "s_sh000001"): 4000.0, ("prev", "s_sh000001"): 3900.0})
    r = MarketResolver(fq, _fake_prev(fq))
    out = r.resolve(_q(datetime(2026, 8, 14, 15, 30)), spec, datetime(2026, 8, 14, 15, 45))
    assert out == (True, "tencent")


# ---------- 补充：双源分歧 → 无条件降级；备源挂 → 合理性兜底 ----------


def test_disagree_degrades_to_none():
    # 双源差 2% → 无条件降级 None（T4 裁定：双源分歧 → 人工不猜，不做合理性检查；
    # 即使主源偏离昨收 0.9% ≤3% 也不采信）
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("tencent", "usINX"): 5610.0, ("prev", "gb_$inx"): 5450.0}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    assert (
        r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 15, 16, 30)) is None
    )


def test_disagree_sanity_drift_degrades():
    # 双源差 2% → 无条件降级 None（drift 无关——T4 裁定分歧不做合理性检查）
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("tencent", "usINX"): 5610.0, ("prev", "gb_$inx"): 5200.0}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    assert (
        r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 15, 16, 30)) is None
    )


def test_sanity_exempt_skips_drift_check():
    # 备源挂 → 单源 + 合理性：主源偏离昨收 5.8% >3%，但 spec 免责（sanity_exempt）→ 采信主源
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("prev", "gb_$inx"): 5200.0}, errors={("tencent", "usINX")}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    out = r.resolve(
        _q(datetime(2026, 8, 14, 9, 0)), _spec(sanity_exempt=True), datetime(2026, 8, 15, 16, 30)
    )
    assert out == (True, "sina")


def test_backup_provider_error_drift_exceeds_degrades():
    # 备源挂 → 单源 + 合理性：主源偏离昨收 5.8% >3% 且无免责 → 降级 None
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("prev", "gb_$inx"): 5200.0}, errors={("tencent", "usINX")}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    assert (
        r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 15, 16, 30)) is None
    )


def test_backup_provider_error_falls_to_single_source():
    # 备源挂（QuoteError）→ 单源继续，走合理性（昨收偏离 1.9% ≤3% → 采信主源）
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("prev", "gb_$inx"): 5400.0}, errors={("tencent", "usINX")}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    out = r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 15, 16, 30))
    assert out == (True, "sina")


def test_backup_symbol_from_spec_overrides_map():
    # spec.backup_symbol 优先于 SYMBOL_MAP 推导（USINX2 一致 → 采信主源；
    # 若错误使用 map 的 usINX 则 2% 分歧 → 无昨收 → None）
    spec = _spec(condition="gt_threshold", value=5000.0, backup_symbol="USINX2")
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("tencent", "USINX2"): 5498.0, ("tencent", "usINX"): 5610.0}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    out = r.resolve(_q(datetime(2026, 8, 14, 9, 0)), spec, datetime(2026, 8, 15, 16, 30))
    assert out == (True, "sina")


def test_no_backup_provider_single_source():
    # 无 source_backup 的 spec（btc 双固定 URL 源）：单源直接判定
    spec = _spec(
        instrument="btc",
        compare_symbol="btc",
        source_primary="coingecko",
        source_backup="coinbase",
        condition="gt_threshold",
        value=50000.0,
        close_timezone="UTC",
    )
    fq = _FakeQuotes({("coingecko", "btc"): 63760.0})
    r = MarketResolver(fq, _fake_prev(fq))
    out = r.resolve(_q(datetime(2026, 8, 14, 9, 0)), spec, datetime(2026, 8, 15, 16, 30))
    assert out == (True, "coingecko")


# ---------- 补充：条件判定与异常 ----------


def test_lt_threshold():
    spec = _spec(condition="lt_threshold", value=6000.0, compare_symbol="gc", instrument="gold")
    fq = _FakeQuotes({("sina", "gc"): 5100.0})
    r = MarketResolver(fq, _fake_prev(fq))
    out = r.resolve(_q(datetime(2026, 8, 14, 9, 0)), spec, datetime(2026, 8, 15, 16, 30))
    assert out == (True, "sina")


def test_threshold_false_side():
    spec = _spec(condition="gt_threshold", value=6000.0, compare_symbol="gc", instrument="gold")
    fq = _FakeQuotes({("sina", "gc"): 5100.0})
    r = MarketResolver(fq, _fake_prev(fq))
    out = r.resolve(_q(datetime(2026, 8, 14, 9, 0)), spec, datetime(2026, 8, 15, 16, 30))
    assert out == (False, "sina")


def test_gt_prev_close_lower_returns_false():
    # 今收 5300 < 昨收 5400 → False
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5300.0, ("tencent", "usINX"): 5298.0, ("prev", "gb_$inx"): 5400.0}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    out = r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 15, 16, 30))
    assert out == (False, "sina")


def test_unknown_condition_returns_none():
    spec = _spec(condition="moon_alignment")
    fq = _FakeQuotes({("sina", "gb_$inx"): 5500.0, ("prev", "gb_$inx"): 5400.0})
    r = MarketResolver(fq, _fake_prev(fq))
    assert r.resolve(_q(datetime(2026, 8, 14, 9, 0)), spec, datetime(2026, 8, 15, 16, 30)) is None


def test_primary_fetch_error_degrades():
    fq = _FakeQuotes(
        {("sina", "gb_$inx"): 5500.0, ("prev", "gb_$inx"): 5400.0}, errors={("sina", "gb_$inx")}
    )
    r = MarketResolver(fq, _fake_prev(fq))
    assert (
        r.resolve(_q(datetime(2026, 8, 14, 9, 0)), _spec(), datetime(2026, 8, 15, 16, 30)) is None
    )


# ---------- record_high（创新高：窗口内收盘最大值 > 窗口前历史最高收盘） ----------


def _rh_question():
    from predictor.data.storage import Question

    return Question(
        id=99,
        title="未来 7 天内标普 500 会创新高吗",
        opens_at=datetime(2026, 8, 1, 9, 0),
        closes_at=datetime(2026, 8, 14, 9, 0),
        outcome=None,
        resolved_at=None,
        is_public=True,
    )


def _rh_spec() -> dict:
    return _spec(
        condition="record_high",
        source_primary="tencent",
        compare_symbol="usINX",
        source_backup="yahoo",
        backup_symbol="^GSPC",
    )


def _fake_kline(bars, errors: set = None):
    """bars: [(date, close)]；errors: 挂掉的 (provider, symbol)。"""

    def f(provider, symbol):
        if (provider, symbol) in (errors or set()):
            raise QuoteError("kline boom")
        return list(bars)

    return f


def _d(day: int):
    from datetime import date

    return date(2026, 8, day)


def _jd(day: int):
    from datetime import date

    return date(2026, 7, day)


def test_record_high_true():
    # 窗口 [8-1, 8-14]：max 7740 > 窗口前 max 7710 → True
    bars = [(_jd(27), 7700.0), (_jd(31), 7710.0)] + [
        (_d(14), 7730.0), (_d(13), 7740.0), (_d(1), 7690.0)
    ]
    r = MarketResolver(_FakeQuotes({}), None, fetch_kline=_fake_kline(bars))
    out = r.resolve(_rh_question(), _rh_spec(), datetime(2026, 8, 15, 16, 30))
    assert out == (True, "tencent")


def test_record_high_false():
    # 窗口 max 7705 < 前高 7710 → False
    bars = [(_jd(27), 7700.0), (_jd(31), 7710.0)] + [(_d(14), 7705.0), (_d(1), 7690.0)]
    r = MarketResolver(_FakeQuotes({}), None, fetch_kline=_fake_kline(bars))
    out = r.resolve(_rh_question(), _rh_spec(), datetime(2026, 8, 15, 16, 30))
    assert out == (False, "tencent")


def test_record_high_incomplete_window_degrades():
    # K 线最后 bar 8-13 < 窗口末 8-14 → 数据未齐 → None（宽限内重试）
    bars = [(_jd(27), 7700.0), (_jd(31), 7710.0)] + [(_d(13), 7740.0)]
    r = MarketResolver(_FakeQuotes({}), None, fetch_kline=_fake_kline(bars))
    assert r.resolve(_rh_question(), _rh_spec(), datetime(2026, 8, 15, 16, 30)) is None


def test_record_high_no_prior_bars_degrades():
    # 全部 bar 都在窗口内 → 无前高对照 → None（不猜）
    bars = [(_d(14), 7730.0), (_d(1), 7690.0)]
    r = MarketResolver(_FakeQuotes({}), None, fetch_kline=_fake_kline(bars))
    assert r.resolve(_rh_question(), _rh_spec(), datetime(2026, 8, 15, 16, 30)) is None


def test_record_high_truncated_prior_degrades():
    # 前高出现在 K 线最早 bar（历史截断风险）→ None（不猜）
    bars = [(_jd(27), 7710.0)] + [(_d(14), 7705.0), (_d(1), 7690.0)]
    r = MarketResolver(_FakeQuotes({}), None, fetch_kline=_fake_kline(bars))
    assert r.resolve(_rh_question(), _rh_spec(), datetime(2026, 8, 15, 16, 30)) is None


def test_record_high_dual_source_disagree_degrades():
    # 主源判 True、备源判 False → 分歧降级（不猜）
    bars_p = [(_jd(27), 7700.0), (_jd(31), 7710.0), (_d(14), 7740.0), (_d(1), 7690.0)]
    bars_b = [(_jd(27), 7700.0), (_jd(31), 7730.0), (_d(14), 7715.0), (_d(1), 7690.0)]
    kl = {(("tencent", "usINX")): bars_p, ("yahoo", "^GSPC"): bars_b}

    def f(provider, symbol):
        return list(kl[(provider, symbol)])

    r = MarketResolver(_FakeQuotes({}), None, fetch_kline=f)
    assert r.resolve(_rh_question(), _rh_spec(), datetime(2026, 8, 15, 16, 30)) is None


def test_record_high_backup_fail_knife_edge_degrades():
    # 备源挂 + 单源 margin 0.1% < 0.2% → 刀口不猜 None
    bars = [(_jd(27), 7700.0), (_jd(31), 7710.0), (_d(14), 7717.7), (_d(1), 7690.0)]
    r = MarketResolver(
        _FakeQuotes({}), None, fetch_kline=_fake_kline(bars, errors={("yahoo", "^GSPC")})
    )
    assert r.resolve(_rh_question(), _rh_spec(), datetime(2026, 8, 15, 16, 30)) is None


def test_record_high_backup_fail_margin_ok_judges():
    # 备源挂 + 单源 margin 0.4% ≥ 0.2% → 判 True
    bars = [(_jd(27), 7700.0), (_jd(31), 7710.0), (_d(14), 7740.0), (_d(1), 7690.0)]
    r = MarketResolver(
        _FakeQuotes({}), None, fetch_kline=_fake_kline(bars, errors={("yahoo", "^GSPC")})
    )
    out = r.resolve(_rh_question(), _rh_spec(), datetime(2026, 8, 15, 16, 30))
    assert out == (True, "tencent")
