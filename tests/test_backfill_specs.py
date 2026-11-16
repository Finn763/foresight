"""backfill_resolution_specs._match 回归：泛化正则不得跨标的/默认数值写 spec。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backfill_resolution_specs as bf  # noqa: E402


def test_gold_threshold_not_misread_as_usdcnh():
    out = bf._match("国际金价会升破4000美元吗")
    assert out is None or out[1].get("instrument") == "gold"


def test_brent_threshold_not_misread_as_usdcnh():
    out = bf._match("布伦特原油会升破100美元/桶吗")
    assert out is None or out[1].get("instrument") == "brent"


def test_sp500_point_threshold_stays_spx():
    cls, spec, _ = bf._match("标普500会升破6000点吗")
    assert cls == "A" and spec["instrument"] == "spx"
    assert spec["condition"] == "gt_threshold" and spec["value"] == 6000.0


def test_btc_direction_and_value_from_title():
    _, spec, _ = bf._match("比特币价格会高于82,000美元吗")
    assert spec["condition"] == "gt_threshold" and spec["value"] == 82000.0
    _, spec2, _ = bf._match("比特币会跌至80,000美元吗")
    assert spec2["condition"] == "lt_threshold" and spec2["value"] == 80000.0


def test_btc_without_number_fails_closed():
    assert bf._match("比特币会大涨吗") is None  # 旧实现默认 150000


def test_cny_strengthening_maps_to_lt():
    _, spec, _ = bf._match("离岸人民币兑美元会升破6.90吗")
    assert spec["instrument"] == "usdcnh" and spec["condition"] == "lt_threshold"
    assert spec["value"] == 6.90


def test_usd_cny_strengthening_maps_to_gt():
    _, spec, _ = bf._match("美元兑人民币会升破7.20吗")
    assert spec["condition"] == "gt_threshold" and spec["value"] == 7.20
