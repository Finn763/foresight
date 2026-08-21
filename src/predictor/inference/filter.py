from typing import Any

from predictor.data.sources import Document

FILTER_PROMPT = (
    "给定预测问题与候选文档，选出与回答问题最相关的 {top_k} 篇。"
    '只输出 JSON：{{"relevant": [文档序号...]}}。\n问题：{title}\n文档：\n{docs}'
)


def filter_relevant(
    title: str, docs: list[Document], client: Any, top_k: int = 5
) -> list[Document]:
    if not docs:
        return []
    listing = "\n".join(f"[{i}] {d.title} | {d.content[:120]}" for i, d in enumerate(docs))
    try:
        out = client.chat_json(
            [
                {
                    "role": "user",
                    "content": FILTER_PROMPT.format(top_k=top_k, title=title, docs=listing),
                }
            ]
        )
        idxs = [int(i) for i in out.get("relevant", [])][:top_k]
        kept = [docs[i] for i in idxs if 0 <= i < len(docs)]
        if kept:
            return kept
        # LLM 返回空相关列表也视为失败：降级 docs[:top_k]（宁多勿漏，相关性交给 forecast）
        return docs[:top_k]
    except Exception:
        return docs[:top_k]
