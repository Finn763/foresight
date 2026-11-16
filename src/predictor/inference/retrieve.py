# retrieve.py
from datetime import UTC, datetime
from typing import Any

from predictor.data.sources import BaseSource, Document
from predictor.timeutil import CN_TZ


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
    # naive 入参按本地时钟解释（与 datetime.now() 调用方一致），再统一成 naive UTC。
    # 旧实现直接 strip tzinfo，把本地时间当 UTC，回测 cutoff 最多放宽 8 小时
    # （truthset_replay 传北京 naive pred_date，会放进预测时点之后的新闻）。
    if now.tzinfo is None:
        now = now.replace(tzinfo=CN_TZ)
    now = now.astimezone(UTC).replace(tzinfo=None)

    def _norm(dt: datetime) -> datetime:
        # 文档时间契约不同：各 source 已统一产出 UTC（aware 或 naive-UTC）。
        if dt.tzinfo is not None:
            return dt.astimezone(UTC).replace(tzinfo=None)
        return dt

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
