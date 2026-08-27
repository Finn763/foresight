"""历史数据层：Yahoo Finance chart API 拉取长周期序列 → LLM 可读摘要 + 基线计算输入。

数据源（免费无 key，2026-08-12 实测可用）：
- Yahoo Finance chart API（query1.finance.yahoo.com）：标普 500（^GSPC）、离岸人民币（USDCNH=X）
  日线 2016 至今（2666 根实测）；已被腾讯/新浪/东财接口限流或不支持长历史后选定

防泄漏纪律：fetch 时只取 published ≤ now 的数据（period2 截断），与检索源一致。
"""

from __future__ import annotations

import json
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import httpx

SERIES = {
    "sp500": {"symbol": "^GSPC", "name": "标普500指数", "unit": "点", "fred": None},
    "usdcnh": {"symbol": "CNY=X", "name": "美元兑人民币（在岸）", "unit": "", "fred": None},
    "gold": {"symbol": "GC=F", "name": "COMEX 黄金期货", "unit": "美元/盎司", "fred": None},
    "brent": {"symbol": "BZ=F", "name": "布伦特原油期货", "unit": "美元/桶", "fred": None},
    "shanghai": {"symbol": "000001.SS", "name": "上证指数", "unit": "点", "fred": None},
    "dow": {"symbol": "^DJI", "name": "道琼斯工业指数", "unit": "点", "fred": None},
    # FRED 宏观序列（key 在 .env FRED_API_KEY，2026-08-12 验证有效）
    "cpi_cn": {
        "symbol": "CHNCPIALLMINMEI",
        "name": "中国 CPI 指数（OECD）",
        "unit": "指数",
        "fred": "CHNCPIALLMINMEI",
        "freq": "monthly",
    },
    "ffr": {
        "symbol": "DFF",
        "name": "美国联邦基金有效利率",
        "unit": "%",
        "fred": "DFF",
        "freq": "monthly",
    },
    "wti_price": {
        "symbol": "WCOILWTICO",
        "name": "WTI 原油现货价（周度）",
        "unit": "美元/桶",
        "fred": "WCOILWTICO",
        "freq": "weekly",
    },
    "wti_stock": {
        "symbol": "stoc/wstk",
        "name": "美国原油商业库存（周度）",
        "unit": "千桶",
        "eia": True,
        "freq": "weekly",
    },  # EIA OpenData（key 在 .env EIA_API_KEY）
}
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
EIA_URL = "https://api.eia.gov/v2/"
START_TS = 1451606400  # 2016-01-01 UTC
_EPOCH = datetime(1970, 1, 1)

# 让 fetch 可注入 transport（测试用）
_transport: httpx.BaseTransport | None = None


