import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from predictor.data.sources import Document
from predictor.data.storage import Storage
from predictor.memory.levers import get_active_candidate
from predictor.pipeline import run_prediction


class _FakeClient:
    """只响应 forecast 的 chat_json（probability/rationale）；其余调用 mock 在下方 monkeypatch。"""

    model = "deepseek-chat"

    def __init__(self, probs):
        self._i = 0
        self._probs = probs

    def chat_json(self, messages, **kw):
        p = self._probs[self._i % len(self._probs)]
        self._i += 1
        return {"probability": p, "rationale": "r"}


class _FakeSource:
    """让 retrieve 返回真实入库文档（同 st.add_document 数据）→ 证据链可溯源。"""

    def fetch(self, term):
        return [
            Document(
                "fake",
                "http://x/1",
                "标题",
                "内容",
                datetime.now() - timedelta(days=1),
                datetime.now(),
            )
        ]


def _make_env(tmp_path: Path):
    st = Storage(str(tmp_path / "a.db"))
    st.create_schema()
    return st


@pytest.fixture
def patch_pipeline(monkeypatch):
    """把 pipeline 的搜索词/过滤/摘要换成确定性 stub（不碰真实 LLM）。"""
    import predictor.pipeline as pl

    monkeypatch.setattr(pl, "generate_search_terms", lambda title, client: ["term1"])

    def _fake_filter(title, docs, client, top_k=5):
        return docs[:top_k]

    monkeypatch.setattr(pl, "filter_relevant", _fake_filter)
    monkeypatch.setattr(pl, "summarize_documents", lambda docs, client: ["摘要"])


def test_arms_stored_with_group(tmp_path, patch_pipeline):
    st = _make_env(tmp_path)
    qid = st.add_question(
        "配对测试题",
        datetime.now() + timedelta(days=5),
        resolution_class="A",
        resolution_spec={"class": "A"},
    )
    st.add_document(qid, "fake", "http://x/1", "标题", "内容", published_at=datetime.now())
    client = _FakeClient([0.6, 0.6, 0.6, 0.7, 0.7, 0.7])
    pa = run_prediction(
        qid, st, client, [_FakeSource()], now=datetime.now(), arm="baseline", arm_group=1
    )
    pb = run_prediction(
        qid,
        st,
        client,
        [_FakeSource()],
        now=datetime.now(),
        prior=0.6,
        arm="experiment",
        arm_group=1,
    )
    assert pa is not None and pb is not None
    rows = st._conn.execute(
        "SELECT arm, arm_group, probability FROM predictions WHERE question_id = ? ORDER BY id",
        [qid],
    ).fetchall()
    assert [r[0] for r in rows] == ["baseline", "experiment"]
    assert all(r[1] == 1 for r in rows)


def test_alpha_param_offsets_extremize(tmp_path, patch_pipeline):
    st = _make_env(tmp_path)
    qid = st.add_question(
        "配对测试题2",
        datetime.now() + timedelta(days=5),
        resolution_class="A",
        resolution_spec={"class": "A"},
    )
    st.add_document(qid, "fake", "http://x/2", "标题2", "内容2", published_at=datetime.now())
    client = _FakeClient([0.55, 0.55, 0.55])
    p = run_prediction(qid, st, client, [_FakeSource()], now=datetime.now(), alpha=0.0)
    assert abs(p.probability - 0.55) < 1e-6  # alpha=0 → 不外推


def test_get_active_candidate_empty(tmp_path):
    st = _make_env(tmp_path)
    assert get_active_candidate(st) is None
