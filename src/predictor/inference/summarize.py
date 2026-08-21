import json
from typing import Any

from predictor.data.sources import Document

SUMMARIZE_PROMPT = '用 ≤80 字总结下面文档的要点，输出 JSON：{{"summary": "..."}}。\n文档标题：{title}\n正文：{content}'


def _summarize_one(d: Document, client: Any) -> str:
    try:
        raw = client.chat(
            [
                {
                    "role": "user",
                    "content": SUMMARIZE_PROMPT.format(title=d.title, content=d.content[:800]),
                }
            ],
            json_mode=True,
        )
        return json.loads(raw).get("summary", d.title)
    except Exception:
        return d.title


def summarize_documents(docs: list[Document], client: Any) -> list[str]:
    return [_summarize_one(d, client) for d in docs]
