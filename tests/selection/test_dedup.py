"""建题去重测试（P1 修复）：精确标题 + 事件签名近似判重；不同日/不同标的/无日期模板题不误判。"""

from datetime import datetime, timedelta

from predictor.data.storage import Storage
from predictor.selection.dedup import event_signature, find_duplicate_question

T97 = (
    "2026年8月19日（美东时间）道琼斯工业平均指数收盘点位会高于2026年8月18日收盘点位吗"
    "（判定口径：以官方/权威行情源的每日收盘价为准，19日收盘 > 18日收盘即成立）"
)
T98 = (
    "2026年8月19日（美东时间）道琼斯工业平均指数收盘点位会高于2026年8月18日收盘点位吗"
    "（判定口径：以权威行情源的每日收盘价为准，8月19日收盘 > 8月18日收盘即成立）"
)
T93 = (
    "伦敦金现（XAU/USD）下周是否上涨：2026年8月21日（下周五）收盘价高于2026年8月14日"
    "（本周五）收盘价即算涨，数据源以公开行情（金十数据/伦敦金银市场）为准"
)
T94 = "伦敦金现货(XAU/USD)下周五2026-08-21收盘价是否高于本周五2026-08-14收盘价（即周度上涨）？"


def _st(tmp_path) -> Storage:
    st = Storage(str(tmp_path / "d.db"))
    st.create_schema()
    return st


def _add(st: Storage, title: str, closes_days: int = 30) -> int:
    return st.add_question(title, datetime(2026, 8, 20) + timedelta(days=closes_days))


def test_exact_title_dup(tmp_path):
    st = _st(tmp_path)
    qid = _add(st, T97)
    assert find_duplicate_question(st, T97) == qid


def test_near_dup_djia_same_event(tmp_path):
    st = _st(tmp_path)
    qid = _add(st, T97)
    assert find_duplicate_question(st, T98) == qid  # 措辞略异 → 事件签名判同题


def test_near_dup_gold_same_event(tmp_path):
    st = _st(tmp_path)
    qid = _add(st, T93)
    assert find_duplicate_question(st, T94) == qid  # 日期格式/措辞略异 → 判同题


def test_different_day_not_dup(tmp_path):
    st = _st(tmp_path)
    _add(st, "2026年8月19日道琼斯收盘高于2026年8月18日吗")
    assert find_duplicate_question(st, "2026年8月20日道琼斯收盘高于2026年8月19日吗") is None


def test_different_instrument_not_dup(tmp_path):
    st = _st(tmp_path)
    _add(st, "2026年8月19日道琼斯收盘高于2026年8月18日吗")
    assert find_duplicate_question(st, "2026年8月19日上证收盘高于2026年8月18日吗") is None


def test_no_dates_template_not_dup(tmp_path):
    # 模板题无绝对日期 → 签名 None，不误判（"未来7天突破5150" vs "未来30天突破4600"）
    st = _st(tmp_path)
    _add(st, "未来7天内COMEX黄金会突破5150美元/盎司吗")
    assert find_duplicate_question(st, "未来30天内COMEX黄金会突破4600美元/盎司吗") is None


def test_event_signature_fields():
    assert event_signature(T97) == ("dji", "up", frozenset({"8-19", "8-18"}))
    assert event_signature(T93) == ("gold", "up", frozenset({"8-21", "8-14"}))
    assert event_signature("未来7天内COMEX黄金会突破5150美元/盎司吗") is None  # 无绝对日期
    assert event_signature("明天道琼斯指数会收涨吗") is None  # 无绝对日期


# ---- CC §2.7②：宏观题族 + 否定句式（2026-08-27）----

T72 = "2026年9月美联储FOMC会议（9/15-16）会加息吗（即上调联邦基金利率目标区间）？"
T73 = "美联储9月会加息吗"
T74 = "美联储2026年9月FOMC会议会加息吗（上调联邦基金利率目标区间）"


