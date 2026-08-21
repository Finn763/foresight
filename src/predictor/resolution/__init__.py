"""揭晓（resolution）模块：行情客户端 + resolution_spec 校验器 + A 类自动揭晓。

P0 四件套：
- quotes.py: 腾讯/新浪/CoinGecko/Coinbase 行情取价（fetch_close / fetch_prev_close）
- spec.py: resolution_spec 校验器（validate_resolution_spec）
- market_resolver.py: A 类双源揭晓（MarketResolver + SYMBOL_MAP）
- registry.py / auto_resolve.py: 注册表 + 揭晓轮入口
"""

from predictor.resolution.quotes import QuoteError, fetch_close, fetch_prev_close
from predictor.resolution.spec import validate_resolution_spec

__all__ = ["QuoteError", "fetch_close", "fetch_prev_close", "validate_resolution_spec"]
