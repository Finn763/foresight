"""autopick_ingest 时间戳：UTC ISO 必须转成北京 naive（DB 全局约定）。"""

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import autopick_ingest as ai  # noqa: E402


def test_parse_dt_converts_utc_to_beijing():
    assert ai._parse_dt("2026-11-04T12:00:00+00:00") == datetime(2026, 11, 4, 20, 0)
    assert ai._parse_dt("2026-11-04T12:00:00Z") == datetime(2026, 11, 4, 20, 0)


def test_parse_dt_naive_treated_as_utc():
    # autopick prompt 要求 +00:00；缺失偏移时按 UTC 解释，不得当本地时间
    assert ai._parse_dt("2026-11-04T12:00:00") == datetime(2026, 11, 4, 20, 0)
