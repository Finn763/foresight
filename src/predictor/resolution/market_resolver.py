"""A 类（数值行情）揭晓：双源一致性 → T+1 确认 → 备源挂时合理性兜底 → 降级人工。
判定条件（P0）：gt_prev_close / gt_threshold / lt_threshold。

流程（resolve）：
1. 截止未到 → None；宽限窗口（grace_days，默认 3 天）内重试，超窗 → None（调用方降级）
2. 非 Asia 时区（美股/欧盘）T+1 确认：now < closes_at + 1 天（= closes 次日 09:00 北京）不判定
3. 双源取价：两价差 <0.5%（默认容差）→ 用主源；双源都可用但差 ≥0.5% → 无条件降级 None
   （T4 裁定：双源分歧 → 降级人工不猜，不做合理性检查）
4. 备源获取失败（异常）→ 单源 + 合理性兜底：主源偏离昨收 >3% 且无 spec.sanity_exempt
   免责 → 降级不猜（这是"备源挂"路径的设计）
5. 条件判定：gt_prev_close 需昨收（sina/tencent 各端点取法见 quotes.py）、gt/lt_threshold
"""

from datetime import datetime, timedelta

# instrument → {provider: symbol} 规范化映射。
# Task 5/7 生成 resolution_spec 时据此填 source_primary/source_backup 与
# compare_symbol/backup_symbol；本类在 spec 缺 backup_symbol 时据此推导备源 symbol。
# （不做 symbol→providers 倒排表：spx 双源 symbol 不同，倒排会失真——gb_$inx 只新浪可拉。）
# None = provider 用固定 URL（coingecko/coinbase），无 symbol 概念。
SYMBOL_MAP = {
    "spx": {"sina": "gb_$inx", "tencent": "usINX"},
    "dji": {"sina": "gb_$dji", "tencent": "usDJI"},
    "sh000001": {"sina": "s_sh000001", "tencent": "s_sh000001"},
    "gold": {"sina": "hf_GC"},
    "brent": {"sina": "hf_OIL"},
    "cnh": {"sina": "fx_susdcnh"},
    "btc": {"coingecko": None, "coinbase": None},
}


