"""统计基线：从真实历史序列算客观基准率（方案 A 核心）。

对每类题用历史数据计算"事件在过去发生的频率"，作为 LLM 预测的统计锚点。
防泄漏：基线只使用预测日之前的历史数据（滚动窗口在预测日截止）。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from typing import Any

# 题目类型识别（标题关键词 → 基线算法）
# 顺序即优先级：更具体的 pattern 在前（cny_below 带"升破+阈值"比 usdcnh_7 的"人民币兑美元"更具体，
# 否则"离岸人民币兑美元会升破6.90"会先命中 usdcnh_7 的 7.0 阈值）。
# CC §2.2 修复（2026-08-27）：
#  - sp500_break：标普"站上/突破 N 点"阈值题走 breakout 算法（原误走创新高算法）；
#  - sp500_high 必须带标普主体：裸"创新高"（如"淘宝成交额创历史新高"）不再映射标普；
#  - cpi_mom 两支都要求「中国」：排除"美国 CPI 同比"误注入中国序列（CHNCPIALLMINMEI）；
#  - ffr_meeting 只认美联储语境（防"某国央行降息/维持利率"误注入 DFF），方向三支
#    （加息/降息/维持）在 baseline_ffr_meeting 内按题面判定。
_PATTERNS = [
    (
        re.compile(r"(?:标普|S&P|SPX|标准普尔).*(?:站上|突破|升破|触及)\s*(\d+(?:\.\d+)?)"),
        "sp500_break",
    ),
    (re.compile(r"(?:标普|S&P|SPX|标准普尔).*(?:创新高|新高)"), "sp500_high"),
    # (?<!美元兑)：排除"美元兑人民币升破7.0"反向句式（该句式汇率数值上涨，方向与
    # cny_below 的 low≤阈值相反，留给既有 usdcnh_7 粗分类）
    (re.compile(r"(?<!美元兑)人民币(?:兑美元)?.*升破\s*(\d+(?:\.\d+)?)"), "cny_below"),
    (re.compile(r"人民币兑美元|汇率|破 ?7\.0"), "usdcnh_7"),
    (re.compile(r"中国.*CPI|CPI.*同比.*中国"), "cpi_mom"),
    (re.compile(r"美联储|FOMC"), "ffr_meeting"),
    (re.compile(r"EIA|原油库存"), "wti_stock"),
    (re.compile(r"黄金.*突破\s*(\d+)"), "gold_break"),
    (re.compile(r"布伦特.*突破\s*(\d+)"), "brent_break"),
    (re.compile(r"上证.*(收涨|上涨)"), "shanghai_up"),
    (re.compile(r"(道琼斯|道指).*(收涨|上涨)"), "dow_up"),
]

# CC §2.2 宁缺毋滥：窗口 >90 天的题不注入基线（低频历史频率外推失真，且与
# 月/年底口径题的真正剩余窗口偏差太大——"年底前升破 7.0"≈126 天、"标普首次
# 站上 8500"≈157 天，注入 7/30 天窗口频率是系统性错锚点）。
_MAX_WINDOW_DAYS = 90


def _classify(title: str) -> str | None:
    for pat, kind in _PATTERNS:
        if pat.search(title):
            return kind
    return None


def _end_of_month(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _parse_title_window(title: str, now: datetime) -> int | None:
    """题面窗口措辞 → 天数。支持绝对日期/月底/年底/月-日/数量单位（天周月年）。

    无法从题面识别窗口返回 None（调用方走 closes_at 兜底或宁缺毋滥降级）。
    """
    today = now.date()
    # 1) 绝对日期：2027年1月31日前 / 2027年1月31日
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", title)
    if m:
        try:
            return (date(int(m.group(1)), int(m.group(2)), int(m.group(3))) - today).days
        except ValueError:
            return None
    # 2) 带年份的月底：2026年10月底前
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(?:底|末)", title)
    if m:
        try:
            return (_end_of_month(date(int(m.group(1)), int(m.group(2)), 1)) - today).days
        except ValueError:
            return None
    # 3) 带年份的年底/年末/年内：2026年底前
    m = re.search(r"(\d{4})\s*年\s*(?:底|末|内)", title)
    if m:
        return (date(int(m.group(1)), 12, 31) - today).days
    # 4) 不带年份的月-日：9月30日前（已过则取明年同月日）
    m = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日", title)
    if m:
        mo, day = int(m.group(1)), int(m.group(2))
        for year in (today.year, today.year + 1):
            try:
                target = date(year, mo, day)
            except ValueError:
                return None
            if target >= today:
                return (target - today).days
        return None
    # 5) 不带年份的月底：10月底前 / 月底前 / 本月底
    m = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(?:底|末)", title)
    if m:
        mo = int(m.group(1))
        try:
            target = _end_of_month(date(today.year, mo, 1))
        except ValueError:
            return None
        if target < today:
            target = _end_of_month(date(today.year + 1, mo, 1))
        return (target - today).days
    if re.search(r"(?:本?月底|月末)", title):
        return (_end_of_month(today) - today).days
    # 6) 数量单位：N天/日/周/星期/月/年。年计数 >5 视为日历年份措辞残留（如
    # "2026 年 9 月"被误读为"26 年"），不按数量解析。
    m = re.search(r"(\d{1,3})\s*个?\s*(天|日|周|星期|月|年)", title)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "年" and n > 5:
            return None
        return {
            "天": n,
            "日": n,
            "周": n * 7,
            "星期": n * 7,
            "月": n * 30,
            "年": n * 365,
        }[unit]
    # 7) 裸年底/年末/年内/今年（无年份、无计数，如"年底前"；须在数量单位之后，
    # 否则"未来1年内"的"年内"会被误当年底口径）
    if re.search(r"(?:今年|年底|年末|年内)", title):
        return (date(today.year, 12, 31) - today).days
    return None


def _resolve_window(title: str, now: datetime, closes_at: datetime | None) -> int | None:
    """解析题面窗口 → 天数；题面无措辞时回退 closes_at-now；>90 天或无法确定 → None。"""
    days = _parse_title_window(title, now)
    if days is not None:
        return days if 1 <= days <= _MAX_WINDOW_DAYS else None
    if closes_at is not None:
        try:
            d = (closes_at - now).days
        except TypeError:  # tz 混用等异常输入 → 无法确定
            return None
        if d < 0:
            return None
        d = max(d, 1)
        return d if d <= _MAX_WINDOW_DAYS else None
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
    """美联储利率题基线：历史"相邻月末利率变化方向"频率（近似 FOMC 决议先验）。

    CC §2.2 修复：方向三支按题面判定——加息/升息/上调 → 上调频率；降息/下调 →
    下调频率；维持/不变/按兵不动 → 维持频率；方向不明 → None（宁缺毋滥，不再
    一律注入"维持利率不变"，那会给"9 月会加息吗"类题方向性误导锚点）。

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
    hold = up = down = 0
    for i in range(1, len(month_end)):
        diff = month_end[i] - month_end[i - 1]
        # 阈值 5bp：DFF 有效利率日间波动 1-5bp 属常态；FOMC 调整通常 25bp
        if abs(diff) < 0.05:
            hold += 1
        elif diff > 0.05:
            up += 1
        else:
            down += 1
    total = len(month_end) - 1
    if total < 12:
        return None
    if re.search(r"加息|升息|上调", title):
        rate, name = up / total, "利率上调"
    elif re.search(r"降息|下调", title):
        rate, name = down / total, "利率下调"
    elif re.search(r"维持|不变|按兵不动", title):
        rate, name = hold / total, "利率维持不变"
    else:
        return None
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
    title: str,
    series_map: dict[str, list[dict[str, Any]]],
    *,
    now: datetime | None = None,
    closes_at: datetime | None = None,
) -> dict[str, Any] | None:
    """按题类型算基线；无匹配类型或数据不足返回 None（调用方降级，不阻塞预测）。

    now/closes_at 用于窗口解析（CC §2.2 修复后窗口不再是硬编码默认值）：题面有
    明确窗口措辞（天/周/月/年/月底/年底/绝对日期）用之；否则回退 closes_at-now；
    两者都无法确定或窗口 >90 天 → None（宁缺毋滥，不注入错误频率的锚点）。
    """
    kind = _classify(title)
    if kind is None:
        return None
    now = now or datetime.now()
    if kind == "sp500_high":
        window = _resolve_window(title, now, closes_at)
        if window is None:
            return None
        return baseline_sp500_high(series_map.get("sp500", []), window)
    if kind == "sp500_break":
        m = re.search(r"(?:标普|S&P|SPX|标准普尔).*(?:站上|突破|升破|触及)\s*(\d+(?:\.\d+)?)", title)
        threshold = float(m.group(1)) if m else None
        if threshold is None:
            return None
        window = _resolve_window(title, now, closes_at)
        if window is None:
            return None
        result = baseline_breakout(series_map.get("sp500", []), threshold, window)
        if result is not None:
            result["kind"] = kind
        return result
    if kind == "usdcnh_7":
        window = _resolve_window(title, now, closes_at)
        if window is None:
            return None
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
        window = _resolve_window(title, now, closes_at)
        if window is None:
            return None
        result = baseline_breakout(series_map.get("gold", []), threshold, window)
        if result is not None:
            result["kind"] = kind
        return result
    if kind == "brent_break":
        m = re.search(r"布伦特.*突破\s*(\d+)", title)
        threshold = float(m.group(1)) if m else None
        if threshold is None:
            return None
        window = _resolve_window(title, now, closes_at)
        if window is None:
            return None
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
        window = _resolve_window(title, now, closes_at)
        if window is None:
            return None
        result = baseline_cny_below(series_map.get("usdcnh", []), threshold, window)
        if result is not None:
            result["kind"] = kind
        return result
    return None
