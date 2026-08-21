"""quotes.py 解析单测：样例全部来自 scripts/probe_quotes.py 2026-08-12 16:34(北京时间) 实测量取。
（brief 假设的字段序与实测不符处已按实测修正，见各用例注释。）
"""

import pytest

from predictor.resolution.quotes import (
    QuoteError,
    _parse_coinbase,
    _parse_coingecko,
    _parse_sina,
    _parse_sina_prev,
    _parse_tencent,
    _parse_tencent_prev,
)

# ---------- 新浪：现价 ----------


def test_sina_close_parse():
    # 实测 gb_$inx 前 12 字段（verbatim）: parts[1]=现价 7728.2002
    text = (
        'var hq_str_gb_$inx="标普500指数,7728.2002,-0.32,2026-08-12 04:34:22,'
        "-24.9100,7767.5098,7767.5098,7717.2500,7793.6802,6316.9102,"
        '2502546862,3171411671,...";'
    )
    assert _parse_sina(text, "gb_$inx") == pytest.approx(7728.2002)


def test_sina_hf_close_parse():
    # 实测 hf_GC（verbatim）: parts[0]=现价 4467.245
    text = (
        'var hq_str_hf_GC="4467.245,,4466.000,4466.300,4475.700,4421.400,'
        '16:31:49,4441.100,4430.000,0,2,1,2026-08-12,纽约黄金,0";'
    )
    assert _parse_sina(text, "hf_GC") == pytest.approx(4467.245)


def test_sina_empty_body_raises():
    # 实测 hf_B 空响应（verbatim）
    with pytest.raises(QuoteError):
        _parse_sina('var hq_str_hf_B="";\n', "hf_B")


# ---------- 新浪：昨收 ----------


def test_sina_prev_parse_gb():
    # 实测 gb_$inx: parts[26]=昨收 7753.1099（== 腾讯 usINX parts[4]=7753.11）
    # 字段 12-25 按同日 gb_$dji 实测布局填充（$inx 实测 0-11 与 26 为真值）
    text = (
        'var hq_str_gb_$inx="标普500指数,7728.2002,-0.32,2026-08-12 04:34:22,'
        "-24.9100,7767.5098,7767.5098,7717.2500,7793.6802,6316.9102,"
        "2502546862,3171411671,0,0.00,--,0.00,0.00,0.00,0.00,0,0,0.0000,"
        '0.00,0.0000,,Aug 11 04:34PM EDT,7753.1099,0,1,2026";'
    )
    assert _parse_sina_prev(text, "gb_$inx") == pytest.approx(7753.1099)


def test_sina_prev_parse_fx():
    # 实测 fx_susdcnh: parts[8]=昨收（平盘日 6.746900==现价；sina fx 布局昨收=8）
    text = (
        'var hq_str_fx_susdcnh="16:32:55,6.746900,6.747000,6.746200,49,'
        "6.746400,6.748800,6.743900,6.746900,离岸人民币（香港）,0.010000,"
        '0.000700,0.0007263,,6.995700,6.740100,,2026-08-12";'
    )
    assert _parse_sina_prev(text, "fx_susdcnh") == pytest.approx(6.746900)


def test_sina_prev_body_zero_raises():
    # T3 遗留 minor：_parse_sina_prev 空值守卫缺 body=="0"（与 _parse_sina 对齐），
    # fx_ 前缀会把 "0" 当数据解析 → 对齐后 body="0" 直接抛 QuoteError
    with pytest.raises(QuoteError):
        _parse_sina_prev('var hq_str_fx_susdcnh="0";\n', "fx_susdcnh")


def test_sina_hf_prev_rejected():
    # brief 规则：hf_ 拒昨收（parts[7] 昨收未双源验证，P0 期货题不用 gt_prev_close）
    with pytest.raises(QuoteError):
        _parse_sina_prev('var hq_str_hf_GC="5100.0,5090.0,...";', "hf_GC")


# ---------- 腾讯：现价 ----------


def test_tencent_close_parse_us():
    # 实测 v_usINX（verbatim 前 10 字段）: parts[3]=现价 7728.20
    text = (
        'v_usINX="200~标普500~.INX~7728.20~7753.11~7767.51~2502546862~0~0~'
        '7693.80~0~0~0~0~0~0~0~0~0~7767.56~...";'
    )
    assert _parse_tencent(text, "usINX") == pytest.approx(7728.20)


def test_tencent_close_parse_sh():
    # 实测 v_s_sh000001（verbatim）: parts[3]=现价 3946.68
    text = (
        'v_s_sh000001="1~上证指数~000001~3946.68~12.59~0.32~503257011~98612239~~694282.66~ZS~";\n'
    )
    assert _parse_tencent(text, "s_sh000001") == pytest.approx(3946.68)


# ---------- 腾讯：昨收 ----------


def test_tencent_prev_parse_us():
    # 实测 v_usINX: parts[4]=昨收 7753.11（== 新浪 parts[26]=7753.1099）
    text = (
        'v_usINX="200~标普500~.INX~7728.20~7753.11~7767.51~2502546862~0~0~'
        '7693.80~0~0~0~0~0~0~0~0~0~7767.56~...";'
    )
    assert _parse_tencent_prev(text, "usINX") == pytest.approx(7753.11)


def test_tencent_prev_parse_sh():
    # 实测 v_s_sh000001: parts[4]=涨跌额(12.59) 非昨收 → 昨收=现价-涨跌额=3934.09
    # （跨源核对：新浪 s_sh000001 现价 3946.6752-涨跌额 12.5823=3934.0929 一致）
    text = (
        'v_s_sh000001="1~上证指数~000001~3946.68~12.59~0.32~503257011~98612239~~694282.66~ZS~";\n'
    )
    assert _parse_tencent_prev(text, "s_sh000001") == pytest.approx(3934.09)


# ---------- BTC ----------


def test_coingecko_close_parse():
    # 实测响应（verbatim）
    assert _parse_coingecko('{"bitcoin":{"usd":63760}}') == pytest.approx(63760.0)


def test_coinbase_close_parse():
    # 实测响应（verbatim）
    assert _parse_coinbase(
        '{"data":{"amount":"63710.165","base":"BTC","currency":"USD"}}'
    ) == pytest.approx(63710.165)
