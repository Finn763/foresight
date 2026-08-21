"""LLMResolver 单测（fake client 注入，零网络）：四护栏/双采样/窗口/3-tuple 契约。"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from predictor.data.storage import Question, Storage
from predictor.llm.client import LLMError
from predictor.resolution.llm_resolver import LLMResolver


def _q(closes_at):
    return Question(
        id=68,
        title="明天北京最高气温会超过 35°C 吗",
        opens_at=closes_at - timedelta(days=1),
        closes_at=closes_at,
        outcome=None,
        resolved_at=None,
        is_public=True,
    )


def _ok_response(outcome, confidence, urls):
    return {
        "output": [
            {
                "type": "web_search_call",
                "id": "w1",
                "results": [{"url": u, "title": "t"} for u in urls],
            },
            {
                "type": "output_text",
                "id": "o1",
                "content": json.dumps(
                    {"outcome": outcome, "confidence": confidence, "citations": urls}
                ),
            },
        ]
    }


def _msg_response(outcome, confidence, citations, open_urls):
    """8-13 试点实测结构：最终答案在 type="message" 条目（顶层无 output_text），
    引用 = verdict JSON citations + action.open_page 的 url（action.search 的
    queries 不是引用）。"""
    items = [
        {
            "type": "web_search_call",
            "id": "call_search",
            "status": "completed",
            "action": {"type": "search", "queries": ["北京 2026年8月13日 最高气温 预报"]},
        },
    ]
    items += [
        {
            "type": "web_search_call",
            "id": f"call_open_{i}",
            "status": "completed",
            "action": {"type": "open_page", "url": u},
        }
        for i, u in enumerate(open_urls)
    ]
    items.append(
        {
            "type": "message",
            "id": "m1",
            "status": "completed",
            "role": "assistant",
            "phase": "final_answer",
            "content": [
                {
                    "type": "output_text",
                    "text": json.dumps(
                        {"outcome": outcome, "confidence": confidence, "citations": citations}
                    ),
                }
            ],
        }
    )
    return {"output": items}


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)  # 按采样顺序弹出
        self.calls = []

    def responses_create(self, **kw):
        self.calls.append(kw)
        if not self._responses:
            raise LLMError("no more responses")
        return self._responses.pop(0)


def _spec():
    return {"class": "B"}


def test_agree_returns_verdict_with_merged_citations():
    c = FakeClient(
        [
            _ok_response(False, 0.92, ["https://a/1", "https://a/2"]),
            _ok_response(False, 0.88, ["https://b/1", "https://a/2"]),
        ]
    )
    r = LLMResolver(c)
    out = r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30))
    assert out == (
        False,
        "llm_websearch",
        {"confidence": 0.88, "citations": ["https://a/1", "https://a/2", "https://b/1"]},
    )
    assert len(c.calls) == 2
    # prompt 锚定题面与截止时间（窗口语义：禁止拿当前状态判定）
    assert "35°C" in c.calls[0]["instructions"]
    assert "2026-08-13T09:00:00" in c.calls[0]["instructions"]
    assert c.calls[0]["tools"] == [{"type": "web_search"}]
    assert c.calls[0]["tool_choice"] == {"type": "web_search"}


def test_message_item_real_structure_resolves():
    # 8-13 试点实测结构回归：判定文本取 message 条目 content 的 output_text 块；
    # 引用三来源合并（verdict JSON citations 为主 + open_page url），
    # action.search 的 queries 不混入 citations。
    c = FakeClient(
        [
            _msg_response(
                False,
                0.98,
                [
                    "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
                    "https://www.bjmy.gov.cn/sy/tqyb/x.html",
                ],
                ["https://news.sina.cn/detail-1.html"],
            ),
            _msg_response(
                False,
                0.95,
                ["https://www.nmc.cn/publish/forecast/ABJ/beijing.html"],
                ["https://www.bjmy.gov.cn/sy/tqyb/x.html"],
            ),
        ]
    )
    r = LLMResolver(c)
    out = r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30))
    assert out[0] is False and out[1] == "llm_websearch"
    assert out[2]["confidence"] == 0.95
    assert "https://www.nmc.cn/publish/forecast/ABJ/beijing.html" in out[2]["citations"]
    assert "https://www.bjmy.gov.cn/sy/tqyb/x.html" in out[2]["citations"]
    assert "https://news.sina.cn/detail-1.html" in out[2]["citations"]  # open_page 来源
    assert all("2026年8月13日 最高气温" not in u for u in out[2]["citations"])  # queries 不是引用


def test_no_evidence_declines_and_logs():
    st = Storage(":memory:")
    st.create_schema()
    c = FakeClient(
        [
            {
                "output": [
                    {"type": "web_search_call", "id": "w1", "results": []},
                    {
                        "type": "output_text",
                        "id": "o1",
                        "content": json.dumps(
                            {"outcome": True, "confidence": 0.9, "citations": []}
                        ),
                    },
                ]
            },
            _ok_response(True, 0.9, ["https://a/1"]),
        ]
    )
    r = LLMResolver(c, storage=st)
    assert (
        r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30)) is None
    )
    evs = st._conn.execute("SELECT event_type, detail FROM evolution_log").fetchall()
    assert evs[0][0] == "llm_resolve_failed" and "no_evidence" in evs[0][1]


def test_low_confidence_any_sample_declines():
    st = Storage(":memory:")
    st.create_schema()
    c = FakeClient(
        [_ok_response(True, 0.95, ["https://a/1"]), _ok_response(True, 0.55, ["https://b/1"])]
    )
    r = LLMResolver(c, storage=st)
    assert (
        r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30)) is None
    )
    evs = st._conn.execute("SELECT detail FROM evolution_log").fetchall()
    assert "low_confidence" in evs[0][0]


def test_disagreement_declines():
    st = Storage(":memory:")
    st.create_schema()
    c = FakeClient(
        [_ok_response(True, 0.9, ["https://a/1"]), _ok_response(False, 0.9, ["https://b/1"])]
    )
    r = LLMResolver(c, storage=st)
    assert (
        r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30)) is None
    )
    evs = st._conn.execute("SELECT detail FROM evolution_log").fetchall()
    assert "disagreement" in evs[0][0]


def test_api_error_declines():
    st = Storage(":memory:")
    st.create_schema()

    def boom(**kw):
        raise LLMError("LLM HTTP 429: rate limited")

    r = LLMResolver(type("C", (), {"responses_create": staticmethod(boom)})(), storage=st)
    assert (
        r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30)) is None
    )
    evs = st._conn.execute("SELECT detail FROM evolution_log").fetchall()
    assert "api_error" in evs[0][0]


def test_window_bounds():
    c = FakeClient([])
    r = LLMResolver(c)
    q = _q(datetime(2026, 8, 13, 9, 0))
    assert r.resolve(q, _spec(), datetime(2026, 8, 13, 8, 59)) is None  # 截止未到
    assert r.resolve(q, _spec(), datetime(2026, 8, 20, 9, 0)) is None  # 超宽限（3 天）
    assert not c.calls  # 窗口外不调用


def test_storage_none_silently_skips_logging():
    c = FakeClient(
        [
            {
                "output": [
                    {"type": "web_search_call", "id": "w1", "results": []},
                    {"type": "output_text", "id": "o1", "content": "{}"},
                ]
            }
        ]
    )
    r = LLMResolver(c, storage=None)  # 不抛异常
    assert (
        r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30)) is None
    )


def test_outcome_missing_or_wrong_type_declines_as_api_error():
    st = Storage(":memory:")
    st.create_schema()
    for bad in (
        {"confidence": 0.9, "citations": ["https://a/1"]},  # 缺 outcome
        {"outcome": "false", "confidence": 0.9, "citations": ["https://a/1"]},  # 字符串
        {"outcome": None, "confidence": 0.9, "citations": ["https://a/1"]},
    ):  # None
        c = FakeClient(
            [
                {"output": [{"type": "output_text", "id": "o1", "content": json.dumps(bad)}]},
                _ok_response(False, 0.9, ["https://a/1"]),
            ]
        )
        r = LLMResolver(c, storage=st)
        assert (
            r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30))
            is None
        )
        evs = st._conn.execute(
            "SELECT detail FROM evolution_log ORDER BY id DESC LIMIT 1"
        ).fetchall()
        assert "api_error" in evs[0][0]


def test_outcome_confidence_invalid_declines_as_api_error():
    # confidence 缺字段/非数值 → ValueError → api_error（spec §4.4：缺字段走降级）
    st = Storage(":memory:")
    st.create_schema()
    for bad in (
        {"outcome": True, "citations": ["https://a/1"]},  # 缺 confidence
        {"outcome": True, "confidence": "高", "citations": ["https://a/1"]},
    ):  # 非数值
        c = FakeClient(
            [
                {"output": [{"type": "output_text", "id": "o1", "content": json.dumps(bad)}]},
                _ok_response(False, 0.9, ["https://a/1"]),
            ]
        )
        r = LLMResolver(c, storage=st)
        assert (
            r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30))
            is None
        )
        evs = st._conn.execute(
            "SELECT detail FROM evolution_log ORDER BY id DESC LIMIT 1"
        ).fetchall()
        assert "api_error" in evs[0][0]


def test_citations_string_form_declines_no_evidence():
    # M-1：verdict citations 为字符串（非 list-of-str）→ 整体作废 → no_evidence，
    # 不逐字符拆成垃圾引用。
    st = Storage(":memory:")
    st.create_schema()
    c = FakeClient(
        [
            {
                "output": [
                    {"type": "web_search_call", "id": "w1", "results": []},
                    {
                        "type": "output_text",
                        "id": "o1",
                        "content": json.dumps(
                            {"outcome": True, "confidence": 0.9, "citations": "https://a/1"}
                        ),
                    },
                ]
            },
            _ok_response(True, 0.9, ["https://a/1"]),
        ]
    )
    r = LLMResolver(c, storage=st)
    assert (
        r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30)) is None
    )
    evs = st._conn.execute("SELECT detail FROM evolution_log ORDER BY id DESC LIMIT 1").fetchall()
    assert "no_evidence" in evs[0][0]


def test_missing_web_search_call_declines_as_api_error():
    # M-2：响应无 web_search_call 条目（搜索未发生）→ 引用必为幻觉 → api_error
    st = Storage(":memory:")
    st.create_schema()
    c = FakeClient(
        [
            {
                "output": [
                    {
                        "type": "output_text",
                        "id": "o1",
                        "content": json.dumps(
                            {"outcome": True, "confidence": 0.9, "citations": ["https://a/1"]}
                        ),
                    }
                ]
            },
            _ok_response(True, 0.9, ["https://a/1"]),
        ]
    )
    r = LLMResolver(c, storage=st)
    assert (
        r.resolve(_q(datetime(2026, 8, 13, 9, 0)), _spec(), datetime(2026, 8, 14, 16, 30)) is None
    )
    evs = st._conn.execute("SELECT detail FROM evolution_log ORDER BY id DESC LIMIT 1").fetchall()
    assert "api_error" in evs[0][0]
