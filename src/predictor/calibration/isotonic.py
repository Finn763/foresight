# calibration/isotonic.py
"""PAVA 保序回归：无 scipy 依赖，~40 行。"""

from dataclasses import dataclass, field


@dataclass
class _Block:
    sum_p: float
    sum_o: float
    n: int
    preds: list[float] = field(default_factory=list)
    outcomes: list[bool] = field(default_factory=list)

    @property
    def value(self) -> float:
        return self.sum_o / self.n if self.n else 0.0


def fit_isotonic(preds: list[float], outcomes: list[bool]):
    """返回 IsotonicCalibrator。要求输入按 preds 升序或自动排序（用索引）。"""
    order = sorted(range(len(preds)), key=lambda i: preds[i])
    blocks: list[_Block] = []
    for i in order:
        blocks.append(
            _Block(
                sum_p=preds[i],
                sum_o=int(outcomes[i]),
                n=1,
                preds=[preds[i]],
                outcomes=[outcomes[i]],
            )
        )
        while len(blocks) >= 2 and blocks[-2].value > blocks[-1].value:
            b2 = blocks.pop()
            b1 = blocks.pop()
            merged = _Block(
                sum_p=b1.sum_p + b2.sum_p,
                sum_o=b1.sum_o + b2.sum_o,
                n=b1.n + b2.n,
                preds=b1.preds + b2.preds,
                outcomes=b1.outcomes + b2.outcomes,
            )
            blocks.append(merged)
    # 每个块的常数值 = 块内 outcome 频率；块在其区间左右端点各贡献一次该值。
    # 重复概率点可能落在不同块（同 x 不同 y，PAVA 只在严格逆序时合并），
    # apply 会命中首个匹配块丢失组内信息 → 同 x 聚合。
    # 权重 = 块样本数 n（sum(y·n)/sum(n)）：块大小不参与加权时，同 x 多块
    # 频率被等权稀释（评审报告 §2.5：4 样本 1 真 → 0.1667 而非 0.25）。
    merged: dict[float, list[tuple[float, int]]] = {}
    for b in blocks:
        merged.setdefault(b.preds[0], []).append((b.value, b.n))
        if b.preds[-1] != b.preds[0]:
            merged.setdefault(b.preds[-1], []).append((b.value, b.n))
    steps: list[tuple[float, float]] = [
        (x, sum(y * n for y, n in entries) / sum(n for _, n in entries))
        for x, entries in merged.items()
    ]
    return IsotonicCalibrator(steps)


@dataclass
class IsotonicCalibrator:
    steps: list[tuple[float, float]]

    def apply(self, p: float) -> float:
        lo, hi = self.steps[0][1], self.steps[-1][1]
        for x, y in self.steps:
            if p <= x:
                hi = y
                break
            lo = y
        return max(0.0, min(1.0, (lo + hi) / 2))
