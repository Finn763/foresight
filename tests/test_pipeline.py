from datetime import datetime

from predictor.data.sources import Document
from predictor.pipeline import run_prediction


class FakeStore:
    def __init__(self):
        self.docs, self.preds = [], []

    def get_question(self, qid):
        from predictor.data.storage import Question

        return Question(qid, "Q", datetime(2026, 8, 1), datetime(2026, 9, 1), None, None, True)

    def add_document(self, qid, source, url, title, content, *, published_at, fetched_at=None):
        self.docs.append(url)
        return len(self.docs)

    def add_prediction(
        self, qid, probability, *, evidence_ids, model_runs, arm="baseline", arm_group=None
    ):
        assert evidence_ids, "必须有证据"
        self.preds.append(probability)
        return len(self.preds)


class FakeClient:
    def chat_json(self, messages, **kw):
        if "搜索" in messages[-1]["content"]:
            return {"terms": ["term"]}
        if "相关" in messages[-1]["content"]:
            return {"relevant": [0]}
        return {"probability": 0.55, "rationale": "r"}

    def chat(self, messages, **kw):
        return '{"summary": "s"}'


class FakeSource:
    name = "fake"

    def fetch(self, term):
        return [
            Document("fake", "http://e", "证据", "正文", datetime(2026, 8, 1), datetime(2026, 8, 2))
        ]


def test_run_prediction_end_to_end():
    store = FakeStore()
    pred = run_prediction(1, store, FakeClient(), [FakeSource()], now=datetime(2026, 8, 2))
    assert pred is not None
    assert 0.01 <= pred.probability <= 0.99
    assert pred.evidence_ids  # 证据链存在
    assert "依据" in pred.report_md  # 报告含依据段落


def test_run_prediction_rejects_missing_question():
    class NoQuestionStore(FakeStore):
        def get_question(self, qid):
            return None

    assert (
        run_prediction(1, NoQuestionStore(), FakeClient(), [FakeSource()], now=datetime(2026, 8, 2))
        is None
    )


def test_run_prediction_rejects_no_evidence():
    class EmptySource:
        name = "empty"

        def fetch(self, term):
            return []

    assert (
        run_prediction(1, FakeStore(), FakeClient(), [EmptySource()], now=datetime(2026, 8, 2))
        is None
    )


def test_run_prediction_keeps_all_samples_in_model_runs():
    store = FakeStore()
    pred = run_prediction(1, store, FakeClient(), [FakeSource()], now=datetime(2026, 8, 2))
    assert pred is not None
    assert pred.model_runs["deepseek-chat"] == [0.55, 0.55, 0.55]  # n_samples=3 全量采样
    assert store.preds == [pred.probability]


class FailingSearchTermsClient(FakeClient):
    """搜索词阶段 LLM 故障（模拟 API 故障日）。"""

    def chat_json(self, messages, **kw):
        from predictor.llm.client import LLMError

        if "搜索" in messages[-1]["content"]:
            raise LLMError("search terms LLM down")
        return super().chat_json(messages, **kw)


def test_run_prediction_skips_question_when_search_terms_llm_fails():
    """LLM 故障（搜索词阶段）→ 记 evolution_log 后 skip 单题，不击垮整轮。"""

    class LoggingStore(FakeStore):
        def __init__(self):
            super().__init__()
            self.logs = []

        def log_evolution(self, event_type, detail):
            self.logs.append((event_type, detail))

    store = LoggingStore()
    pred = run_prediction(
        1, store, FailingSearchTermsClient(), [FakeSource()], now=datetime(2026, 8, 2)
    )
    assert pred is None  # 单题跳过，不返回预测
    assert store.logs  # 有归因日志
    assert store.logs[0][0] == "prediction_skipped"
    assert "search" in store.logs[0][1].lower()
    assert store.preds == []  # 没有写入任何预测
