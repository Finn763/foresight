"""行情客户端：腾讯(A股/美股) + 新浪(美股/外盘期货/外汇) + CoinGecko/Coinbase(BTC)。
双源一致性由 market_resolver（Task 4）负责；本模块只负责"取价"，失败抛 QuoteError。

端点与字段序由 scripts/probe_quotes.py 于 2026-08-12 16:34(北京时间) 实测定稿。
关键实测记录（与 brief 假设不符处已修正，样例为 verbatim 实测输出）：

- 新浪 gb_$inx（标普500指数）: parts[1]=现价 parts[2]=涨跌幅% parts[4]=涨跌额
  parts[5]=今开 parts[26]=昨收。实测: 现价 7728.2002 / 涨跌幅 -0.32 / 涨跌额 -24.9100 /
  今开 7767.5098 / 昨收(26)=7753.1099 —— 与腾讯 usINX parts[4]=7753.11 一致。
  ⚠️ brief 假设 parts[2]=昨收 实为涨跌幅；gb_ 指数昨收在 parts[26]（gb_$dji 同布局: parts[26]=53975.9805）。
- 新浪 hf_GC / hf_OIL（外盘期货）: parts[0]=现价（实测 4467.245 / 89.240）。
  hf_OIL 即布伦特原油（field 13=名称"布伦特原油"）；hf_B 实测空响应不可用。
  hf_ 昨收在 parts[7]（GC 实测 4441.100 内洽但未双源验证），P0 期货题不用 gt_prev_close 判定 → 拒昨收。
- 新浪 fx_susdcnh（离岸人民币）: parts[1]=现价 parts[8]=昨收。
  实测: 16:32:55,6.746900,6.747000,6.746200,...（平盘日 现价==昨收=6.746900）。
  ⚠️ brief 假设 parts[2]=昨收；T3 首版标注"parts[2]=买价"有误——实测 6.747000
  非买价（parts[3]=6.746200 与之构成买/卖对时 parts[2] 在偏高侧；语义未独立验证，
  P0 不用 CNH 昨收判定）。
- 腾讯 usINX / usDJI（美股）: parts[3]=现价 parts[4]=昨收。
  实测: v_usINX="200~标普500~.INX~7728.20~7753.11~7767.51~..."（现价 7728.20/昨收 7753.11，与新浪一致）。
- 腾讯 s_sh000001（A股指数）: parts[3]=现价 parts[4]=涨跌额（无直接昨收 → 昨收=现价-涨跌额）。
  实测: v_s_sh000001="1~上证指数~000001~3946.68~12.59~0.32~..." → 昨收 3934.09；
  跨源核对新浪 s_sh000001（现价 3946.6752-涨跌额 12.5823=3934.0929）一致。
  ⚠️ brief 假设 parts[4]=昨收 实为涨跌额。
- CoinGecko: {"bitcoin":{"usd":63760}}；Coinbase: {"data":{"amount":"63710.165",...}}。
- 币安 api.binance.com 本机实测 HTTP 451（地域限制）不可用 → 备源换 Coinbase（实测 HTTP 200）。
"""

from datetime import UTC, datetime

import httpx


class QuoteError(Exception):
    pass


_TIMEOUT = 15.0
# provider -> (url_template, headers, parse_fn 名)；coingecko/coinbase 用固定 URL（忽略 symbol）
QUOTE_PATHS = {
    "sina": ("https://hq.sinajs.cn/list={sym}", {"Referer": "https://finance.sina.com.cn"}, "sina"),
    "tencent": ("https://qt.gtimg.cn/q={sym}", {}, "tencent"),
    "coingecko": (
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
        {},
        "coingecko",
    ),
    "coinbase": ("https://api.coinbase.com/v2/prices/BTC-USD/spot", {}, "coinbase"),
}


def _parse_sina(text: str, symbol: str) -> float:
    # gb_$inx/fx_: parts[1]=现价；hf_ 期货: parts[0]=现价（实测定稿）
    body = text.split('"')[1] if '"' in text else ""
    if not body or body.startswith("0;") or body == "0":
        raise QuoteError(f"sina empty for {symbol}")
    parts = body.split(",")
    if symbol.startswith("hf_"):
        price = float(parts[0])
    else:
        price = float(parts[1])
    return price


def _parse_tencent(text: str, symbol: str) -> float:
    # usINX/s_sh000001 均实测 parts[3]=现价
    body = text.split('"')[1]
    parts = body.split("~")
    if len(parts) < 5:
        raise QuoteError(f"tencent parse fail for {symbol}: {text[:80]}")
    return float(parts[3])


def _parse_coingecko(text: str) -> float:
    import json

    return float(json.loads(text)["bitcoin"]["usd"])


def _parse_coinbase(text: str) -> float:
    import json

    return float(json.loads(text)["data"]["amount"])


_PARSERS = {
    "sina": _parse_sina,
    "tencent": _parse_tencent,
    "coingecko": _parse_coingecko,
    "coinbase": _parse_coinbase,
}


def fetch_close(provider: str, symbol: str) -> float:
    """拉一次报价。symbol 与 provider 由 QUOTE_PATHS 决定；sina/tencent 需传 symbol。"""
    if provider not in QUOTE_PATHS:
        raise QuoteError(f"unknown provider {provider}")
    url_tmpl, headers, parser_name = QUOTE_PATHS[provider]
    try:
        if provider in ("coingecko", "coinbase"):
            url = url_tmpl
        else:
            url = url_tmpl.format(sym=symbol)
        r = httpx.get(url, headers=headers, timeout=_TIMEOUT)
        if r.status_code >= 400:
            raise QuoteError(f"HTTP {r.status_code} from {provider}")
        return _PARSERS[parser_name](r.text, symbol)
    except QuoteError:
        raise
    except Exception as e:
        raise QuoteError(f"quote failed {provider}/{symbol}: {e}") from e