def set_transport(t: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = t


def _fred_key() -> str:
    from predictor.config import Settings

    return Settings().fred_api_key


def _eia_key() -> str:
    from predictor.config import Settings

    return Settings().eia_api_key


def fetch_eia_series(
    path: str = "petroleum/stoc/wstk/data/", start: str = "2016-01-01", end: str | None = None
) -> list[dict[str, Any]]:
    """EIA OpenData：美国原油商业库存周度（千桶）→ [{date, close, ...}] 升序。无 key/失败返回 []。"""
    key = _eia_key()
    if not key:
        return []
    try:
        params = {
            "api_key": key,
            "frequency": "weekly",
            "data[0]": "value",
            "facets[duoarea][]": "NUS",
            "facets[product][]": "EPC0",
            "start": start,
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": 5000,
        }
        if end:
            params["end"] = end
        with httpx.Client(transport=_transport, timeout=20.0) as c:
            r = c.get(EIA_URL + path, params=params)
            r.raise_for_status()
            d = r.json()
        out = []
        for row in d.get("response", {}).get("data") or []:
            v = row.get("value")
            if v in (None, ""):
                continue
            try:
                fv = float(v)
            except ValueError:
                continue
            out.append(
                {"date": row.get("period", ""), "open": fv, "close": fv, "high": fv, "low": fv}
            )
        return out
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []


def _to_ts(dt: datetime) -> int:
    return int((dt - _EPOCH).total_seconds())


def fetch_fred_series(
    series_id: str, start: str = "2016-01-01", end: str | None = None
) -> list[dict[str, Any]]:
    """FRED 观测序列 → [{date, close, ...}] 升序（value 存 close）。无 key/失败返回 []。"""
    key = _fred_key()
    if not key:
        return []
    try:
        params = {
            "series_id": series_id,
            "api_key": key,
            "file_type": "json",
            "observation_start": start,
        }
        if end:
            params["observation_end"] = end
        with httpx.Client(transport=_transport, timeout=15.0) as c:
            r = c.get(FRED_URL, params=params)
            r.raise_for_status()
            d = r.json()
        out = []
        for o in d.get("observations", []):
            v = o.get("value")
            if v in (None, "", "."):
                continue
            try:
                fv = float(v)
            except ValueError:
                continue
            out.append({"date": o["date"], "open": fv, "close": fv, "high": fv, "low": fv})
        return out
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []


def fetch_series(
    symbol: str, start_ts: int = START_TS, end_ts: int | None = None, interval: str = "1d"
) -> list[dict[str, Any]]:
    """拉日线 → [{date, open, close, high, low}] 升序。网络失败返回 []（调用方降级）。"""
    try:
        params = {
            "period1": start_ts,
            "period2": end_ts or _to_ts(datetime.now()),
            "interval": interval,
        }
        with httpx.Client(
            transport=_transport, timeout=15.0, headers={"User-Agent": "Mozilla/5.0"}
        ) as c:
            r = c.get(CHART_URL + symbol, params=params)
            r.raise_for_status()
            d = r.json()
        res = (d.get("chart", {}).get("result") or [None])[0]
        if not res:
            return []
        ts = res.get("timestamp") or []
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        closes, highs, lows, opens = q.get("close"), q.get("high"), q.get("low"), q.get("open")
        out = []
        for i, t in enumerate(ts):
            c = closes[i] if closes and i < len(closes) else None
            if c is None:
                continue
            out.append(
                {
                    "date": datetime.fromtimestamp(t, __import__("datetime").timezone.utc).strftime(
                        "%Y-%m-%d"
                    ),
                    "open": opens[i] if opens and i < len(opens) else c,
                    "close": c,
                    "high": highs[i] if highs and i < len(highs) else c,
                    "low": lows[i] if lows and i < len(lows) else c,
                }
            )
        return out
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        return []


# ---- 轮级日粒度缓存 + 并发拉取（CC §4.1 修复，2026-08-27）----
# 缓存 key 取日期粒度而非精确 now：daily/evolve 同轮内每题 now 仅秒级差异、
# 结果完全相同，同一自然日只需真实拉取一次；跨日自动失效。
_series_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}
_series_cache_lock = threading.Lock()


def cache_clear() -> None:
    """清空 fetch_series_map 的轮级缓存（测试隔离 / 长时间驻留进程维护用）。"""
    global _series_cache
    with _series_cache_lock:
        _series_cache = {}


def _fetch_one(key: str, cfg: dict[str, Any], end_ts: int, end_str: str) -> list[dict[str, Any]]:
    """拉单个序列。异常上抛，由 fetch_series_map 统一降级为空并标记本轮失败。"""
    if cfg.get("fred"):
        return fetch_fred_series(cfg["fred"], end=end_str)
    if cfg.get("eia"):
        return fetch_eia_series(end=end_str)
    return fetch_series(cfg["symbol"], end_ts=end_ts)


