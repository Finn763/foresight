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
    """按基准率分档：easy(br>0.6 或 br<0.4) / medium / blind(无基线命中)。

    base_rates 的 key 约定为中文子串（与族 key 同构），如
    {"标普": 0.52, "上证": 0.45, "布伦特": 0.55, "黄金": 0.40, "人民币": 0.48}；
    以子串匹配中文标题（"标普" in "明天标普500收盘会高于今天吗"），
    避免 ASCII key 永远匹配不上中文标题、全判 blind。
    """
    br = None
    for k, v in base_rates.items():
        if k in title:
            br = v
            break
    if br is None:
        return "blind"
    return "easy" if (br > 0.6 or br < 0.4) else "medium"


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
        # 族配额：同族未揭晓 ≤3（≈30% 题池）。family_key 用中文子串匹配标题
        family_share = sum(1 for t in existing + [s.title for s in specs] if family_key in t)
        return family_share < 3

    # —— 超短档（周一~周四每日 3 道）：标普 + 上证 + 道琼斯（均 A 类、次日收盘为真实事件）——
    # 周五/周末不出超短题：次日 closes 落在休市日（周五→周六、周六→周日），
    # "周六收盘 vs 周五收盘"这类事件不存在——行情 API 周末只返回"周五收盘 vs 周四收盘"，
    # 系统会用周五数据揭晓假事件 → 错日错判永久入账（final review C1）。
    # 规则：closes 落周六的族一律跳过；周五起标普/上证/道琼斯全部被跳过。
    # 布伦特/黄金 hf_ 期货曾作 24h 轮换位：hf_ 昨收无双源验证（quotes.py 拒昨收，
    # gt_prev_close 恒失败 → resolution_failed 刷屏、3 天后降级 C，"A 类自动揭晓"落空），
    # 且 ICE/COMEX 周六无结算——"周六布伦特收盘"同为假事件 → P0 移出超短池（final review I1）。
    # P1：hf_ 昨收语义（parts[7] 未双源验证）+ 24h 电子盘收盘价定义解决后，可恢复布伦特
    # 超短轮换位（须保证 closes 不落休市日）。
    ultra_defs = [
        (
            "明天标普500收盘会高于今天吗",
            "标普",
            0,
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
            2,
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
    ]
    if now.weekday() < 4:  # 周一~周四：次日为工作日，标普/上证/道琼斯收盘事件存在
        picks = [0, 1, 2]
    else:
        picks = []  # 周五/周末：次日收盘不存在 → 超短不出题
    for idx in picks:
        title, key, _, spec = ultra_defs[idx]
        if title in existing:
            continue
        if not _quota_ok(title, key):
            continue
        closes = now + timedelta(days=1)
        if _closes_ok(closes):
            specs.append(NewQuestionSpec(title, closes, True, "A", dict(spec)))

    # —— 短/中/长档阈值网格（每周）：7d / 30d / 60d 阈值题 ——
    now_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    # P1: record_high 解析器实现后恢复（P0 CONDITIONS 仅 gt_prev_close/gt_threshold/lt_threshold，
    #      旧标普新高题 condition=record_high_7d 会被 daily.py 校验门静默丢弃）
    weekly = [
        (
            "未来7天内COMEX黄金会突破5150美元/盎司吗",
            "黄金",
            now_t + timedelta(days=7),
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
            now_t + timedelta(days=30),
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
            now_t + timedelta(days=30),
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
            now_t + timedelta(days=30),
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
            now_t + timedelta(days=60),
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
    for title, key, closes, spec in weekly:
        if any(key in t for t in existing):
            continue
        if not _quota_ok(title, key):
            continue
        if _closes_ok(closes):
            specs.append(NewQuestionSpec(title, closes, True, "A", dict(spec)))

    # —— 难度三档 ≥25% 平衡由调用方（evolve.py）注入 base_rates 负责，P0 生成器不内置兜底。
    # 早期"盲档保底"块产出的 {"class": "C"} spec 缺 6 个 REQUIRED 字段（class/instrument/
    # source_primary/condition/close_timezone/grace_days/degrade_to），被 daily.py 校验门
    # 100% 拦截、从未成功入库——功能失效还误导，故整块删除。
    return specs
