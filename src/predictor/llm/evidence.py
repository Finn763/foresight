"""DeepSeek Search → 证据块 + 允许引用集（预测器与 B 类揭晓器共用）。

2026-10 起 DeepSeek 的搜索是独立 REST 接口 /v1/tools/search；/v1/responses 的
web_search 工具不再真正执行（output 无 web_search_call）——护栏全灭。此模块把
「搜索 → 证据块 + 可引用 URL 集」这一层收在一处，两条链路（预测 / 揭晓）共用。
"""


def evidence_block(results: list[dict], *, limit: int = 8) -> tuple[str, list[str]]:
    """把 search results 渲染成给模型读的证据块，并返回可引用 URL 集（护栏白名单）。"""
    chunks: list[str] = []
    urls: list[str] = []
    for r in results[:limit]:
        if not isinstance(r, dict):
            continue
        u = r.get("url")
        if not isinstance(u, str) or not u:
            continue
        urls.append(u)
        meta = [
            str(r.get("site_name") or ""),
            str(r.get("date") or "")[:10],
            f"authority={r.get('authority')}" if r.get("authority") is not None else "",
        ]
        body = str(r.get("summary") or r.get("snippet") or r.get("content") or "")[:900]
        chunks.append(
            f"### {r.get('title') or u}\nURL: {u}\n{' · '.join(m for m in meta if m)}\n{body}"
        )
    if not chunks:
        return "", []
    return "\n\n【检索结果】（citations 只能取下列 URL）\n" + "\n\n".join(chunks), urls


async def asearch_evidence(
    client, query: str, *, ori_question: str | None = None, count: int = 8
) -> tuple[str, list[str]]:
    """异步入口（预测器在事件循环内调用）。"""
    results = await client.asearch(query, count=count, ori_question=ori_question)
    return evidence_block(results)


def search_evidence(
    client, query: str, *, ori_question: str | None = None, count: int = 8
) -> tuple[str, list[str]]:
    """同步入口（揭晓器在同步上下文调用）。"""
    return evidence_block(client.search(query, count=count, ori_question=ori_question))
