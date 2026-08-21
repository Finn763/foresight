"""websearch_predictor 单测（fake client 注入，零网络）：解析/护栏/ensemble/拒绝路径。"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from predictor.data.storage import Storage
from predictor.llm.client import LLMError
from predictor.websearch_predictor import _extract_citations, websearch_predict


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def responses_create(self, **kw):
        self.calls.append(kw)
        if not self._responses:
            raise LLMError("no more responses")
        return self._responses.pop(0)


def _msg_response(prob, rationale, citations, open_urls, *, with_search=True):
    """8-13 实测结构：最终答案在 message 条目，引用三来源。"""
    items = []
    if with_search:
        items.append(
            {
                "type": "web_search_call",
                "id": "call_search",
                "status": "completed",
                "action": {"type": "search", "queries": ["美联储 9月 加息"]},
            }
        )
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
                        {"probability": prob, "rationale": rationale, "citations": citations}
                    ),
                }
            ],
        }
    )
    return {"output": items}


def _st():
    st = Storage(":memory:")
    st.create_schema()
    qid = st.add_question("美联储9月会加息吗", datetime(2026, 9, 17, 9, 0))
    return st, qid


def test_two_samples_mean_and_merged_citations():
    st, qid = _st()
    c = FakeClient(
        [
            _msg_response(0.30, "9月维持概率大", ["https://a/1"], ["https://o/1"]),
            _msg_response(0.34, "同上", ["https://a/1", "https://b/2"], ["https://o/2"]),
        ]
    )
    now = datetime(2026, 8, 13, 20, 0)
    pred = websearch_predict(qid, "美联储9月会加息吗", datetime(2026, 9, 17, 9, 0), now, c, st)
    assert pred is not None
    assert pred.probability == (0.30 + 0.34) / 2
    assert pred.evidence_ids, "引用应落库"
    assert len(pred.evidence_ids) == 4  # verdict citations ∪ open_page：a/1,b/2,o/1,o/2
    assert c.calls[0]["tools"] == [{"type": "web_search"}]
    assert c.calls[0]["tool_choice"] == {"type": "web_search"}
    assert "美联储" in c.calls[0]["instructions"]
    assert c.calls[0]["temperature"] == 0.5


def test_merged_citation_count_and_storage_rows():
    st, qid = _st()
    c = FakeClient(
        [
            _msg_response(0.3, "r", ["https://a/1"], ["https://o/1"]),
            _msg_response(0.3, "r", ["https://a/1", "https://b/2"], ["https://o/2"]),
        ]
    )
    pred = websearch_predict(
        qid, "t", datetime(2026, 9, 17, 9, 0), datetime(2026, 8, 13, 20, 0), c, st
    )
    assert pred is not None
    docs = st.list_question_documents(qid)
    urls = sorted(d["url"] for d in docs)
    assert urls == ["https://a/1", "https://b/2", "https://o/1", "https://o/2"]


def test_invalid_probability_discards_sample():
    st, qid = _st()
    c = FakeClient(
        [
            _msg_response("0.5", "字符串概率", ["https://a/1"], []),  # 非法 → 作废
            _msg_response(0.40, "正常", ["https://b/2"], []),  # 合法
        ]
    )
    pred = websearch_predict(
        qid, "t", datetime(2026, 9, 17, 9, 0), datetime(2026, 8, 13, 20, 0), c, st
    )
    assert pred is not None
    assert pred.probability == 0.40  # 仅合法采样参与


def test_out_of_range_probability_discards_sample():
    st, qid = _st()
    c = FakeClient(
        [
            _msg_response(1.5, "越界", ["https://a/1"], []),
            _msg_response(0.40, "正常", ["https://b/2"], []),
        ]
    )
    pred = websearch_predict(
        qid, "t", datetime(2026, 9, 17, 9, 0), datetime(2026, 8, 13, 20, 0), c, st
    )
    assert pred is not None
    assert pred.probability == 0.40


def test_empty_citations_rejected_no_evidence():
    st, qid = _st()
    c = FakeClient(
        [
            _msg_response(0.30, "r", [], []),
            _msg_response(0.34, "r", ["https://ok/1"], []),
        ]
    )
    pred = websearch_predict(
        qid, "t", datetime(2026, 9, 17, 9, 0), datetime(2026, 8, 13, 20, 0), c, st
    )
    assert pred is None  # 任一采样无引用 → 拒绝
    evs = st.list_evolution_log()
    assert any("no_evidence" in (e.get("detail") or "") for e in evs)


def test_all_samples_fail_returns_none_and_logs():
    st, qid = _st()
    c = FakeClient([])  # 全部 LLMError
    pred = websearch_predict(
        qid, "t", datetime(2026, 9, 17, 9, 0), datetime(2026, 8, 13, 20, 0), c, st
    )
    assert pred is None
    evs = st.list_evolution_log()
    assert any("all samples failed" in (e.get("detail") or "") for e in evs)


def test_missing_web_search_call_discards_sample():
    st, qid = _st()
    c = FakeClient(
        [
            _msg_response(0.30, "r", ["https://a/1"], [], with_search=False),
            _msg_response(0.40, "r", ["https://b/2"], []),
        ]
    )
    pred = websearch_predict(
        qid, "t", datetime(2026, 9, 17, 9, 0), datetime(2026, 8, 13, 20, 0), c, st
    )
    assert pred is not None
    assert pred.probability == 0.40


def test_baseline_and_historical_context_injected():
    st, qid = _st()
    c = FakeClient(
        [_msg_response(0.3, "r", ["https://a/1"], []), _msg_response(0.3, "r", ["https://a/1"], [])]
    )
    baseline = {"base_rate": 0.764, "method": "历史统计"}
    websearch_predict(
        qid,
        "美联储9月会加息吗",
        datetime(2026, 9, 17, 9, 0),
        datetime(2026, 8, 13, 20, 0),
        c,
        st,
        baseline=baseline,
        historical_context="CPI 月环比历史序列",
    )
    assert "【统计基线】" in c.calls[0]["instructions"]
    assert "0.764" in c.calls[0]["instructions"]
    assert "【历史数据上下文】" in c.calls[0]["instructions"]


def test_citations_string_form_discarded():
    # 引用非法（字符串而非 list）→ 整体作废 → 该采样无引用 → 拒绝
    raw = {
        "output": [
            {"type": "web_search_call", "action": {"type": "search", "queries": ["q"]}},
            {
                "type": "message",
                "phase": "final_answer",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {"probability": 0.3, "rationale": "r", "citations": "https://a/1"}
                        ),
                    }
                ],
            },
        ]
    }
    verdict = json.loads(_msg_text(raw))
    assert _extract_citations(raw, verdict) == []


def _msg_text(raw):
    for item in raw["output"]:
        if item.get("type") == "message":
            for block in item.get("content", []) or []:
                if block.get("type") == "output_text":
                    return block["text"]
    return "{}"
