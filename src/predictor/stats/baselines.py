"""统计基线：从真实历史序列算客观基准率（方案 A 核心）。

对每类题用历史数据计算"事件在过去发生的频率"，作为 LLM 预测的统计锚点。
防泄漏：基线只使用预测日之前的历史数据（滚动窗口在预测日截止）。
"""

from __future__ import annotations

import re
from typing import Any

# 题目类型识别（标题关键词 → 基线算法）
# 顺序即优先级：更具体的 pattern 在前（cny_below 带"升破+阈值"比 usdcnh_7 的"人民币兑美元"更具体，
# 否则"离岸人民币兑美元会升破6.90"会先命中 usdcnh_7 的 7.0 阈值）
_PATTERNS = [
    (re.compile(r"标普|S&P|创新高"), "sp500_high"),
    # (?<!美元兑)：排除"美元兑人民币升破7.0"反向句式（该句式汇率数值上涨，方向与
    # cny_below 的 low≤阈值相反，留给既有 usdcnh_7 粗分类）
    (re.compile(r"(?<!美元兑)人民币(?:兑美元)?.*升破\s*(\d+(?:\.\d+)?)"), "cny_below"),
    (re.compile(r"人民币兑美元|汇率|破 ?7\.0"), "usdcnh_7"),
    (re.compile(r"中国.*CPI|CPI.*同比"), "cpi_mom"),
    (re.compile(r"美联储|FOMC|维持利率|降息"), "ffr_meeting"),
    (re.compile(r"EIA|原油库存"), "wti_stock"),
    (re.compile(r"黄金.*突破\s*(\d+)"), "gold_break"),
    (re.compile(r"布伦特.*突破\s*(\d+)"), "brent_break"),
    (re.compile(r"上证.*(收涨|上涨)"), "shanghai_up"),
    (re.compile(r"(道琼斯|道指).*(收涨|上涨)"), "dow_up"),
]


def _classify(title: str) -> str | None:
    for pat, kind in _PATTERNS:
        if pat.search(title):
            return kind
    return None


def _max_close_before(rows: list[dict[str, Any]], idx: int) -> float:
    """rows[0..idx] 的最大收盘价（截至预测日的历史峰值）。"""
    return max(r["close"] for r in rows[: idx + 1])


def baseline_sp500_high(rows: list[dict[str, Any]], window_days: int) -> dict[str, Any] | None:
    """标普"未来 N 天创新高"基准率：在历史序列上滚动统计。

    对每个交易日 t（2016-2021，留出 2022+ 作为"检验期"窗口起点也行——简单起见用全部
    历史 t，只要 t+窗口仍在序列内），统计 [t, t+window] 内最高收盘 > 截至 t 的历史峰值
    的比例。窗口按交易日折算（约 7/14/30 天 → 5/10/21 个交易日）。
    """
    if len(rows) < 500:
        return None
    n_trade = max(1, round(window_days / 7 * 5))  # 7 天 ≈ 5 交易日
    hit = total = 0
    for t in range(100, len(rows) - n_trade):
        peak = _max_close_before(rows, t)
        window_high = max(r["close"] for r in rows[t + 1 : t + n_trade + 1])
        total += 1
        if window_high > peak:
            hit += 1
    if total < 100:
        return None
    return {
        "kind": "sp500_high",
        "window_days": window_days,
        "base_rate": hit / total,
        "n_obs": total,
        "method": f"历史滚动统计：{total} 个窗口（{rows[0]['date']}~{rows[-1]['date']}），"
        f"窗口内最高收盘突破此前历史峰值频率",
    }


def baseline_usdcnh_7(rows: list[dict[str, Any]], window_days: int) -> dict[str, Any] | None:
    """汇率"未来 N 天触及 7.0"基准率：滚动窗口内最低价 ≤ 7.0 的频率。"""
    if len(rows) < 500:
        return None
    n_trade = max(1, round(window_days / 7 * 5))
    hit = total = 0
    for t in range(100, len(rows) - n_trade):
        window_low = min(r["low"] for r in rows[t + 1 : t + n_trade + 1])
        total += 1
        if window_low <= 7.0:
            hit += 1
    if total < 100:
        return None
    return {
        "kind": "usdcnh_7",
        "window_days": window_days,
        "base_rate": hit / total,
        "n_obs": total,
        "method": f"历史滚动统计：{total} 个窗口，窗口内最低价 ≤7.0 频率",
    }


