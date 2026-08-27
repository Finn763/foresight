"""Polymarket 数据源测试：拉取解析（MockTransport）+ 分档筛选 + 翻译降级。"""

import sys
from datetime import UTC, datetime, timedelta

import httpx

from predictor.data.polymarket_source import (
    fetch_event_markets,
    fetch_events,
    select_candidates,
    translate_title,
)
from predictor.data.storage import Storage


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


def _load_pm_resolve():
    import importlib.util

    spec = importlib.util.spec_from_file_location("pm_resolve", "scripts/pm_resolve.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_main_db_init_failure_returns_1(monkeypatch, capsys):
    """DB 撞锁（Storage 构造抛异常）→ 优雅 exit 1 + 可读消息，不裸 traceback。"""
    mod = _load_pm_resolve()

    def boom(db_path, **kw):
        raise RuntimeError("IO Error: database is locked")

    monkeypatch.setattr(mod, "Storage", boom)
    monkeypatch.setattr(sys, "argv", ["pm_resolve.py", "--db", "locked.db"])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "持锁" in out
    assert "Traceback" not in out


def test_main_llm_unavailable_does_not_block_market_and_rerun_idempotent(
    tmp_path, monkeypatch, capsys
):
    """LLM 客户端构造失败不阻断后续题市场决议；重跑不重复揭晓（幂等）。"""
    mod = _load_pm_resolve()
    db = tmp_path / "pm.db"
    st = Storage(str(db))
    st.create_schema()
    now = datetime.now()
    spec_pm = lambda mid: {"source": "polymarket", "market_id": mid}  # noqa: E731
    q1 = st.add_question("Q1", now - timedelta(days=1), resolution_spec=spec_pm("m1"))
    q2 = st.add_question("Q2", now - timedelta(days=10), resolution_spec=spec_pm("m2"))
    q3 = st.add_question("Q3", now - timedelta(days=1), resolution_spec=spec_pm("m3"))

    # m2 市场未决议且超独占窗口 → 走 LLM 兜底；m1/m3 市场已决议
    outcomes = {"m1": True, "m2": None, "m3": False}
    monkeypatch.setattr(mod, "market_outcome", lambda http, mid: outcomes.get(mid))

    def boom(**kw):
        raise RuntimeError("no api key in test")

    monkeypatch.setattr(mod, "LLMClient", boom)
    monkeypatch.setattr(sys, "argv", ["pm_resolve.py", "--db", str(db)])

    assert mod.main() == 0  # 首轮：m1/m3 市场决议，m2 兜底不可用跳过，整轮不崩
    st2 = Storage(str(db))
    assert st2.get_question(q1).outcome is True
    assert st2.get_question(q3).outcome is False
    assert st2.get_question(q2).outcome is None
    out = capsys.readouterr().out
    assert "LLM 兜底跳过" in out

    assert mod.main() == 0  # 重跑：已揭晓跳过、未揭晓继续等待 → 幂等，无重复回填
    assert st2.get_question(q1).outcome is True
    assert st2.get_question(q2).outcome is None
    assert st2.get_question(q3).outcome is False
    out2 = capsys.readouterr().out
    assert "本轮揭晓 0 题" in out2


def _load_pm_fetch():
    import importlib.util

    spec = importlib.util.spec_from_file_location("pm_fetch", "scripts/pm_fetch.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _pm_market(mid: str, *, days: float, volume: float = 100000.0) -> dict:
    """相对当前时刻构造一个可过 select_candidates 的活跃市场（endDate 动态）。"""
    now = datetime.now(UTC)
    end = (now + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return {
        "id": mid,
        "slug": f"slug-{mid}",
        "question": f"Will the {mid} market resolve Yes?",
        "endDate": end,
        "closed": "False",
        "volume": str(volume),
        "outcomes": '["Yes", "No"]',
        "description": "resolves to Yes if ...",
    }


def test_pm_fetch_main_db_init_failure_returns_1(monkeypatch, capsys):
    """DB 撞锁（Storage 构造抛异常）→ 优雅 exit 1 + 可读消息，不裸 traceback。"""
    mod = _load_pm_fetch()

    def boom(db_path, **kw):
        raise RuntimeError("IO Error: database is locked")

    monkeypatch.setattr(mod, "Storage", boom)
    monkeypatch.setattr(sys, "argv", ["pm_fetch.py", "--db", "locked.db"])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "持锁" in out
    assert "Traceback" not in out


def test_pm_fetch_main_network_failure_returns_1(tmp_path, monkeypatch, capsys):
    """事件列表网络失败 → 可读消息 + exit 1，不裸 traceback。"""
    mod = _load_pm_fetch()
    db = tmp_path / "pm.db"
    st = Storage(str(db))
    st.create_schema()

    def boom(http, limit=100, offset=0):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(mod, "fetch_events", boom)
    monkeypatch.setattr(sys, "argv", ["pm_fetch.py", "--db", str(db)])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "网络降级" in out
    assert "Traceback" not in out


def test_pm_fetch_main_ingest_and_rerun_idempotent(tmp_path, monkeypatch, capsys):
    """首轮入库 1 题；重跑 market_id 判重 → 0 新题入库（幂等）。"""
    mod = _load_pm_fetch()
    db = tmp_path / "pm.db"
    event = {"id": "ev1", "endDate": _pm_market("x", days=7)["endDate"]}
    market = _pm_market("m-idem-1", days=7)

    def fake_events(http, limit=100, offset=0):
        return [event] if offset == 0 else []

    def fake_markets(http, eid):
        return [market] if eid == "ev1" else []

    monkeypatch.setattr(mod, "fetch_events", fake_events)
    monkeypatch.setattr(mod, "fetch_event_markets", fake_markets)
    monkeypatch.setattr(
        sys, "argv", ["pm_fetch.py", "--db", str(db), "--no-translate"]
    )

    assert mod.main() == 0  # 首轮入库 1 题
    st2 = Storage(str(db))
    assert st2.source_market_ids("polymarket") == {"m-idem-1"}
    out = capsys.readouterr().out
    assert "本轮入库 1 题" in out

    assert mod.main() == 0  # 重跑：market_id 已入库 → 全部去重，不重复建题
    assert st2.source_market_ids("polymarket") == {"m-idem-1"}
    out2 = capsys.readouterr().out
    assert "去重后新增 0" in out2
    assert "本轮入库 0 题" in out2


def test_pm_fetch_main_add_failure_degrades_gracefully(monkeypatch, capsys):
    """入库撞锁（add_question 抛异常）→ 单题降级不击垮整轮，退出 0 不裸 traceback。"""
    mod = _load_pm_fetch()
    event = {"id": "ev1", "endDate": _pm_market("x", days=7)["endDate"]}
    market = _pm_market("m-lock-1", days=7)

    class FakeStorage:
        def __init__(self, db_path):
            pass

        def create_schema(self):
            pass

        def source_market_ids(self, source):
            return set()

        def add_question(self, *a, **kw):
            raise RuntimeError("IO Error: database is locked")

    def fake_events(http, limit=100, offset=0):
        return [event] if offset == 0 else []

    def fake_markets(http, eid):
        return [market] if eid == "ev1" else []

    monkeypatch.setattr(mod, "Storage", FakeStorage)
    monkeypatch.setattr(mod, "fetch_events", fake_events)
    monkeypatch.setattr(mod, "fetch_event_markets", fake_markets)
    monkeypatch.setattr(sys, "argv", ["pm_fetch.py", "--db", "x.db", "--no-translate"])

    assert mod.main() == 0  # 整轮跑完（降级不算失败），不裸 traceback
    out = capsys.readouterr().out
    assert "降级" in out
    assert "Traceback" not in out
    assert "本轮入库 0 题" in out
