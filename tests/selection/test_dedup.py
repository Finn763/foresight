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
