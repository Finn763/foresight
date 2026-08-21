"""auto_resolve 编排单测：真实 DuckDB(:memory:) + monkeypatch get_resolver。

get_resolver 被 monkeypatch 成假 resolver（不触网）；spec 损坏（JSON）走 degraded 不 crash。
"""

from datetime import datetime, timedelta

import pytest

from predictor.data.storage import Storage
from predictor.resolution import auto_resolve


@pytest.fixture()
def storage() -> Storage:
    st = Storage(":memory:")
    st.create_schema()
    return st


def _spec(**kw) -> dict:
    if "class_" in kw:  # class 是保留字，用 class_ 传入
        kw["class"] = kw.pop("class_")
    base = {
        "class": "A",
        "instrument": "spx",
        "source_primary": "sina",
        "source_backup": "tencent",
        "condition": "gt_threshold",
        "value": 5000.0,
        "compare_symbol": "gb_$inx",
        "close_timezone": "Asia/Shanghai",
        "grace_days": 3,
        "degrade_to": "C",
    }
    base.update(kw)
    return base


class _FakeResolver:
    """带 .resolve 接口的假 resolver（auto_resolve 调用 resolver.resolve(...)）。"""

    def __init__(self, outcome):
        self._outcome = outcome

    def resolve(self, q, spec, now):
        return self._outcome


def _due_question(storage, spec: dict | None, **kw) -> int:
    cls = spec.get("class") if spec else None
    return storage.add_question(
        "到期题",
        datetime.now() - timedelta(days=1),
        resolution_class=cls,
        resolution_spec=spec,
        **kw,
    )


def test_auto_resolve_resolves_class_a(storage, monkeypatch):
    qid = _due_question(storage, _spec())
    monkeypatch.setattr(
        auto_resolve, "get_resolver", lambda cls, storage=None: _FakeResolver((True, "sina"))
    )
    stats = auto_resolve.auto_resolve(storage, now=datetime.now())
    assert stats == {"resolved": 1, "degraded": 0, "pending": 0}
    q = storage.get_question(qid)
    assert q.outcome is True
    events = storage._conn.execute("SELECT event_type FROM evolution_log").fetchall()
    assert [e[0] for e in events] == ["resolution_ok"]


def test_auto_resolve_class_c_is_pending(storage, monkeypatch):
    qid = _due_question(storage, _spec(class_="C"))
    monkeypatch.setattr(
        auto_resolve, "get_resolver", lambda cls, storage=None: _FakeResolver((True, "sina"))
    )
    stats = auto_resolve.auto_resolve(storage, now=datetime.now())
    assert stats == {"resolved": 0, "degraded": 0, "pending": 1}
    assert storage.get_question(qid).outcome is None


def test_auto_resolve_class_b_is_pending(storage, monkeypatch):
    qid = _due_question(storage, _spec(class_="B"))
    monkeypatch.setattr(auto_resolve, "get_resolver", lambda cls, storage=None: None)
    stats = auto_resolve.auto_resolve(storage, now=datetime.now())
    assert stats == {"resolved": 0, "degraded": 0, "pending": 1}
    assert storage.get_question(qid).outcome is None


def test_auto_resolve_no_spec_is_pending(storage, monkeypatch):
    qid = _due_question(storage, None)
    monkeypatch.setattr(
        auto_resolve, "get_resolver", lambda cls, storage=None: _FakeResolver((True, "sina"))
    )
    stats = auto_resolve.auto_resolve(storage, now=datetime.now())
    assert stats == {"resolved": 0, "degraded": 0, "pending": 1}
    assert storage.get_question(qid).outcome is None


