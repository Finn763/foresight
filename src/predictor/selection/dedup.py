"""建题去重：精确标题 + 事件签名（标的 × 方向 × 绝对日期）近似判重。

背景（2026-08-20 P1 修复）：agent 建题入口（predictor.cli.predict_once）此前仅按
`title = ?` 精确判重，#97/#98（道琼斯 8-19 vs 8-18）与 #93/#94（伦敦金 8-21 vs 8-14）
仅措辞/日期格式略有差异即双双入库。纯字符串相似度无法可靠判重——#93/#94 相似度仅
0.53，而"未来7天黄金突破5150 vs 未来30天突破4600"这类真·不同题相似度高达 0.82
（模板同构），阈值无论取哪都会误伤一侧。故改用「事件签名」：抽标的 + 方向 + 绝对
日期集合，三者都命中且相等才判同题。保守取向（宁可漏判不误判）：无标的/无方向/无
绝对日期的题不参与签名判重（如"未来7天突破5150"类模板题），避免把不同阈值/不同
horizon 的真·不同题挡掉。
"""

import re
import unicodedata

from predictor.data.storage import Storage

# 标的别名 → 规范 key（中文子串 + 常见英文代码，子串命中即停）
_INSTRUMENT_ALIASES = [
    ("道琼斯", "dji"), ("道指", "dji"), ("dji", "dji"),
    ("伦敦金", "gold"), ("comex黄金", "gold"), ("黄金", "gold"),
    ("xauusd", "gold"), ("xau/usd", "gold"), ("xau", "gold"),
    ("标普", "spx"), ("s&p", "spx"), ("s＆p", "spx"), ("spx", "spx"),
    ("上证", "sh"),
    ("布伦特", "brent"),
    ("离岸人民币", "cny"), ("人民币", "cny"),
    ("比特币", "btc"), ("btc", "btc"),
]

# 方向关键词 → up/down（子串匹配，先命中先得；"突破"≠"跌破"是不同子串）
_DIRECTION = [
    ("高于", "up"), ("上涨", "up"), ("收涨", "up"), ("升破", "up"),
    ("突破", "up"), ("上调", "up"), ("涨", "up"),
    ("低于", "down"), ("下跌", "down"), ("收跌", "down"), ("跌破", "down"),
    ("下调", "down"), ("下降", "down"), ("跌", "down"),
]

_DATE_FULL = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
_DATE_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?")


def _normalize(title: str) -> str:
    s = unicodedata.normalize("NFKC", title)
    s = re.sub(r"[\s（）()，,。.：:；;？?！!【】\[\]\"'‘’、/\\\-]+", "", s)
    return s.lower()


def _dates(title: str) -> set[str]:
    """抽取绝对日期，统一为 "M-D"（丢弃年份：同事件近义题年份一致，跨年同日题
    不可能同时未揭晓）。裸"19日"（无月份）歧义大，不采。只做 NFKC 归一（保留
    日期分隔符），不做 _normalize 的符号剥离——否则 "2026-08-21" 会变成 "20260821"
    而抽不到日期。"""
    t = unicodedata.normalize("NFKC", title)
    out = set()
    for m in _DATE_FULL.finditer(t):
        out.add(f"{int(m.group(2))}-{int(m.group(3))}")
    for m in _DATE_MD.finditer(t):
        out.add(f"{int(m.group(1))}-{int(m.group(2))}")
    return out


def event_signature(title: str) -> tuple[str, str, frozenset[str]] | None:
    """事件签名 (instrument, direction, dates)。缺任一 → None（不参与签名判重）。"""
    t = _normalize(title)
    inst = next((key for alias, key in _INSTRUMENT_ALIASES if alias in t), None)
    direction = next((d for alias, d in _DIRECTION if alias in t), None)
    dates = frozenset(_dates(title))
    if inst is None or direction is None or not dates:
        return None
    return (inst, direction, dates)


def find_duplicate_question(st: Storage, title: str) -> int | None:
    """未揭晓题里找同题：先精确标题，再事件签名近似匹配；无则 None。"""
    unresolved = st.list_unresolved()
    for q in unresolved:
        if q.title == title:
            return q.id
    sig = event_signature(title)
    if sig is None:
        return None
    for q in unresolved:
        if event_signature(q.title) == sig:
            return q.id
    return None
