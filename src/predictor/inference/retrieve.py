# retrieve.py
from datetime import datetime
from typing import Any

from predictor.data.sources import BaseSource, Document


def retrieve_and_store(
    question_id: int,
    title: str,
    search_terms: list[str],
    sources: list[BaseSource],
    storage: Any,
    *,
    now: datetime | None = None,
) -> list[Document]:
    now = now or datetime.now()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)  # 统一为 naive UTC 比较/入库（DuckDB TIMESTAMP 无时区）

    def _norm(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

    docs: list[Document] = []
    seen: set[str] = set()
    for term in search_terms:
        for src in sources:
            try:
                fetched = src.fetch(term)
            except Exception:
                fetched = []  # 源不可用：跳过不致命
            for d in fetched:
                # 防泄漏铁律：只保留预测时点之前发布的文档
                if d.published_at is None:
                    continue  # 时间戳缺失无法证明揭晓前可得 → 拒绝
                if _norm(d.published_at) > now:
                    continue
                if d.url in seen:
                    continue
                seen.add(d.url)
                d.published_at = _norm(d.published_at)  # 入库统一 naive UTC
                d.id = storage.add_document(
                    question_id,
                    d.source,
                    d.url,
                    d.title,
                    d.content,
                    published_at=d.published_at,
                    fetched_at=d.fetched_at,
                )
                docs.append(d)
    return docs