def test_auto_resolve_polymarket_source_skipped_and_logged(storage, monkeypatch):
    """Polymarket 题交 pm_resolve 混合揭晓：16:30 轮必须显式跳过并记日志，
    不得被 LLMResolver 在独占窗口内抢先揭晓（哪怕将来 spec 补了 class）。"""
    qid = _due_question(
        storage,
        {"source": "polymarket", "market_id": "123", "class": "B"},  # 模拟未来补 class
    )
    monkeypatch.setattr(
        auto_resolve, "get_resolver", lambda cls, storage=None: _FakeResolver((True, "llm"))
    )
    stats = auto_resolve.auto_resolve(storage, now=datetime.now())
    assert stats == {"resolved": 0, "degraded": 0, "pending": 1}
    assert storage.get_question(qid).outcome is None  # 未被抢先揭晓
    events = storage._conn.execute("SELECT event_type FROM evolution_log").fetchall()
    assert [e[0] for e in events] == ["resolution_skipped_polymarket"]


def test_auto_resolve_resolver_none_degrades_and_logs(storage, monkeypatch):
    qid = _due_question(storage, _spec())
    monkeypatch.setattr(auto_resolve, "get_resolver", lambda cls, storage=None: _FakeResolver(None))
    stats = auto_resolve.auto_resolve(storage, now=datetime.now())
    assert stats == {"resolved": 0, "degraded": 1, "pending": 0}
    assert storage.get_question(qid).outcome is None
    events = storage._conn.execute("SELECT event_type FROM evolution_log").fetchall()
    assert [e[0] for e in events] == ["resolution_failed"]


def test_auto_resolve_broken_spec_json_degrades_not_crash(storage, monkeypatch):
    # resolution_spec 解析失败（JSON 损坏）→ 计 degraded，整轮继续不 crash。
    # 注：DuckDB JSON 列在写入时即校验，坏 JSON 进不了库（ConversionException）；
    # 损坏只可能来自存量 TEXT 迁移或 schema 漂移 → 在 storage 边界模拟 json.loads 抛错。
    qid = _due_question(storage, _spec())
    qid2 = _due_question(storage, _spec())
    orig = storage.question_resolution

    def broken(question_id):
        if question_id == qid:
            raise ValueError("malformed spec JSON")
        return orig(question_id)

    monkeypatch.setattr(storage, "question_resolution", broken)
    monkeypatch.setattr(
        auto_resolve, "get_resolver", lambda cls, storage=None: _FakeResolver((True, "sina"))
    )
    stats = auto_resolve.auto_resolve(storage, now=datetime.now())
    assert stats == {"resolved": 1, "degraded": 1, "pending": 0}
    assert storage.get_question(qid).outcome is None
    assert storage.get_question(qid2).outcome is True
    events = storage._conn.execute("SELECT event_type FROM evolution_log").fetchall()
    assert sorted(e[0] for e in events) == ["resolution_failed", "resolution_ok"]


def test_auto_resolve_skips_not_due(storage, monkeypatch):
    storage.add_question(
        "未到期题",
        datetime.now() + timedelta(days=5),
        resolution_class="A",
        resolution_spec=_spec(),
    )
    monkeypatch.setattr(
        auto_resolve, "get_resolver", lambda cls, storage=None: _FakeResolver((True, "sina"))
    )
    stats = auto_resolve.auto_resolve(storage, now=datetime.now())
    assert stats == {"resolved": 0, "degraded": 0, "pending": 0}


def test_auto_resolve_class_b_uses_llm_resolver_and_merges_extra(storage, monkeypatch):
    """B 类走 LLMResolver：3-tuple 解包入账，resolution_ok detail 合并 confidence/citations。"""
    qid = _due_question(storage, _spec(class_="B"))
    monkeypatch.setattr(
        auto_resolve,
        "get_resolver",
        lambda cls, storage=None: _FakeResolver(
            (True, "llm_websearch", {"confidence": 0.88, "citations": ["https://a/1"]})
        ),
    )
    stats = auto_resolve.auto_resolve(storage, now=datetime.now())
    assert stats == {"resolved": 1, "degraded": 0, "pending": 0}
    assert storage.get_question(qid).outcome is True
    detail = storage._conn.execute(
        "SELECT detail FROM evolution_log WHERE event_type='resolution_ok'"
    ).fetchone()[0]
    assert '"source": "llm_websearch"' in detail and "https://a/1" in detail