# ---------- 昨收（Task 4 market_resolver gt_prev_close 依赖） ----------


def _parse_sina_prev(text: str, symbol: str) -> float:
    body = text.split('"')[1] if '"' in text else ""
    # T3 遗留 minor 修复：与 _parse_sina 对齐补 body == "0"（"0" 是新浪无效代码空响应，
    # 缺此守卫时 fx_ 前缀会拿 parts[8] 越界/空值，毒化昨收判定）
    if not body or body.startswith("0;") or body == "0":
        raise QuoteError(f"sina empty for {symbol}")
    parts = body.split(",")
    if symbol.startswith("hf_"):
        # hf_ 昨收在 parts[7]（GC 实测 4441.100，未双源验证）；P0 期货题不用 gt_prev_close 判定
        raise QuoteError("hf_ 无双源验证的昨收，P0 期货题不用 gt_prev_close 判定")
    if symbol.startswith("gb_"):
        # 实测 2026-08-12: parts[26]=昨收（$inx 7753.1099 / $dji 53975.9805，均与腾讯 parts[4] 一致）
        return float(parts[26])
    if symbol.startswith("fx_"):
        # 实测 2026-08-12: parts[8]=昨收（平盘日 6.746900==现价；未独立验证，P0 不用 CNH 昨收判定）
        return float(parts[8])
    raise QuoteError(f"sina prev-close layout unknown for {symbol}")


def _parse_tencent_prev(text: str, symbol: str) -> float:
    parts = text.split('"')[1].split("~")
    if len(parts) < 5:
        raise QuoteError(f"tencent parse fail for {symbol}")
    if symbol.startswith("us"):
        # 实测 2026-08-12: parts[4]=昨收（usINX 7753.11 / usDJI 53975.98，均与新浪一致）
        return float(parts[4])
    if symbol.startswith("s_"):
        # A股指数: parts[4]=涨跌额 → 昨收=现价-涨跌额（跨源与新浪 s_sh000001 一致）
        return float(parts[3]) - float(parts[4])
    raise QuoteError(f"tencent prev-close layout unknown for {symbol}")


_PREV_PARSERS = {"sina": _parse_sina_prev, "tencent": _parse_tencent_prev}


def fetch_prev_close(provider: str, symbol: str) -> float:
    if provider not in _PREV_PARSERS:
        raise QuoteError(f"no prev-close parser for {provider}")
    url_tmpl, headers, _ = QUOTE_PATHS[provider]
    try:
        r = httpx.get(url_tmpl.format(sym=symbol), headers=headers, timeout=_TIMEOUT)
        if r.status_code >= 400:
            raise QuoteError(f"HTTP {r.status_code} from {provider}")
        return _PREV_PARSERS[provider](r.text, symbol)
    except QuoteError:
        raise
    except Exception as e:
        raise QuoteError(f"prev-close failed {provider}/{symbol}: {e}") from e


# ---------- 日 K 线（record_high 依赖；2026-08-14 实测定稿） ----------
# 新浪 US 日K 端点实测不可用（Service not valid / File not found）→ 主源腾讯、备源 Yahoo。
# 腾讯 kline/kline: https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=usINX,day,,,320
#   → data["us.INX"].day = [[date, open, close, high, low, vol], ...]（key 规则与 fqkline 不同：
#   fqkline 用 usINX，kline/kline 用 us.INX —— 解析时按 data 下第一个含 day 的节点取，不硬编码 key）。
#   320 根 ≈ 1.3 年。
# Yahoo chart: https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=2y&interval=1d
#   → result[0].timestamp + indicators.quote[0].close（None close 跳过）。
#   实测 8-13 close 7798.99 与腾讯 fqkline 一致。
_KLINE_DATALEN = {"tencent": 320, "yahoo": None}  # yahoo 用 range=2y
_KLINE_TIMEOUT = 20.0


def fetch_kline(provider: str, symbol: str) -> list[tuple]:
    """拉日 K 收盘序列，返回升序 [(date, close), ...]。失败抛 QuoteError。"""
    try:
        if provider == "tencent":
            url = (
                f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
                f"?param={symbol},day,,,{_KLINE_DATALEN[provider]}"
            )
            r = httpx.get(url, timeout=_KLINE_TIMEOUT)
            if r.status_code >= 400:
                raise QuoteError(f"HTTP {r.status_code} from {provider}")
            node = next(
                v for v in r.json()["data"].values() if isinstance(v, dict) and "day" in v
            )
            day = node["day"]
            if not day:
                raise QuoteError(f"tencent kline empty for {symbol}")
            bars = [
                (
                    datetime.strptime(parts[0], "%Y-%m-%d").date(),
                    float(parts[2]),
                )
                for parts in day
            ]
        elif provider == "yahoo":
            from urllib.parse import quote as _quote

            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/"
                f"{_quote(symbol, safe='')}?range=2y&interval=1d"
            )
            r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=_KLINE_TIMEOUT)
            if r.status_code >= 400:
                raise QuoteError(f"HTTP {r.status_code} from {provider}")
            res = r.json()["chart"]["result"][0]
            bars = [
                (datetime.fromtimestamp(t, tz=UTC).date(), float(c))
                for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"])
                if c is not None
            ]
        else:
            raise QuoteError(f"unknown kline provider {provider}")
    except QuoteError:
        raise
    except Exception as e:
        raise QuoteError(f"kline failed {provider}/{symbol}: {e}") from e
    bars.sort(key=lambda b: b[0])
    return bars
