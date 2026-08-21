"""校准闭环：从已揭晓题收集 (probability, outcome) 对 → fit 保序校准器 → 落盘/加载。

样本不足（< MIN_SAMPLES）时 build 返回 None（不启用校准，identity 退化），
避免小样本过拟合污染概率。加载失败（文件缺失/损坏）一律 None，调用方静默降级。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from predictor.calibration.isotonic import IsotonicCalibrator, fit_isotonic

MIN_SAMPLES = 30
DEFAULT_CALIBRATOR_PATH = "data/calibrator.json"  # 与 Settings().db_path 同相对口径


def build_calibrator(storage: Any, *, min_samples: int = MIN_SAMPLES) -> IsotonicCalibrator | None:
    """用历史已揭晓题的 (prob, outcome) 对 fit 保序校准器；样本不足返回 None。"""
    pairs = storage.calibration_pairs()
    if len(pairs) < min_samples:
        return None
    return fit_isotonic([p for p, _ in pairs], [o for _, o in pairs])


def save_calibrator(cal: IsotonicCalibrator, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"steps": cal.steps}), encoding="utf-8")


def load_calibrator(path: str | Path) -> IsotonicCalibrator | None:
    """加载落盘校准器；文件缺失/损坏 → None（identity 降级）。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        steps = [(float(x), float(y)) for x, y in raw["steps"]]
    except Exception:
        return None
    return IsotonicCalibrator(steps) if steps else None


def apply_calibrator(p: float, cal: IsotonicCalibrator | None) -> float:
    """校准器为空时 identity 返回原概率。"""
    return cal.apply(p) if cal is not None else p


def refresh_from_storage(storage: Any, *, path: str | Path = DEFAULT_CALIBRATOR_PATH) -> bool:
    """揭晓回填后重 fit 并落盘（resolve.py / pm_resolve.py 共用）。
    返回是否刷新；样本不足返回 False；异常上抛由调用方降级。"""
    cal = build_calibrator(storage)
    if cal is None:
        return False
    save_calibrator(cal, path)
    return True
