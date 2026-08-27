"""CC §4.1 修复回归测试：fetch_series_map 轮级日粒度缓存 + 10 序列并发拉取。

全部离线（httpx.MockTransport + monkeypatch 假 key），验证：
- 缓存命中：同一天第二次调用零 HTTP（请求总数保持 10）
- 跨日期失效：次日首次调用重新拉取
- 失败不缓存：网络错误 / 拉取器异常后，下次调用自动重试
- cache_clear 强制失效
- 并发正确性：最大同时在途请求 ≥ 5 + 各序列数据归属正确
"""

import threading
import time
from datetime import UTC, datetime
from urllib.parse import unquote

import httpx
import pytest

from predictor.stats import historical
from predictor.stats.historical import cache_clear, fetch_series_map

# 各序列期望基准值（handler 据此生成可辨识数据，用于归属校验）
YAHOO_BASE = {
    "^GSPC": 6000.0,
    "CNY=X": 7.1,
    "GC=F": 5000.0,
    "BZ=F": 90.0,
    "000001.SS": 3500.0,
    "^DJI": 45000.0,
}
FRED_BASE = {"CHNCPIALLMINMEI": 101.0, "DFF": 4.0, "WCOILWTICO": 70.0}

EXPECTED_LAST = {
    "sp500": 6020.0,
    "usdcnh": 27.1,
    "gold": 5020.0,
    "brent": 110.0,
    "shanghai": 3520.0,
    "dow": 45020.0,
    "cpi_cn": 103.0,
    "ffr": 6.0,
    "wti_price": 72.0,
    "wti_stock": 440002.0,
}


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch):
    """隔离：清缓存、注入假 key、测试后还原 transport（避免跨测试/跨文件污染）。"""
    cache_clear()
    monkeypatch.setattr(historical, "_fred_key", lambda: "test-fred-key")
    monkeypatch.setattr(historical, "_eia_key", lambda: "test-eia-key")
    yield
    cache_clear()
    historical.set_transport(None)


def _make_handler(requests: list, delay: float = 0.0, fail: bool = False):
    """MockTransport handler：按 host 分发三源、记录请求、支持延时与故障注入。"""
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            requests.append(str(request.url))
        if delay:
            time.sleep(delay)
        if fail:
            raise httpx.ConnectError("net down", request=request)
        host = request.url.host
        if host == "query1.finance.yahoo.com":
            symbol = unquote(request.url.path.rsplit("/", 1)[-1])
            base = YAHOO_BASE[symbol]
            ts = [int(datetime(2026, 8, 20 + i, tzinfo=UTC).timestamp()) for i in range(3)]
            closes = [base + 10 * i for i in range(3)]
            payload = {
                "chart": {
                    "result": [
                        {
                            "timestamp": ts,
                            "indicators": {
                                "quote": [
                                    {"close": closes, "open": closes, "high": closes, "low": closes}
                                ]
                            },
                        }
                    ]
                }
            }
            return httpx.Response(200, json=payload)
        if host == "api.stlouisfed.org":
            series_id = request.url.params["series_id"]
            base = FRED_BASE[series_id]
            payload = {
                "observations": [
                    {"date": f"2026-07-0{i + 1}", "value": f"{base + i}"} for i in range(3)
                ]
            }
            return httpx.Response(200, json=payload)
        if host == "api.eia.gov":
            payload = {
                "response": {
                    "data": [
                        {"period": f"2026-07-0{i + 4}", "value": str(440000 + i)} for i in range(3)
                    ]
                }
            }
            return httpx.Response(200, json=payload)
        raise AssertionError(f"unexpected url {request.url}")

    return handler


def _install_ok_transport(requests: list, delay: float = 0.0) -> None:
    historical.set_transport(httpx.MockTransport(_make_handler(requests, delay=delay)))


