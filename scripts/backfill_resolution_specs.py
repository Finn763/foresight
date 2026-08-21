"""python scripts/backfill_resolution_specs.py --db data/foresight.db
给存量 42 题补 resolution_class/spec（A 类行情题：标普/布伦特/金价/人民币/比特币；
B/C 类写 class 占位）。按标题匹配——阈值用函数式提取（题面为准），不依赖关键词硬编码。

与 brief RULES 关键词表的差异（均按题面核对，见 T7 报告核对表）：
- 汇率阈值从题面正则提取："升破 X" → lt_threshold X（覆盖 7.0/6.75/6.5 多值，
  避免"人民币兑美元=7.0"与"离岸人民币=6.75"两条关键词规则的先后序冲突）；
- 金价/布伦特/标普阈值题（突破 X 美元/桶、站上 X 点）同样提取阈值；
- "创新高/创历史新高"7 题：P0 CONDITIONS 无 record_high（T5 审查裁定该族移除），
  不写 gt_prev_close 近似（会按错误语义自动揭晓、永久入错账）→ 标 C 人工占位，P1 恢复；
- "美联储"补进 B 类 FOMC 关键词（#66 标题无 "FOMC" 子串）。
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predictor.config import Settings
from predictor.data.storage import Storage
from predictor.resolution.spec import validate_resolution_spec


def _a_spec(
    instrument: str,
    *,
    condition: str,
    value: float | None = None,
    source_primary: str = "sina",
    compare_symbol: str | None = None,
    backup_symbol: str | None = None,
    source_backup: str | None = None,
    close_timezone: str = "UTC",
) -> dict:
    spec = {
        "class": "A",
        "instrument": instrument,
        "source_primary": source_primary,
        "condition": condition,
        "close_timezone": close_timezone,
        "grace_days": 3,
        "degrade_to": "C",
    }
    if compare_symbol:
        spec["compare_symbol"] = compare_symbol
    if backup_symbol:
        spec["backup_symbol"] = backup_symbol
    if source_backup:
        spec["source_backup"] = source_backup
    if value is not None:
        spec["value"] = value
    return spec


def _match(title: str) -> tuple[str, dict, str] | None:
    """按题面匹配 → (resolution_class, spec, 规则描述)。无匹配返回 None。"""
    # —— C 类（人工揭晓）——
    for kw in ("苹果", "微博"):
        if kw in title:
            return "C", {"class": "C"}, f"C 关键词「{kw}」"
    if "双11" in title or "双 11" in title:  # 存量题面是"双 11"（带空格）
        return "C", {"class": "C"}, "C 关键词「双11」"
    # —— B 类（P1 揭晓；美联储 = FOMC 类，补关键词）——
    for kw in ("EIA", "CPI", "FOMC", "美联储", "PMI", "票房", "气温", "新能源", "国庆档"):
        if kw in title:
            return "B", {"class": "B"}, f"B 关键词「{kw}」"
    # —— 创新高/创历史新高：P0 无 record_high 条件（T5 裁定 P1），标 C 人工占位 ——
    if "创历史新高" in title or "创新高" in title:
        return "C", {"class": "C"}, "A 标普 record_high → P0 无该条件，C 人工占位 (P1)"
    # —— A 类行情（函数式匹配，阈值按题面提取）——
    m = re.search(r"升破\s*([\d.]+)", title)
    if m:
        v = float(m.group(1))
        return (
            "A",
            _a_spec(
                "usdcnh",
                condition="lt_threshold",
                value=v,
                compare_symbol="fx_susdcnh",
                close_timezone="Asia/Shanghai",
            ),
            f"A 人民币兑美元 升破 {v} → lt_threshold",
        )
    if "比特币" in title:
        m = re.search(r"突破\s*([\d.]+)\s*万", title)
        v = float(m.group(1)) * 10000 if m else 150000.0
        return (
            "A",
            _a_spec("btc", condition="gt_threshold", value=v, source_primary="coingecko"),
            f"A 比特币 突破 {v:.0f} → gt_threshold",
        )
    m = re.search(r"突破\s*([\d.]+)\s*美元", title)
    if m and "金价" in title:
        v = float(m.group(1))
        return (
            "A",
            _a_spec(
                "gold",
                condition="gt_threshold",
                value=v,
                compare_symbol="hf_GC",
                close_timezone="America/New_York",
            ),
            f"A 金价 突破 {v:.0f} → gt_threshold",
        )
    m = re.search(r"突破\s*([\d.]+)\s*美元/桶", title)
    if m:
        v = float(m.group(1))
        return (
            "A",
            _a_spec("brent", condition="gt_threshold", value=v, compare_symbol="hf_OIL"),
            f"A 布伦特 突破 {v:.0f} → gt_threshold",
        )
    m = re.search(r"站上\s*([\d.]+)\s*点", title)
    if m:
        v = float(m.group(1))
        return (
            "A",
            _a_spec(
                "spx",
                condition="gt_threshold",
                value=v,
                compare_symbol="gb_$inx",
                backup_symbol="usINX",
                source_backup="tencent",
                close_timezone="America/New_York",
            ),
            f"A 标普 站上 {v:.0f} → gt_threshold",
        )
    if "标普" in title:
        return (
            "A",
            _a_spec(
                "spx",
                condition="gt_prev_close",
                compare_symbol="gb_$inx",
                backup_symbol="usINX",
                source_backup="tencent",
                close_timezone="America/New_York",
            ),
            "A 标普 收盘高于昨收 → gt_prev_close",
        )
    if "布伦特" in title:
        return (
            "A",
            _a_spec("brent", condition="gt_prev_close", compare_symbol="hf_OIL"),
            "A 布伦特 收盘高于昨收 → gt_prev_close",
        )
    if "金价" in title:
        return (
            "A",
            _a_spec(
                "gold",
                condition="gt_prev_close",
                compare_symbol="hf_GC",
                close_timezone="America/New_York",
            ),
            "A 金价 收盘高于昨收 → gt_prev_close",
        )
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=Settings().db_path)
    args = ap.parse_args()
    st = Storage(args.db)
    st.create_schema()
    # 只处理"存量未回填"（resolution_class IS NULL）：幂等重跑 0 变更，
    # 且不覆盖运行时已发生的降级（如 resolve_round 宽限超时标 C）
    rows = st._conn.execute(
        "SELECT id, title FROM questions WHERE outcome IS NULL "
        "AND resolution_class IS NULL ORDER BY id"
    ).fetchall()
    done, skipped = 0, 0
    for qid, title in rows:
        hit = _match(title)
        if hit is None:
            print(f"SKIP 未匹配: #{qid} {title}")
            skipped += 1
            continue
        cls, spec, desc = hit
        if cls == "A" and validate_resolution_spec(spec):
            print(f"  WARN 非法 A spec 拒绝写入: #{qid} {title}")
            skipped += 1
            continue
        st.set_resolution(qid, cls, spec)
        done += 1
        print(f"  #{qid} [{cls}] {title}  ← {desc}")
    print(f"已补规格 {done} 题，未匹配 {skipped} 题")


if __name__ == "__main__":
    main()