def fetch_series_map(now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """拉全部序列，只保留 ≤ now 的数据（防泄漏）。失败序列留空。

    性能（CC §4.1 修复）：
    - 10 序列用 ThreadPoolExecutor 并发拉取（原串行 ~20.4s → 与最慢单序列相当）；
    - 缓存 key = now 的日期（非精确时刻）：同一天内后续调用零网络直接命中，
      跨日自动失效；拉取失败（任一序列抛异常，或全部序列为空）不写缓存，
      下次调用自动重试。cache_clear() 手动清缓存。
    """
    now = now or datetime.now()
    cache_key = now.date().isoformat()
    with _series_cache_lock:
        cached = _series_cache.get(cache_key)
    if cached is not None:
        return cached
    end_ts = _to_ts(now)
    end_str = now.strftime("%Y-%m-%d")
    out: dict[str, list[dict[str, Any]]] = {}
    failed = 0
    with ThreadPoolExecutor(max_workers=len(SERIES)) as ex:
        futures = {
            ex.submit(_fetch_one, key, cfg, end_ts, end_str): key for key, cfg in SERIES.items()
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                out[key] = fut.result()
            except Exception:
                out[key] = []  # 单序列失败不拖垮整体（保留原降级语义）
                failed += 1
    if failed == 0 and any(out.values()):
        with _series_cache_lock:
            _series_cache.setdefault(cache_key, out)
    return out


def _fmt(x: float, digits: int = 2) -> str:
    return f"{x:.{digits}f}"


def build_series_context(
    series_map: dict[str, list[dict[str, Any]]], now: datetime | None = None
) -> str:
    """把序列压缩成 LLM 可读摘要：全周期统计 + 近 5 年年末 + 近 12 月 + 最新状态。"""
    now = now or datetime.now()
    blocks = []
    for key, cfg in SERIES.items():
        rows = series_map.get(key, [])
        if len(rows) < 30:
            continue
        closes = [r["close"] for r in rows]
        name, unit = cfg["name"], cfg["unit"]
        latest = rows[-1]
        hi = max(r["high"] for r in rows)
        lo = min(r["low"] for r in rows)
        mean = statistics.mean(closes)
        stdev = statistics.stdev(closes) if len(closes) > 1 else 0.0
        # 近 5 年年末（取每年最后一个交易日）
        yearly = []
        for yr in range(now.year - 5, now.year + 1):
            y_rows = [r for r in rows if r["date"].startswith(str(yr))]
            if y_rows:
                yearly.append(f"{yr}年末 {y_rows[-1]['close']:.2f}")
        # 近 12 月每月末
        monthly = []
        for m in range(11, -1, -1):
            ym = now.year * 100 + now.month - m
            y, mo = (ym - 1) // 100, (ym - 1) % 100 + 1
            y_rows = [r for r in rows if r["date"].startswith(f"{y}-{mo:02d}")]
            if y_rows:
                monthly.append(f"{y}-{mo:02d}:{y_rows[-1]['close']:.2f}")
        dist_hi = (latest["close"] / hi - 1) * 100
        blocks.append(
            f"### {name}（{key}）\n"
            f"- 最新收盘（{latest['date']}）：{_fmt(latest['close'])} {unit}；距历史高点 {hi:.2f} 的 "
            f"{dist_hi:+.2f}%，距历史低点 {lo:.2f} 的 +{(latest['close'] / lo - 1) * 100:.1f}%\n"
            f"- 全周期均值 {mean:.2f}，标准差 {stdev:.2f}，历史最高 {hi:.2f}，最低 {lo:.2f}\n"
            f"- 近 5 年年末值：{'、'.join(yearly)}\n"
            f"- 近 12 个月月末值：{'、'.join(monthly)}"
        )
    return "\n\n".join(blocks)


def series_json_cache(series_map: dict[str, list[dict[str, Any]]]) -> str:
    """序列精简 JSON（供 debug/缓存），只保留月频采样。"""
    slim = {}
    for key, rows in series_map.items():
        slim[key] = [{"d": r["date"], "c": r["close"]} for r in rows[::20]]
    return json.dumps(slim, ensure_ascii=False)
