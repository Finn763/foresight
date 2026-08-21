# calibration/scoring.py
import math


def brier(probability: float, outcome: bool) -> float:
    return (probability - int(outcome)) ** 2


def log_score(probability: float, outcome: bool) -> float:
    p = max(1e-9, min(1 - 1e-9, probability))
    return -math.log(p if outcome else 1 - p)