class MarketResolver:
    """双源取价 → 一致性 → 分歧降级 / 备源挂时合理性兜底 → 条件判定 → None 降级。"""

    def __init__(
        self,
        fetch_close,
        fetch_prev_close=None,
        *,
        fetch_kline=None,
        diff_tolerance: float = 0.005,
        sanity_drift: float = 0.03,
        record_margin: float = 0.002,
        symbol_map: dict | None = None,
    ):
        self._fetch = fetch_close
        self._fetch_prev = fetch_prev_close
        self._fetch_kline = fetch_kline
        self._tol = diff_tolerance
        self._sanity = sanity_drift
        self._record_margin = record_margin
        self._symbol_map = symbol_map if symbol_map is not None else SYMBOL_MAP

    def resolve(self, question, spec: dict, now: datetime) -> tuple[bool, str] | None:
        """返回 (outcome, source)；无法判定（数据未定/分歧降级/超窗）返回 None。"""
        if now < question.closes_at:
            return None  # 截止未到
        if now > question.closes_at + timedelta(days=spec.get("grace_days", 3)):
            return None  # 宽限已过 → 调用方降级
        # 数据窗口纪律（8-14 预演前对抗审计 P1）：行情快照只在事件发生后、下一交易日
        # 开市前有效，窗口外一律不判定。宽限期内跨日重试只会取到"下一交易日收盘"
        # 的快照——把题判成另一天的事件、静默错判永久入账（#67：8-14 16:30 揭晓失败
        # 若 8-15 重试，拿 8-14 收盘 vs 8-13 收盘判"8-13 收 > 8-12 收"的题）。
        # 非 Asia：事件在 closes 次日凌晨收盘（美股 16:00 ET = 次日 04:00 北京），
        #   有效窗口 [closes+1d, closes+2d)（保留 T+1 下限：开闸前恒 None）
        # Asia：事件当日 15:00 收盘，有效窗口 [closes, closes+1d)
        if spec.get("close_timezone") != "Asia/Shanghai":
            window_lo = question.closes_at + timedelta(days=1)
        else:
            window_lo = question.closes_at
        if now < window_lo or now >= window_lo + timedelta(days=1):
            return None
        try:
            return self._resolve_close(question, spec, now)
        except Exception:
            return None  # 取价/解析异常 → 降级（不猜）

    def _resolve_close(self, question, spec, now) -> tuple[bool, str] | None:
        cond = spec["condition"]
        if cond == "record_high":
            return self._resolve_record_high(question, spec)
        primary = (spec["source_primary"], spec.get("compare_symbol"))
        p_price = self._fetch(*primary)
        backup_provider = spec.get("source_backup")
        backup_sym = self._backup_symbol(spec)
        if backup_provider and backup_sym:
            try:
                b_price = self._fetch(backup_provider, backup_sym)
                if abs(b_price - p_price) / max(p_price, 1e-9) > self._tol:
                    return None  # T4 裁定：双源都可用但分歧 → 无条件降级人工（不猜）
            except Exception:
                # 备源获取失败 → 单源 + 合理性兜底（偏离昨收 > sanity_drift 且无免责 → 降级）
                if not self._sanity_ok(spec, *primary, p_price):
                    return None
        cond = spec["condition"]
        if cond == "gt_threshold":
            return (p_price > spec["value"], primary[0])
        if cond == "lt_threshold":
            return (p_price < spec["value"], primary[0])
        if cond == "gt_prev_close":
            prev = self._fetch_prev_close(primary[0], primary[1])
            if prev is None:
                return None
            return (p_price > prev, primary[0])
        return None

    def _resolve_record_high(self, question, spec) -> tuple[bool, str] | None:
        """创新高判定（收盘价口径）：窗口内收盘 max > 窗口前历史最高收盘。

        双源：主源 K 线判 verdict；备源判出相反结论 → 分歧降级 None。
        备源挂 → 单源 + 刀口兜底（创新高幅度 < record_margin 不猜）。
        """
        if self._fetch_kline is None:
            return None
        primary = (spec["source_primary"], spec.get("compare_symbol"))
        try:
            bars = self._fetch_kline(*primary)
        except Exception:
            return None
        verdict, margin = self._judge_record_high(bars, question)
        if verdict is None:
            return None
        backup_provider = spec.get("source_backup")
        backup_sym = self._backup_symbol(spec)
        if backup_provider and backup_sym:
            try:
                b_bars = self._fetch_kline(backup_provider, backup_sym)
                b_verdict, _ = self._judge_record_high(b_bars, question)
                if b_verdict is not None and b_verdict != verdict:
                    return None  # 双源分歧 → 降级人工（不猜）
            except Exception:
                if margin < self._record_margin:
                    return None  # 备源挂 + 刀口幅度 → 不猜
        return (verdict, primary[0])

    def _judge_record_high(self, bars, question) -> tuple[bool | None, float]:
        """bars: [(date, close)]。返回 (verdict, margin)；数据不足 → (None, 0)。"""
        lo, hi = question.opens_at.date(), question.closes_at.date()
        if not bars:
            return None, 0.0
        dates = [d for d, _ in bars]
        if max(dates) < hi:
            return None, 0.0  # 窗口数据未齐 → 宽限内重试
        min_date = min(dates)
        prior = [c for d, c in bars if d < lo]
        window = [c for d, c in bars if lo <= d <= hi]
        if not window or not prior:
            return None, 0.0
        prior_max = max(prior)
        if min_date in [d for d, c in bars if d < lo and c == prior_max]:
            return None, 0.0  # 前高在序列最早 bar → 历史截断风险（不猜）
        win_max = max(window)
        margin = win_max / prior_max - 1.0
        return (win_max > prior_max, margin)

    def _backup_symbol(self, spec) -> str | None:
        sym = spec.get("backup_symbol")
        if sym:
            return sym
        inst = spec.get("instrument")
        prov = spec.get("source_backup")
        if inst and prov:
            return self._symbol_map.get(inst, {}).get(prov)
        return None

    def _sanity_ok(self, spec, provider, symbol, price) -> bool:
        """合理性兜底：主源价偏离昨收 ≤ sanity_drift(3%) 或 spec 免责 → 可采信。"""
        if spec.get("sanity_exempt"):
            return True  # 免责（如新品/异常行情无昨收对标）
        prev = self._fetch_prev_close(provider, symbol)
        if prev is None:
            return False  # 无昨收对照 → 无法兜底 → 降级
        return abs(price - prev) / max(prev, 1e-9) <= self._sanity

    def _fetch_prev_close(self, provider, symbol) -> float | None:
        # 昨收：sina gb_→parts[26] / fx_→parts[8]；tencent us*→parts[4] / s_*→现价-涨跌额
        if self._fetch_prev is None:
            return None
        try:
            return self._fetch_prev(provider, symbol)
        except Exception:
            return None
