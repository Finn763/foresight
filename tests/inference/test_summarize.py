from predictor.data.sources import Document
from predictor.inference.summarize import summarize_documents


def _doc(i):
    return Document("s", f"u{i}", f"标题{i}", "正文内容", None, None)


class FakeClient:
    def chat(self, messages, **kw):
        return '{"summary": "这是摘要"}'


def test_summarizes_each_doc():
    out = summarize_documents([_doc(0), _doc(1)], FakeClient())
    assert len(out) == 2


def test_falls_back_to_title_on_bad_json():
    class BadClient:
        def chat(self, messages, **kw):
            return "not json"

    out = summarize_documents([_doc(0)], BadClient())
    assert out == ["标题0"]


def test_falls_back_to_title_on_exception():
    class BoomClient:
        def chat(self, messages, **kw):
            raise RuntimeError("llm down")

    out = summarize_documents([_doc(0)], BoomClient())
    assert out == ["标题0"]
