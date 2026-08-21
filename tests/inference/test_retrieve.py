from datetime import UTC, datetime

import httpx

from predictor.data.gdelt_source import GDELTSource, _parse_gdelt_date
from predictor.data.newsapi_source import NewsAPISource
from predictor.data.sources import BaseSource
from predictor.inference.retrieve import Document, retrieve_and_store


class FakeSource(BaseSource):
    name = "fake"

    def __init__(self, docs):
        self.docs = docs

    def fetch(self, search_term):
        return self.docs


def _doc(days_ago: int) -> Document:
    return Document(
        source="fake",
        url=f"u{days_ago}",
        title=f"t{days_ago}",
        content="c",
        published_at=datetime(2026, 8, 10),
        fetched_at=datetime(2026, 8, 11),
    )


class MemStore:
    def __init__(self):
        self.docs = []

    def add_document(self, qid, source, url, title, content, *, published_at, fetched_at=None):
        self.docs.append((qid, url))
        return len(self.docs)


def test_retrieve_filters_future_and_timeless_documents():
    now = datetime(2026, 8, 11)
    future = Document(
        source="fake",
        url="future",
        title="t",
        content="c",
        published_at=datetime(2026, 8, 12),
        fetched_at=now,
    )
    timeless = Document(
        source="fake", url="timeless", title="t", content="c", published_at=None, fetched_at=now
    )
    source = FakeSource([_doc(1), future, timeless])
    store = MemStore()
    docs = retrieve_and_store(1, "Q", ["term"], [source], store, now=now)
    assert all(d.published_at <= now for d in docs)  # 未来文档被剔除
    assert "future" not in [d.url for d in docs]
    assert "timeless" not in [d.url for d in docs]  # 时间戳缺失同样拒绝（防泄漏）


def test_retrieve_deduplicates_by_url():
    now = datetime(2026, 8, 11)
    d1 = _doc(1)
    d1_dup = Document(
        source="fake",
        url="u1",
        title="t",
        content="c",
        published_at=datetime(2026, 8, 10),
        fetched_at=now,
    )
    store = MemStore()
    docs = retrieve_and_store(1, "Q", ["term", "term2"], [FakeSource([d1, d1_dup])], store, now=now)
    assert len(docs) == 1
    assert docs[0].id == 1


def test_retrieve_swallows_source_errors():
    class BoomSource(BaseSource):
        name = "boom"

        def fetch(self, search_term):
            raise RuntimeError("network down")

    store = MemStore()
    docs = retrieve_and_store(1, "Q", ["term"], [BoomSource()], store, now=datetime(2026, 8, 11))
    assert docs == []


def test_retrieve_normalizes_tz_aware_dates():
    import datetime as dt

    now = dt.datetime(2026, 8, 11)
    aware = Document(
        source="fake",
        url="aware",
        title="t",
        content="c",
        published_at=dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
        fetched_at=dt.datetime(2026, 8, 11, tzinfo=dt.UTC),
    )
    store = MemStore()
    docs = retrieve_and_store(1, "Q", ["term"], [FakeSource([aware])], store, now=now)
    assert len(docs) == 1
    assert docs[0].published_at.tzinfo is None


# ---- GDELT / NewsAPI 数据源（计划 Task 7 Step 4，mock transport 覆盖）----


def test_parse_gdelt_date_formats():
    assert _parse_gdelt_date("20260810T120000Z") == datetime(2026, 8, 10, 12, 0, 0)
    assert _parse_gdelt_date("2026-08-10T12:00:00Z") == datetime(2026, 8, 10, 12, 0, 0)
    assert _parse_gdelt_date("20260810120000") == datetime(2026, 8, 10, 12, 0, 0)
    assert _parse_gdelt_date("garbage") is None


def test_gdelt_source_fetch_parses_articles():
    def handler(request):
        q = request.url.params
        assert q["query"] == "油价"
        assert q["startdatetime"] == "20260801000000"  # 历史窗口生效
        assert q["enddatetime"] == "20260810000000"
        return httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "url": "http://a",
                        "title": "标题",
                        "content": "正文",
                        "seendate": "20260805T100000Z",
                    },
                    {"url": "http://b", "title": "无日期", "content": "x", "seendate": "bad"},
                ]
            },
        )

    src = GDELTSource(
        start=datetime(2026, 8, 1),
        end=datetime(2026, 8, 10),
        _transport=httpx.MockTransport(handler),
    )
    docs = src.fetch("油价")
    assert len(docs) == 2
    assert docs[0].source == "gdelt"
    assert docs[0].published_at == datetime(2026, 8, 5, 10, 0, 0)
    assert docs[1].published_at is None


def test_gdelt_source_uses_timespan_when_no_window():
    def handler(request):
        assert "timespan" in request.url.params
        return httpx.Response(200, json={"articles": []})

    src = GDELTSource(_transport=httpx.MockTransport(handler))
    assert src.fetch("q") == []


def test_gdelt_source_returns_empty_on_http_error():
    def handler(request):
        return httpx.Response(500)

    src = GDELTSource(_transport=httpx.MockTransport(handler))
    assert src.fetch("q") == []


def test_newsapi_source_requires_key():
    src = NewsAPISource(api_key="")
    assert src.fetch("q") == []  # 无 key 直接空，不发网络


def test_newsapi_source_fetch_parses_articles():
    def handler(request):
        assert request.headers["X-Api-Key"] == "k"
        return httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "url": "http://n",
                        "title": "T",
                        "description": "D",
                        "publishedAt": "2026-08-10T12:00:00Z",
                    },
                    {
                        "url": "http://n2",
                        "title": "T2",
                        "content": "C2",
                        "publishedAt": "not-a-date",
                    },
                ]
            },
        )

    src = NewsAPISource(api_key="k", _transport=httpx.MockTransport(handler))
    docs = src.fetch("q")
    assert len(docs) == 2
    assert docs[0].source == "newsapi"
    assert docs[0].published_at == datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
    assert docs[1].published_at is None
