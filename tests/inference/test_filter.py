from predictor.data.sources import Document
from predictor.inference.filter import filter_relevant


def _doc(i):
    return Document(
        source="s", url=f"u{i}", title=f"t{i}", content="c", published_at=None, fetched_at=None
    )


class FakeClient:
    def chat_json(self, messages, **kw):
        # 题目里包含序号，直接回 0,1
        return {"relevant": [0, 1]}


def test_keeps_top_selected():
    docs = [_doc(i) for i in range(4)]
    kept = filter_relevant("Q", docs, FakeClient(), top_k=2)
    assert [d.url for d in kept] == ["u0", "u1"]


def test_empty_docs_returns_empty():
    assert filter_relevant("Q", [], FakeClient()) == []


def test_out_of_range_indices_ignored():
    class BadIdxClient:
        def chat_json(self, messages, **kw):
            return {"relevant": [99, -1, 1]}

    docs = [_doc(i) for i in range(3)]
    kept = filter_relevant("Q", docs, BadIdxClient(), top_k=5)
    assert [d.url for d in kept] == ["u1"]


def test_falls_back_to_top_k_on_error():
    class BoomClient:
        def chat_json(self, messages, **kw):
            raise RuntimeError("llm down")

    docs = [_doc(i) for i in range(4)]
    kept = filter_relevant("Q", docs, BoomClient(), top_k=2)
    assert [d.url for d in kept] == ["u0", "u1"]
