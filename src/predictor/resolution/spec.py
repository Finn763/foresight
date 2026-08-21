"""resolution_spec 校验：出题即写、机器可读、永不修订。

P0 判定条件三种：
- gt_prev_close: 收盘价 > 昨收（需双源可比，sina/tencent 取昨收）
- gt_threshold:  收盘价 > value（需 value 字段）
- lt_threshold:  收盘价 < value（需 value 字段）
"""

REQUIRED = {
    "class",
    "instrument",
    "source_primary",
    "condition",
    "close_timezone",
    "grace_days",
    "degrade_to",
}
CONDITIONS = {"gt_prev_close", "gt_threshold", "lt_threshold", "record_high"}
CLASSES = {"A", "B", "C"}


def validate_resolution_spec(spec: dict) -> list[str]:
    """校验 spec，返回缺失/非法字段列表；空列表 = 合法。

    C 类人工揭晓不依赖自动化字段：降级产物（A/B 超宽限标 C，保留原字段）与
    人工题（裸 `{"class": "C"}`）都合法，直接放行（2026-08-20 修——此前裸 C
    spec 会被 REQUIRED 拦下，与"人工揭晓无需 spec 字段"语义冲突）。
    """
    if spec.get("class") == "C":
        return []
    errs = []
    for k in REQUIRED - set(spec):
        errs.append(f"missing: {k}")
    if "condition" in spec and spec["condition"] not in CONDITIONS:
        errs.append(f"unknown condition: {spec['condition']}")
    if spec.get("class") not in CLASSES:
        errs.append(f"unknown class: {spec.get('class')}")
    if spec.get("condition") in ("gt_threshold", "lt_threshold") and "value" not in spec:
        errs.append("missing: value (threshold condition)")
    return errs
