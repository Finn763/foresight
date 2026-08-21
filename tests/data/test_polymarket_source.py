"""Polymarket 数据源测试：拉取解析（MockTransport）+ 分档筛选 + 翻译降级。"""

from datetime import UTC, datetime, timedelta

import httpx

from predictor.data.polymarket_source import (
    fetch_event_markets,
    fetch_events,
    select_candidates,
    translate_title,
)


def _client(payload: list | dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _market(
    mid: str,
    *,
    days: float,
    volume: float = 100000.0,
    closed="False",
    end: str | None = None,
    question: str | None = None,
) -> dict:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    end = end or (now + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return {
        "id": mid,
        "slug": f"slug-{mid}",
        "question": question or f"Will Alpha win the thing {mid}?",
        "endDate": end,
        "closed": closed,
        "volume": str(volume),
        "outcomes": '["Yes", "No"]',
        "description": "resolves to Yes if ...",
    }


def test_fetch_events_parses_list():
    client = _client([{"id": "1"}, {"id": "2"}])
    assert [e["id"] for e in fetch_events(client)] == ["1", "2"]


def test_fetch_event_markets_extracts_markets():
    client = _client({"markets": [{"id": "m1"}]})
    assert [m["id"] for m in fetch_event_markets(client, "ev1")] == ["m1"]


def test_select_candidates_tier_split_and_volume_sort():
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    markets = [
        _market("s1", days=7, volume=80000, question="Will Alpha win?"),
        _market("s2", days=10, volume=90000, question="Will Beta win?"),
        _market("m1", days=30, volume=50000, question="Will Gamma win?"),
        _market("l1", days=60, volume=60000, question="Will Delta win?"),
        _market("far", days=120, volume=999999, question="Will Far win?"),  # 超 90 天排除
        _market("past", days=-5, volume=999999, question="Will Past win?"),  # 已过期排除
        _market("closed", days=5, volume=999999, closed="True", question="Will Closed win?"),
        _market("tiny", days=5, volume=100, question="Will Tiny win?"),  # volume 不足排除
    ]
    out = select_candidates(markets, now=now, per_tier=2, min_volume=5000)
    ids = [c.market_id for c in out]
    assert ids == ["s2", "s1", "m1", "l1"]  # 短档 volume 降序 + 中档 + 长档


def test_select_candidates_per_tier_cap():
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    markets = [
        _market(
            f"s{i}",
            days=5 + i,
            volume=10000 + i,
            question=f"Will Person {chr(65 + i)} win?",
        )
        for i in range(8)
    ]
    out = select_candidates(markets, now=now, per_tier=3, min_volume=0)
    assert len([c for c in out]) == 3  # 短档只取前 3


def test_select_candidates_closes_at_beijing_time():
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    end = "2026-08-25T00:00:00Z"  # UTC 零点 → 北京 08:00
    out = select_candidates([_market("t1", days=9, end=end)], now=now, per_tier=1)
    assert out[0].closes_at.hour == 8
    assert out[0].closes_at.tzinfo is None  # naive 北京时间（与题池口径一致）
    assert out[0].url == "https://polymarket.com/market/slug-t1"


def test_select_candidates_excludes_multi_outcome_markets():
    """多选市场（outcomes >2）二值预测无意义，必须排除。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    multi = _market("multi", days=5, volume=99999)
    multi["outcomes"] = '["A", "B", "C"]'
    bad = _market("badout", days=5, volume=99999)
    bad["outcomes"] = "not-json"
    ok = _market("ok", days=5, volume=99999)
    ok["outcomes"] = '["Yes", "No"]'
    out = select_candidates([multi, bad, ok], now=now, per_tier=5)
    assert [c.market_id for c in out] == ["ok"]


def test_select_candidates_bad_enddate_skipped():
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    out = select_candidates(
        [_market("bad", days=5, end="not-a-date"), _market("ok", days=5)],
        now=now,
        per_tier=5,
    )
    assert [c.market_id for c in out] == ["ok"]


def test_select_candidates_dedup_same_topic_keeps_earliest():
    """同主题（仅日期数字不同）只保留最早结束的一个，跨 event 也去重。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    m1 = _market("a", days=5, volume=90000)
    m2 = _market("b", days=9, volume=80000)
    for m, q in (
        (m1, "Will GPT-6 be released by August 21, 2026?"),
        (m2, "Will GPT-6 be released by August 31, 2026?"),
    ):
        m["_event_id"] = "ev-gpt"
        m["question"] = q
    m2["_event_id"] = "ev-gpt-v2"  # 跨 event 同主题仍要去重
    other = _market("c", days=6, volume=70000)
    other["_event_id"] = "ev-other"
    other["question"] = "Will Apple launch iPhone 18 this year?"
    out = select_candidates([m1, m2, other], now=now, per_tier=5, min_volume=0)
    ids = [c.market_id for c in out]
    assert "a" in ids and "b" not in ids  # 同主题只留最早（a 5 天 < b 9 天）
    assert "c" in ids


def test_select_candidates_dedup_keeps_threshold_family():
    """阈值类数字（$100k vs $150k）不是日期，不得被去重归一化误杀。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    lo = _market("lo", days=20, volume=90000, question="Will BTC hit $100k in 2026?")
    hi = _market("hi", days=20, volume=80000, question="Will BTC hit $150k in 2026?")
    out = select_candidates([lo, hi], now=now, per_tier=5, min_volume=0)
    assert {c.market_id for c in out} == {"lo", "hi"}


class _FakeLLM:
    def __init__(self, title: str | None = None, fail: bool = False):
        self._title = title
        self._fail = fail

    def chat_json(self, messages):
        if self._fail:
            raise RuntimeError("boom")
        if self._title is None:
            return {}
        return {"title": self._title}


def test_translate_title_ok():
    assert translate_title(_FakeLLM(title="苹果会在9月发布新品吗"), "Will Apple...?") == (
        "苹果会在9月发布新品吗"
    )


def test_translate_title_fallback_on_failure():
    assert translate_title(_FakeLLM(fail=True), "Will Apple...?") == "Will Apple...?"
    assert translate_title(_FakeLLM(title=None), "Will Apple...?") == "Will Apple...?"


def test_market_outcome_parses_resolution():
    import importlib.util

    spec = importlib.util.spec_from_file_location("pm_resolve", "scripts/pm_resolve.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    market_outcome = mod.market_outcome

    def client(prices, ok=True):
        def handler(request):
            if not ok:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"outcomePrices": prices})

        return httpx.Client(transport=httpx.MockTransport(handler))

    assert market_outcome(client(["1", "0"]), "m") is True
    assert market_outcome(client(["0", "1"]), "m") is False
    assert market_outcome(client([1.0, 0.0]), "m") is True  # 浮点决议值
    assert market_outcome(client(["0.5", "0.5"]), "m") is None  # 未决议
    assert market_outcome(client([], ok=False), "m") is None  # 网络失败降级

    def client_with_outcomes(prices, outcomes):
        def handler(request):
            return httpx.Response(200, json={"outcomePrices": prices, "outcomes": outcomes})

        return httpx.Client(transport=httpx.MockTransport(handler))

    # outcomes 顺序 ["No","Yes"]：index0=No 获胜 → 揭晓应为 False（防反转）
    assert market_outcome(client_with_outcomes(["1", "0"], '["No", "Yes"]'), "m") is False
    assert market_outcome(client_with_outcomes(["0", "1"], '["No", "Yes"]'), "m") is True
    assert market_outcome(client_with_outcomes(["1", "0"], '["Yes", "No"]'), "m") is True


def test_should_fallback_window_boundary():
    """市场独占窗口：closes 后 <3 天不兜底，≥3 天允许兜底（边界恰第 3 天）。"""
    import importlib.util
    from datetime import timedelta

    spec = importlib.util.spec_from_file_location("pm_resolve", "scripts/pm_resolve.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    should_fallback = mod.should_fallback

    closes = datetime(2026, 8, 15, 9, 0)
    assert should_fallback(closes + timedelta(days=2, hours=23), closes) is False
    assert should_fallback(closes + timedelta(days=3), closes) is True
    assert should_fallback(closes + timedelta(days=10), closes) is True
    assert should_fallback(closes - timedelta(days=1), closes) is False  # 未到期
