"""建题去重：精确标题 + 事件签名（标的 × 方向 × 日期）近似判重。

背景（2026-08-20 P1 修复）：agent 建题入口（predictor.cli.predict_once）此前仅按
`title = ?` 精确判重，#97/#98（道琼斯 8-19 vs 8-18）与 #93/#94（伦敦金 8-21 vs 8-14）
仅措辞/日期格式略有差异即双双入库。纯字符串相似度无法可靠判重——#93/#94 相似度仅
0.53，而"未来7天黄金突破5150 vs 未来30天突破4600"这类真·不同题相似度高达 0.82
（模板同构），阈值无论取哪都会误伤一侧。故改用「事件签名」：抽标的 + 方向 + 绝对
日期集合，三者都命中且相等才判同题。保守取向（宁可漏判不误判）：无标的/无方向/无
绝对日期的题不参与签名判重（如"未来7天突破5150"类模板题），避免把不同阈值/不同
horizon 的真·不同题挡掉。

背景（2026-08-27 CC §2.7②）：标的表不覆盖「美联储/FOMC/CPI/EIA」→ #72/#73/#74
三道近义题（9 月 FOMC 会加息吗）并存。本版补宏观题族：fed（美联储/FOMC/联储）、
CPI（国别消歧 cn_cpi/us_cpi）、EIA、非农、PMI。两处配套修正：
1) 方向关键词按题族分表——"美联储决议后黄金会涨吗"的"涨"不得与"加息"互判同题；
2) 否定句式方向抽取——"不会加息"≠"加息"（方向加 _neg 后缀），否定词窗口排除
   "否"（"是否降息"的"否"是疑问词，仍判降息题），包含"停"（"暂停加息"=不加息）。
日期粒度：宏观月度题族（fed/CPI/非农/PMI）取月份集合——FOMC 会议、CPI、PMI 均为
月度事件，且"9/15-16"与"9月"只有按月份才能判同题（#72 带日、#73/#74 只有月）；
EIA 为周频事件保持日粒度（"9月10日当周"与"9月17日当周"是不同事件）。
"""

import re
import unicodedata

from predictor.data.storage import Storage

# 标的别名 → 规范 key（中文子串 + 常见英文代码，子串命中即停）。
# 顺序即优先级：市场标的在前（"美联储决议后黄金会涨吗"的主语是黄金），宏观题族在后。
_INSTRUMENT_ALIASES = [
    ("道琼斯", "dji"), ("道指", "dji"), ("dji", "dji"),
    ("伦敦金", "gold"), ("comex黄金", "gold"), ("黄金", "gold"),
    ("xauusd", "gold"), ("xau/usd", "gold"), ("xau", "gold"),
    ("标普", "spx"), ("s&p", "spx"), ("s＆p", "spx"), ("spx", "spx"),
    ("上证", "sh"),
    ("布伦特", "brent"),
    ("离岸人民币", "cny"), ("人民币", "cny"),
    ("比特币", "btc"), ("btc", "btc"),
    ("美联储", "fed"), ("联储", "fed"), ("fomc", "fed"), ("fed", "fed"),
    ("非农", "nfp"), ("nfp", "nfp"),
    ("pmi", "pmi"),
    ("eia", "eia"),
]

# CPI 需国别消歧：中国 CPI 与美国 CPI 是不同事件（#54 美 CPI vs #5/#7/#25 中 CPI），
# 不能共用 "cpi" 键判同题；"中国08月CPI" 里 "中国" 与 "cpi" 之间隔着月份，
# 普通子串别名匹配不上，用正则（不带国别的裸 "cpi" 命中则回退通用键）。
_CPI_ALIAS = re.compile(r"(中国|美国)?\s*(?:\d{1,2}\s*月)?\s*cpi")

