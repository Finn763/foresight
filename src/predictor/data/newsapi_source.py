# newsapi_source.py：NewsAPI（需 key；免费档禁商用，商用档自备）
import os
from datetime import UTC, datetime, timedelta

import httpx

from predictor.data.sources import Document


class NewsAPISource:
    name = "newsapi"

    def __init__(self, api_key: str | None = None, *, _transport=None):
        self.api_key = api_key or os.getenv("NEWSAPI_KEY", "")
        self._transport = _transport

    def fetch(self, search_term: str) -> list[Document]:
        if not self.api_key:
            return []
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": search_term,
            "from": (datetime.now(UTC) - timedelta(days=3)).date().isoformat(),
            "sortBy": "relevancy",
            "pageSize": 5,
            "language": "en",
        }
        try:
            with httpx.Client(transport=self._transport, timeout=5.0) as c:
                r = c.get(url, params=params, headers={"X-Api-Key": self.api_key})
                r.raise_for_status()
                arts = r.json().get("articles", [])
        except (httpx.HTTPError, ValueError):
            return []
        out = []
        for a in arts:
            pub = a.get("publishedAt")
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else None
            except Exception:
                pub_dt = None
            out.append(
                Document(
                    source="newsapi",
                    url=a.get("url", ""),
                    title=a.get("title", ""),
                    content=(a.get("description") or a.get("content") or "")[:2000],
                    published_at=pub_dt,
                    fetched_at=datetime.now(UTC),
                )
            )
        return out