def baseline_breakout(
    rows: list[dict[str, Any]], threshold: float, window_days: int
) -> dict[str, Any] | None:
    """价格"未来 N 天突破阈值"基准率：滚动窗口内最高价 > threshold 的频率。

    对每个交易日 t（跳过前 100 个作为预热），统计 [t+1, t+n_trade] 窗口内 high
    突破阈值的比例。窗口按交易日折算（约 7/14/30 天 → 5/10/21 个交易日）。
    """
    if len(rows) < 500:
        return None
    n_trade = max(1, round(window_days / 7 * 5))  # 7 天 ≈ 5 交易日
    hit = total = 0
    for t in range(100, len(rows) - n_trade):
        window_high = max(r["high"] for r in rows[t + 1 : t + n_trade + 1])
        total += 1
        if window_high > threshold:
            hit += 1
    if total < 100:
        return None
    return {
        "kind": "breakout",
        "threshold": threshold,
        "window_days": window_days,
        "base_rate": hit / total,
        "n_obs": total,
        "method": f"历史滚动统计：{total} 个窗口（{rows[0]['date']}~{rows[-1]['date']}），"
        f"窗口内最高价突破 {threshold:g} 的频率",
    }


def baseline_next_day_up(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """指数"次日收涨"基线：相邻两日收盘上涨（次日 > 当日）的频率。"""
    if len(rows) < 100:
        return None
    up = total = 0
    for i in range(1, len(rows)):
        total += 1
        if rows[i]["close"] > rows[i - 1]["close"]:
            up += 1
    return {
        "kind": "next_day_up",
        "base_rate": up / total,
        "n_obs": total,
        "method": f"历史日频统计：{total} 个交易日（{rows[0]['date']}~{rows[-1]['date']}），"
        f"次日收盘上涨频率",
    }


def baseline_cny_below(
    rows: list[dict[str, Any]], threshold: float, window_days: int
) -> dict[str, Any] | None:
    """汇率"未来 N 天触及阈值以下"基准率：滚动窗口内最低价 ≤ threshold 的频率。"""
    if len(rows) < 500:
        return None
    n_trade = max(1, round(window_days / 7 * 5))
    hit = total = 0
    for t in range(100, len(rows) - n_trade):
        window_low = min(r["low"] for r in rows[t + 1 : t + n_trade + 1])
        total += 1
        if window_low <= threshold:
            hit += 1
    if total < 100:
        return None
    return {
        "kind": "cny_below",
        "threshold": threshold,
        "window_days": window_days,
        "base_rate": hit / total,
        "n_obs": total,
        "method": f"历史滚动统计：{total} 个窗口，窗口内最低价 ≤{threshold:g} 频率",
    }


def baseline_cpi_mom(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """中国 CPI"同比高于上月"基线：历史月环比上升的频率（同比口径由 LLM 结合上下文处理，
    环比上升频率是外部视角的合理近似——同比变化方向与环比强相关）。"""
    if len(rows) < 24:
        return None
    closes = [r["close"] for r in rows]
    up = total = 0
    for i in range(1, len(closes)):
        total += 1
        if closes[i] > closes[i - 1]:
            up += 1
    if total < 12:
        return None
    return {
        "kind": "cpi_mom",
        "base_rate": up / total,
        "n_obs": total,
        "method": f"历史月环比统计：{total} 个月份（{rows[0]['date']}~{rows[-1]['date']}），"
        f"CPI 月环比上升频率（同比口径的保守近似）",
    }


def baseline_ffr_meeting(rows: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    """美联储利率题基线：历史"相邻月末利率未变"频率（近似 FOMC 会议维持利率的先验）。

    DFF 为日频——先按月份聚合取**月末值**，再比较相邻月末（月末间变化 = 当月会议动了利率）。
    """
    if len(rows) < 24:
        return None
    # 按月份聚合：每月最后一个观测值
    month_end: list[float] = []
    cur_key = None
    for r in rows:
        key = r["date"][:7]
        if key != cur_key:
            month_end.append(r["close"])
            cur_key = key
        else:
            month_end[-1] = r["close"]  # 同月覆盖为最新
    hold = down = 0
    for i in range(1, len(month_end)):
        diff = month_end[i] - month_end[i - 1]
        # 阈值 5bp：DFF 有效利率日间波动 1-5bp 属常态；FOMC 调整通常 25bp
        if abs(diff) < 0.05:
            hold += 1
        elif diff < -0.05:
            down += 1
    total = len(month_end) - 1
    if total < 12:
        return None
    if "降息" in title:
        rate, name = down / total, "利率下调"
    else:
        rate, name = hold / total, "利率维持不变"
    return {
        "kind": "ffr_meeting",
        "base_rate": rate,
        "n_obs": total,
        "method": f"历史月末统计：{total} 个月（{rows[0]['date']}~{rows[-1]['date']}），"
        f"联邦基金利率月末值{name}的频率（近似 FOMC 决议）",
    }


def baseline_wti_stock(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """EIA"本周原油库存下降"基线：历史周度库存环比下降频率。"""
    if len(rows) < 52:
        return None
    closes = [r["close"] for r in rows]
    down = total = 0
    for i in range(1, len(closes)):
        total += 1
        if closes[i] < closes[i - 1]:
            down += 1
    if total < 26:
        return None
    return {
        "kind": "wti_stock",
        "base_rate": down / total,
        "n_obs": total,
        "method": f"历史周度统计：{total} 周（{rows[0]['date']}~{rows[-1]['date']}），"
        f"原油商业库存环比下降频率",
    }


def compute_baseline(
    title: str, series_map: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """按题类型算基线；无匹配类型或数据不足返回 None（调用方降级，不阻塞预测）。"""
    kind = _classify(title)
    if kind is None:
        return None
    if kind == "sp500_high":
        m = re.search(r"(\d+)\s*天", title)
        window = int(m.group(1)) if m else 7
        return baseline_sp500_high(series_map.get("sp500", []), window)
    if kind == "usdcnh_7":
        m = re.search(r"(\d+)\s*天", title)
        window = int(m.group(1)) if m else 30
        return baseline_usdcnh_7(series_map.get("usdcnh", []), window)
    if kind == "cpi_mom":
        return baseline_cpi_mom(series_map.get("cpi_cn", []))
    if kind == "ffr_meeting":
        return baseline_ffr_meeting(series_map.get("ffr", []), title)
    if kind == "wti_stock":
        return baseline_wti_stock(series_map.get("wti_stock", []))
    if kind == "gold_break":
        m = re.search(r"黄金.*突破\s*(\d+)", title)
        threshold = float(m.group(1)) if m else None
        if threshold is None:
            return None
        wm = re.search(r"(\d+)\s*天", title)
        window = int(wm.group(1)) if wm else 7
        result = baseline_breakout(series_map.get("gold", []), threshold, window)
        if result is not None:
            result["kind"] = kind
        return result
    if kind == "brent_break":
        m = re.search(r"布伦特.*突破\s*(\d+)", title)
        threshold = float(m.group(1)) if m else None
        if threshold is None:
            return None
        wm = re.search(r"(\d+)\s*天", title)
        window = int(wm.group(1)) if wm else 30
        result = baseline_breakout(series_map.get("brent", []), threshold, window)
        if result is not None:
            result["kind"] = kind
        return result
    if kind == "shanghai_up":
        result = baseline_next_day_up(series_map.get("shanghai", []))
        if result is not None:
            result["kind"] = kind
        return result
    if kind == "dow_up":
        result = baseline_next_day_up(series_map.get("dow", []))
        if result is not None:
            result["kind"] = kind
        return result
    if kind == "cny_below":
        m = re.search(r"(?<!美元兑)人民币(?:兑美元)?.*升破\s*(\d+(?:\.\d+)?)", title)
        threshold = float(m.group(1)) if m else None
        if threshold is None:
            return None
        wm = re.search(r"(\d+)\s*天", title)
        window = int(wm.group(1)) if wm else 30
        result = baseline_cny_below(series_map.get("usdcnh", []), threshold, window)
        if result is not None:
            result["kind"] = kind
        return result
    return None
