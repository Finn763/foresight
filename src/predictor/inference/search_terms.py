"""Step 1：为预测问题生成 5 个 Google 式搜索查询。"""

from typing import Any

SEARCH_TERMS_PROMPT = (
    "你是情报分析师。为回答以下预测问题，生成 6 个搜索查询：前 3 个英文（供 GDELT 等英文新闻源，"
    "必须用英文表达），后 3 个中文（供微博等中文社交源）。覆盖：事件主体、地点、时间、数据来源。"
    '只输出 JSON：{{"terms": ["..."]}}。\n问题：{title}'
)


def generate_search_terms(title: str, client: Any) -> list[str]:
    out = client.chat_json([{"role": "user", "content": SEARCH_TERMS_PROMPT.format(title=title)}])
    terms = out.get("terms") if isinstance(out, dict) else None
    if isinstance(terms, list) and terms:
        return [str(t) for t in terms]
    return [title]
