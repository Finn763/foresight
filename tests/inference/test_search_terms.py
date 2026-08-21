from predictor.inference.search_terms import generate_search_terms


class FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def chat_json(self, messages, **kw):
        return self._payload


def test_returns_terms_list():
    client = FakeClient({"terms": ["OPEC 减产 会议", "油价 上涨 2026"]})
    terms = generate_search_terms("OPEC 会减产吗", client)
    assert isinstance(terms, list) and len(terms) >= 1


def test_falls_back_to_title_on_bad_json():
    client = FakeClient({"nope": 1})
    assert generate_search_terms("兜底问题", client) == ["兜底问题"]


def test_falls_back_to_title_on_empty_list():
    client = FakeClient({"terms": []})
    assert generate_search_terms("兜底问题", client) == ["兜底问题"]


def test_coerces_terms_to_str():
    client = FakeClient({"terms": [123, "ok"]})
    assert generate_search_terms("Q", client) == ["123", "ok"]
