# calibration/postprocess.py
def clamp(p: float) -> float:
    return max(0.01, min(0.99, p))


def extremize(p: float, alpha: float = 0.3) -> float:
    """向 0/1 外推；LLM 概率趋于保守。"""
    return clamp(0.5 + (p - 0.5) * (1 + alpha))
