"""A 类 resolver 注册表。A → MarketResolver；B → LLMResolver（Responses API web_search）；C → None。"""

from predictor.resolution.market_resolver import SYMBOL_MAP, MarketResolver
from predictor.resolution.quotes import fetch_close, fetch_kline, fetch_prev_close

_client = None


def _default_client():
    """惰性构造 LLMClient（stateless 缓存复用）；构造失败（.env 缺 key 等）→ None。"""
    global _client
    if _client is None:
        try:
            from predictor.config import Settings
            from predictor.llm.client import LLMClient

            _client = LLMClient(**Settings().llm_client_kwargs)
        except Exception:
            return None
    return _client


def get_resolver(resolution_class: str, storage=None):
    """A → MarketResolver（默认双源配置）；B → LLMResolver（storage 注入护栏日志，
    client 共享缓存）；C → None。B 类 client 构造失败（如缺 API key）→ None
    （退化为 pending，不崩轮）。"""
    if resolution_class == "A":
        return MarketResolver(fetch_close, fetch_prev_close, fetch_kline=fetch_kline)
    if resolution_class == "B":
        from predictor.resolution.llm_resolver import LLMResolver

        client = _default_client()
        if client is None or not client.api_key:
            return None  # 缺 key/构造失败 → pending（不每日烧 401）
        return LLMResolver(client, storage)
    return None


__all__ = ["get_resolver", "MarketResolver", "SYMBOL_MAP"]
