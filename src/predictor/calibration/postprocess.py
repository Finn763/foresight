# calibration/postprocess.py
import math


def clamp(p: float) -> float:
    # min/max 对 NaN 的比较语义会把 NaN 变成边界值（实测 min(1.0, nan) == 1.0），
    # 必须先拒绝，否则 NaN 会被静默写成 0.99。
    if not math.isfinite(p):
        raise ValueError(f"probability 非有限值: {p!r}")
    return max(0.01, min(0.99, p))


def extremize(p: float, alpha: float = 0.3) -> float:
    """向 0/1 外推；LLM 概率趋于保守。"""
    return clamp(0.5 + (p - 0.5) * (1 + alpha))