def test_fed_family_same_event(tmp_path):
    """#72/#73/#74 根因回归：三道近义题应判同题（标的 fed + 加息 + 9 月）。"""
    st = _st(tmp_path)
    qid = _add(st, T72)
    assert find_duplicate_question(st, T73) == qid  # 只有月份、无日无年份
    assert find_duplicate_question(st, T74) == qid


def test_fed_event_signature_month_granularity():
    sig = ("fed", "up", frozenset({"9"}))
    assert event_signature(T72) == sig  # "9/15-16" 与 "9月" 同月
    assert event_signature(T73) == sig
    assert event_signature(T74) == sig


def test_fed_direction_and_month_distinguish(tmp_path):
    st = _st(tmp_path)
    _add(st, T73)  # 9 月加息
    assert find_duplicate_question(st, "美联储2026年9月FOMC会议会降息吗") is None
    assert find_duplicate_question(st, "2026年9月FOMC会议会维持利率不变吗") is None
    assert find_duplicate_question(st, "美联储2026年10月FOMC会议会加息吗") is None
    assert event_signature("2026年9月FOMC会议会维持利率不变吗") == (
        "fed", "flat", frozenset({"9"})
    )
    assert event_signature("美联储2026年10月FOMC会议会加息吗") == (
        "fed", "up", frozenset({"10"})
    )


def test_fed_negation_not_same_event(tmp_path):
    """否定句式方向抽取：'不会加息'≠'加息'（_neg 隔离，绝不互判同题）。"""
    st = _st(tmp_path)
    _add(st, T73)
    assert find_duplicate_question(st, "美联储9月不会加息吗") is None
    assert event_signature("美联储9月不会加息吗") == ("fed", "up_neg", frozenset({"9"}))
    # "是否降息"的"否"是疑问词不是否定——仍是降息题
    assert event_signature("2026年9月FOMC会议是否降息") == ("fed", "down", frozenset({"9"}))
    # "暂停加息"=不加息
    assert event_signature("美联储9月会议暂停加息") == ("fed", "up_neg", frozenset({"9"}))


def test_cpi_country_disambiguation(tmp_path):
    """#54（美 CPI）与 #5/#7/#25（中 CPI）是不同事件，不得因同为 cpi 判同题。"""
    st = _st(tmp_path)
    _add(st, "2026年9月美国CPI同比会高于8月吗")
    assert find_duplicate_question(st, "中国09月CPI同比会高于上月吗") is None
    assert event_signature("2026年9月美国CPI同比会高于8月吗") == (
        "us_cpi", "up", frozenset({"9", "8"})
    )
    # "中国" 与 "cpi" 隔着月份也能正确归属
    assert event_signature("中国08月CPI同比会高于上月吗") == (
        "cn_cpi", "up", frozenset({"8"})
    )


def test_cpi_near_dup_same_event(tmp_path):
    st = _st(tmp_path)
    qid = _add(st, "2026年10月中国CPI同比会高于9月吗")
    assert find_duplicate_question(st, "中国10月CPI会高于9月吗") == qid


def test_eia_family_day_granularity(tmp_path):
    """EIA 为周频事件：日粒度区分不同周；同周近义题判同题。"""
    st = _st(tmp_path)
    qid = _add(st, "2026年9月17日当周EIA原油库存会下降吗")
    assert find_duplicate_question(st, "9月17日EIA原油库存会下降吗") == qid
    assert find_duplicate_question(st, "2026年9月10日当周EIA原油库存会下降吗") is None
    assert event_signature("2026年9月17日当周EIA原油库存会下降吗") == (
        "eia", "down", frozenset({"9-17"})
    )


def test_market_negation_not_same_event(tmp_path):
    """否定句式回归（市场题族）：'不会高于'≠'高于'。"""
    st = _st(tmp_path)
    _add(st, "2026年8月19日道琼斯收盘会高于2026年8月18日吗")
    assert find_duplicate_question(
        st, "2026年8月19日道琼斯收盘不会高于2026年8月18日吗"
    ) is None
    assert event_signature("2026年8月19日道琼斯收盘不会高于2026年8月18日吗") == (
        "dji", "up_neg", frozenset({"8-19", "8-18"})
    )