def test_cache_hit_same_day_no_extra_http():
    """同一天两次调用：第二次零 HTTP（请求数保持 10），返回同一缓存对象。"""
    requests: list[str] = []
    _install_ok_transport(requests)
    sm1 = fetch_series_map(datetime(2026, 8, 27, 9, 0))
    assert len(requests) == 10
    assert sm1["sp500"][-1]["close"] == EXPECTED_LAST["sp500"]
    sm2 = fetch_series_map(datetime(2026, 8, 27, 21, 30))
    assert len(requests) == 10  # 无新增请求
    assert sm2 is sm1


def test_cache_invalidates_across_dates():
    """跨日期失效：次日首次调用重新拉取；次日当天内仍命中。"""
    requests: list[str] = []
    _install_ok_transport(requests)
    fetch_series_map(datetime(2026, 8, 27, 23, 59))
    fetch_series_map(datetime(2026, 8, 28, 0, 1))
    assert len(requests) == 20
    fetch_series_map(datetime(2026, 8, 28, 8, 0))
    assert len(requests) == 20


def test_network_error_not_cached_retry():
    """网络失败（全部序列空）→ 不写缓存，同日下次调用重新拉取。"""
    requests: list[str] = []
    historical.set_transport(httpx.MockTransport(_make_handler(requests, fail=True)))
    sm1 = fetch_series_map(datetime(2026, 8, 29, 9, 0))
    assert len(requests) == 10
    assert all(not rows for rows in sm1.values())  # 全空 → 调用方降级
    historical.set_transport(httpx.MockTransport(_make_handler(requests)))
    sm2 = fetch_series_map(datetime(2026, 8, 29, 10, 0))
    assert len(requests) == 20  # 失败未缓存 → 重新拉取
    assert sm2["sp500"][-1]["close"] == EXPECTED_LAST["sp500"]


def test_fetcher_exception_not_cached(monkeypatch):
    """任一序列抛异常 → 该序列空、本轮不缓存；恢复后下次调用正常重试。"""
    requests: list[str] = []
    _install_ok_transport(requests)

    def boom(*a, **kw):
        raise RuntimeError("net down")

    with monkeypatch.context() as m:
        m.setattr(historical, "fetch_series", boom)
        m.setattr(historical, "fetch_fred_series", boom)
        m.setattr(historical, "fetch_eia_series", boom)
        sm1 = fetch_series_map(datetime(2026, 8, 30, 9, 0))
    assert all(not rows for rows in sm1.values())
    assert len(requests) == 0  # 异常发生在 HTTP 之前
    sm2 = fetch_series_map(datetime(2026, 8, 30, 10, 0))
    assert len(requests) == 10  # 失败未缓存 → 恢复后正常拉取
    assert sm2["sp500"][-1]["close"] == EXPECTED_LAST["sp500"]


def test_cache_clear_forces_refetch():
    """cache_clear() 后同日再次调用强制重新拉取。"""
    requests: list[str] = []
    _install_ok_transport(requests)
    fetch_series_map(datetime(2026, 8, 31, 9, 0))
    fetch_series_map(datetime(2026, 8, 31, 9, 5))
    assert len(requests) == 10
    cache_clear()
    fetch_series_map(datetime(2026, 8, 31, 9, 10))
    assert len(requests) == 20


def test_concurrent_fetch_parallel_and_data_integrity():
    """10 序列真并发（最大同时在途 ≥ 5）+ 并发下各序列数据归属正确。"""
    requests: list[str] = []
    inflight = 0
    max_inflight = 0
    lock = threading.Lock()
    inner = _make_handler(requests, delay=0.1)

    def handler(request):
        nonlocal inflight, max_inflight
        with lock:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
        try:
            return inner(request)
        finally:
            with lock:
                inflight -= 1

    historical.set_transport(httpx.MockTransport(handler))
    sm = fetch_series_map(datetime(2026, 9, 1, 9, 0))
    assert max_inflight >= 5  # 串行实现此处只会是 1
    assert len(requests) == 10
    for key, expected in EXPECTED_LAST.items():
        rows = sm[key]
        assert rows, key
        assert rows[-1]["close"] == expected
    # 该轮成功 → 已写缓存：同日二次调用零新增请求
    fetch_series_map(datetime(2026, 9, 1, 20, 0))
    assert len(requests) == 10
