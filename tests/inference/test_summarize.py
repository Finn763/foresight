from predictor.data.sources import Document
from predictor.inference.summarize import summarize_documents


def _doc(i):
    return Document("s", f"u{i}", f"标题{i}", "正文内容", None, None)


class FakeClient:
    calls = []

    def chat(self, model, messages, **kw):
        self.calls.append((model, messages))
        return '{"summary": "这是摘要"}'


def test_summarizes_each_doc():
    client = FakeClient()
    out = summarize_documents([_doc(0), _doc(1)], client)
    assert len(out) == 2
    # 真实 LLMClient.chat(model, messages) 的首参是 model；漏传会 TypeError 被吞成标题
    assert client.calls and client.calls[0][0] is None
    assert client.calls[0][1][0]["role"] == "user"


def test_falls_back_to_title_on_bad_json():
    class BadClient:
        def chat(self, model, messages, **kw):
            return "not json"

    out = summarize_documents([_doc(0)], BadClient())
    assert out == ["标题0"]


def test_falls_back_to_title_on_exception():
    class BoomClient:
        def chat(self, model, messages, **kw):
            raise RuntimeError("llm down")

    out = summarize_documents([_doc(0)], BoomClient())
    assert out == ["标题0"]
