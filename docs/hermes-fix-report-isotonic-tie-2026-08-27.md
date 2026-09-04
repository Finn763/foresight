# 修复报告：保序回归 tie 处理加权错误（CC §2.5，P2）

日期：2026-08-27
文件：`src/predictor/calibration/isotonic.py` + `tests/calibration/test_isotonic.py`

## 问题

PAVA 本体正确（严格逆序合并、单调性、apply 中点+端点外推均实测正确），但同 x 跨块聚合处每块贡献 2 个端点、块样本数 n 不参与加权。

实测复现（修复前）：`fit_isotonic([0.5,0.5,0.5,0.5],[F,F,T,F])` → steps=[(0.5, 0.1667)]，正确频率应为 0.25——偏差 33%。

根因：3 个终态块（v=0,n=1）、（v=0,n=1）、（v=0.5,n=2）各贡献 2 个端点共 6 个 y 值，简单均值 = (0+0+0+0+0.5+0.5)/6 = 1/6 ≈ 0.1667，与块大小无关。

## 修法

同 x 聚合改按块样本数 n 加权：`sum(y·n)/sum(n)`。块在其区间左右端点各贡献一次（端点相同时只贡献一次，避免重复计数）；聚合结果按 x 升序保持为 `list[tuple[float, float]]`，API 与输出结构不变（`IsotonicCalibrator.steps`、`apply` 中点+外推语义均未动）。

修复后复现实测：`[(0.5, 0.25)]` ✓

## 单测新增（tests/calibration/test_isotonic.py，+3）

1. `test_isotonic_tie_weighted_by_block_size` —— §2.5 复现用例：4 同分 1 真 → steps=[(0.5, 0.25)]、apply(0.5)=0.25
2. `test_isotonic_tie_weighted_across_multiple_blocks` —— 同 x 多块不同大小：终态块 [0(n1), 0(n1), 0.25(n4), 0.5(n2)] → (0+0+1+1)/8 = 0.25（旧端点均值 = 0.1875）
3. `test_isotonic_tie_weighted_monotonicity` —— 同分跨块 + 多区间混合的单调性回归：steps=[(0.1,0),(0.5,0.2),(0.9,0.6)]，apply 单调不减且值域 [0,1]

## 验证结果

- `pytest tests/calibration -q`：23 passed（含原 4 条 isotonic 用例无回归）
- `pytest tests/resolution/test_auto_resolve.py -q`（校准器刷新路径，CC §2.4 关联）：14 passed
- `ruff check`（两个改动文件）：All checks passed
- 直接复现脚本：`[(0.5, 0.25)]`、`[(0.5, 0.25)]`

## 改动边界

仅 `src/predictor/calibration/isotonic.py`（聚合段 :45-58）与 `tests/calibration/test_isotonic.py`。未 commit、未触碰 .env / shell/pi/ / .foresight/。
