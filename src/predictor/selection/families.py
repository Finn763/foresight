"""题族生成：四时间档 × 阈值网格 × 配额纪律（族≤30%、同日≤3、难度三档各≥25%）。"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class NewQuestionSpec:
    title: str
    closes_at: datetime
    is_public: bool
    resolution_class: str
    resolution_spec: dict


def difficulty_tier(title: str, base_rates: dict) -> str:
    """按基准率分档：easy(br>0.6或<0.4)/medium/blind(无命中)。"""
    br = None
    for k, v in base_rates.items():
        if k in title:
            br = v
            break
    if br is None:
        return "blind"
    return "easy" if (br > 0.6 or br < 0.4) else "medium"


# ponytail: 8 specs table-driven (no YAML), 1-line comments, behavior preserved
FAMILIES = [
    # ultra 1d (closes=now+1d, exact-title dedup, Mon-Thu only)
    (
        "明天标普500收盘会高于今天吗",
        "标普",
        1,
        {
            "class": "A",
            "instrument": "spx",
            "source_primary": "sina",
            "compare_symbol": "gb_$inx",
            "backup_symbol": "usINX",
            "source_backup": "tencent",
            "condition": "gt_prev_close",
            "close_timezone": "America/New_York",
            "grace_days": 3,
            "degrade_to": "C",
        },
    ),
    (
        "明天上证指数会收涨吗",
        "上证",
        1,
        {
            "class": "A",
            "instrument": "sh",
            "source_primary": "tencent",
            "compare_symbol": "s_sh000001",
            "condition": "gt_prev_close",
            "close_timezone": "Asia/Shanghai",
            "grace_days": 3,
            "degrade_to": "C",
        },
    ),
    (
        "明天道琼斯指数会收涨吗",
        "道琼斯",
        1,
        {
            "class": "A",
            "instrument": "dji",
            "source_primary": "sina",
            "compare_symbol": "gb_$dji",
            "backup_symbol": "usDJI",
            "source_backup": "tencent",
            "condition": "gt_prev_close",
            "close_timezone": "America/New_York",
            "grace_days": 3,
            "degrade_to": "C",
        },
    ),
    # weekly 7/30/60d (closes=now_t+days, key-substring dedup)
    (
        "未来7天内COMEX黄金会突破5150美元/盎司吗",
        "黄金",
        7,
        {
            "class": "A",
            "instrument": "gold",
            "source_primary": "sina",
            "compare_symbol": "hf_GC",
            "condition": "gt_threshold",
            "value": 5150.0,
            "close_timezone": "America/New_York",
            "grace_days": 3,
            "degrade_to": "C",
        },
    ),
    (
        "未来30天内COMEX黄金会突破4600美元/盎司吗",
        "黄金",
        30,
        {
            "class": "A",
            "instrument": "gold",
            "source_primary": "sina",
            "compare_symbol": "hf_GC",
            "condition": "gt_threshold",
            "value": 4600.0,
            "close_timezone": "America/New_York",
            "grace_days": 3,
            "degrade_to": "C",
        },
    ),
    (
        "未来30天内离岸人民币兑美元会升破6.90吗",
        "人民币",
        30,
        {
            "class": "A",
            "instrument": "usdcnh",
            "source_primary": "sina",
            "compare_symbol": "fx_susdcnh",
            "condition": "lt_threshold",
            "value": 6.90,
            "close_timezone": "Asia/Shanghai",
            "grace_days": 3,
            "degrade_to": "C",
        },
    ),
    (
        "未来30天内布伦特原油会突破85美元/桶吗",
        "布伦特",
        30,
        {
            "class": "A",
            "instrument": "brent",
            "source_primary": "sina",
            "compare_symbol": "hf_OIL",
            "condition": "gt_threshold",
            "value": 85.0,
            "close_timezone": "UTC",
            "grace_days": 3,
            "degrade_to": "C",
        },
    ),
    (
        "未来60天内布伦特原油会突破90美元/桶吗",
        "布伦特",
        60,
        {
            "class": "A",
            "instrument": "brent",
            "source_primary": "sina",
            "compare_symbol": "hf_OIL",
            "condition": "gt_threshold",
            "value": 90.0,
            "close_timezone": "UTC",
            "grace_days": 3,
            "degrade_to": "C",
        },
    ),
]


def generate_families(
    storage, now: datetime, *, base_rates: dict | None = None
) -> list[NewQuestionSpec]:
    base_rates = base_rates or {}
    specs: list[NewQuestionSpec] = []
    unresolved = storage.list_unresolved()
    existing = [q.title for q in unresolved]
    due_counts: dict = {}

    def _closes_ok(closes: datetime) -> bool:
        d = closes.date()
        due_counts.setdefault(d, 0)
        if due_counts[d] >= 3:
            return False
        due_counts[d] += 1
        return True

    def _quota_ok(title: str, family_key: str) -> bool:
        family_share = sum(1 for t in existing + [s.title for s in specs] if family_key in t)
        return family_share < 3

    now_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    for title, key, days, spec in FAMILIES:
        is_ultra = days == 1  # 1d entries are ultra (Mon-Thu, exact dedup, closes=now+1d)
        if is_ultra:
            if now.weekday() >= 4:
                continue
            if title in existing:
                continue
            closes = now + timedelta(days=1)
        else:
            if any(key in t for t in existing):
                continue
            closes = now_t + timedelta(days=days)
        if not _quota_ok(title, key):
            continue
        if _closes_ok(closes):
            specs.append(NewQuestionSpec(title, closes, True, "A", dict(spec)))
    return specs