# 方向关键词按题族分表（子串匹配，先命中先得；跨族不混用——
# 市场题族用涨跌价词，数据题族（CPI/EIA/非农/PMI）用高低增减词，fed 用利率词）。
_MARKET_DIRECTION = [
    ("高于", "up"), ("上涨", "up"), ("收涨", "up"), ("升破", "up"),
    ("突破", "up"), ("上调", "up"), ("涨", "up"),
    ("低于", "down"), ("下跌", "down"), ("收跌", "down"), ("跌破", "down"),
    ("下调", "down"), ("下降", "down"), ("跌", "down"),
    ("增加", "up"), ("减少", "down"),
]
_MACRO_DIRECTION = [
    ("高于", "up"), ("上涨", "up"), ("上升", "up"), ("增长", "up"), ("增加", "up"),
    ("涨", "up"),
    ("低于", "down"), ("下跌", "down"), ("下降", "down"), ("减少", "down"),
    ("跌", "down"),
]
_FED_DIRECTION = [
    ("加息", "up"), ("上调", "up"),
    ("降息", "down"), ("下调", "down"),
    ("维持不变", "flat"), ("维持利率", "flat"), ("按兵不动", "flat"), ("维持", "flat"),
]

# 否定标记：方向关键词前 ≤3 字符内出现即判定为否定式（"不会加息"/"难以突破"/
# "暂停加息"），方向加 _neg 后缀与肯定式区分（否定题与肯定题是两道相反的题，
# 绝不能互判同题；"不加息"亦不等于"降息"——_neg 后缀天然隔离）。
_NEGATION_CHARS = {"不", "未", "没", "难", "无", "停"}

_DATE_FULL = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
_DATE_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?")
_MONTH_BARE = re.compile(r"(\d{1,2})\s*月")

# 月度粒度题族（FOMC 会议/CPI/PMI/非农均为月度事件）
_MONTH_FAMILIES = {"fed", "cpi", "cn_cpi", "us_cpi", "nfp", "pmi"}


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


def _month_dates(title: str) -> frozenset[str]:
    """月度事件日期集：只取月份——"9/15-16" 与 "9月" 必须判同月（否则 #72 带日、
    #73/#74 只有月，永远判不成同题）。丢弃年份与日粒度版本同理：跨年同月题
    同时未揭晓的概率极低，保守取向接受该漏判边界。"""
    t = unicodedata.normalize("NFKC", title)
    out = set()
    for m in _DATE_FULL.finditer(t):
        out.add(f"{int(m.group(2))}")
    for m in _MONTH_BARE.finditer(t):
        out.add(f"{int(m.group(1))}")
    return frozenset(out)


def _instrument(t: str) -> str | None:
    for alias, key in _INSTRUMENT_ALIASES:
        if alias in t:
            return key
    # CPI 国别消歧：优先带国别的命中（"美联储cpi公布后中国cpi会涨吗"取中国cpi）
    country, bare = None, False
    for m in _CPI_ALIAS.finditer(t):
        if m.group(1):
            country = m.group(1)
            break
        bare = True
    if country:
        return "cn_cpi" if country == "中国" else "us_cpi"
    return "cpi" if bare else None


def _direction(t: str, pairs) -> str | None:
    """按题族方向表取首个命中关键词的方向；关键词前 ≤3 字符含否定标记 → _neg。"""
    for alias, key in pairs:
        pos = t.find(alias)
        if pos < 0:
            continue
        prefix = t[max(0, pos - 3):pos]
        return f"{key}_neg" if any(c in prefix for c in _NEGATION_CHARS) else key
    return None


def event_signature(title: str) -> tuple[str, str, frozenset[str]] | None:
    """事件签名 (instrument, direction, dates)。缺任一 → None（不参与签名判重）。
    宏观月度题族 dates 为月份集合（{"9"}），其余为日粒度（{"8-19"}）。"""
    t = _normalize(title)
    inst = _instrument(t)
    if inst is None:
        return None
    if inst in _MONTH_FAMILIES:
        direction = _direction(t, _FED_DIRECTION if inst == "fed" else _MACRO_DIRECTION)
        dates = _month_dates(title)
    else:  # 市场标的 + EIA（周频事件，保持日粒度日期）
        direction = _direction(t, _MARKET_DIRECTION)
        dates = frozenset(_dates(title))
    if direction is None or not dates:
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
