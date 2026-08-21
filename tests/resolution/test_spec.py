from predictor.resolution.spec import validate_resolution_spec


def test_valid_spec_passes():
    spec = {
        "class": "A",
        "instrument": "spx",
        "source_primary": "sina",
        "condition": "gt_prev_close",
        "close_timezone": "America/New_York",
        "grace_days": 3,
        "degrade_to": "C",
    }
    assert validate_resolution_spec(spec) == []


def test_missing_required_fields():
    spec = {"class": "A"}
    errs = validate_resolution_spec(spec)
    # brief 原断言 "condition" in errs 为列表成员判断（errs 元素是 "missing: condition"），按意图改子串判断
    assert any("condition" in e for e in errs)
    assert any("source_primary" in e for e in errs)


def test_unknown_condition_rejected():
    spec = {
        "class": "A",
        "instrument": "spx",
        "source_primary": "sina",
        "condition": "moon_alignment",
        "close_timezone": "UTC",
        "grace_days": 1,
        "degrade_to": "C",
    }
    assert any("condition" in e for e in validate_resolution_spec(spec))


def test_gt_threshold_requires_value():
    spec = {
        "class": "A",
        "instrument": "btc",
        "source_primary": "coingecko",
        "condition": "gt_threshold",
        "close_timezone": "UTC",
        "grace_days": 3,
        "degrade_to": "C",
    }
    assert any("value" in e for e in validate_resolution_spec(spec))


def test_unknown_class_rejected():
    spec = {
        "class": "D",
        "instrument": "spx",
        "source_primary": "sina",
        "condition": "gt_prev_close",
        "close_timezone": "UTC",
        "grace_days": 3,
        "degrade_to": "C",
    }
    assert any("class" in e for e in validate_resolution_spec(spec))


def test_record_high_spec_valid_without_value():
    # record_high 的 prior ATH 由揭晓时 K 线动态计算，spec 不存 value
    spec = {
        "class": "A",
        "instrument": "spx",
        "source_primary": "tencent",
        "source_backup": "yahoo",
        "condition": "record_high",
        "close_timezone": "America/New_York",
        "grace_days": 3,
        "degrade_to": "C",
    }
    assert validate_resolution_spec(spec) == []
