# 修复报告：回测地面真值失效（CC 评审 §2.1，P1）

- 日期：2026-08-27
- 任务：CC 评审报告 §2.1「回测地面真值失效」（P1，0.5h）
- 状态：已完成并实测验证

## 1. 根因

`scripts/compare_backtest.py` 对每题调 `st.resolve_question(qid, bool(q.outcome), ...)`，
但 ForecastBench 官方题集（`data/fb_seed/*.json`，全部 36 期、latest 期 500 题）**无
`resolution` 字段**（实测字段全集：id/source/question/resolution_criteria/background/
market_info_*/freeze_*/resolution_dates(="N/A")/url，无 outcome）。`_parse_item` 对缺失
resolution 一律返回 `outcome=None`，于是：

- `bool(None) = False` → 所有回测题被按「未发生」计分；
- 零样本臂 n=0、brier=NaN；管线臂 Brier=mean(p²)、常数臂 mean((base_rate-0)²)，
  所有 delta_brier 对比无意义；
- NaN 经 `json.dumps`（默认 allow_nan=True）写成非法 JSON 字面量 `NaN`，落到
  `data/compare_backtest.json`。

## 2. 修复内容

### 2.1 outcome=None 显式跳过 + 计数 + 防御（任务①）

`src/predictor/eval/backtest.py`：

- 新增 `require_outcome(outcome) -> bool`：None（未揭晓/无地面真值）直接抛
  `ValueError`，杜绝 None→False 静默转换；非 None 显式 `bool()` 降型。
- `BacktestReport` 增加 `skipped_outcome_missing: int = 0` 字段（带默认值，位置参数
  构造向后兼容）；`run_zero_shot_backtest` 跳过 None 时计数，计分前过
  `require_outcome` 兜底。

`scripts/compare_backtest.py`：

- 管线臂：`q.outcome is None` 时**在建题/跑预测之前**跳过并计数 `pipeline_skipped`，
  resolve 时再过 `require_outcome` 兜底；
- 常数基线臂：同样先跳过并计数 `baseline_outcome_skipped`，配对时过 `require_outcome`；
- 报告各臂新增 `skipped_outcome_missing` 字段，note 说明跳过语义。

### 2.2 NaN 不再写非法 JSON（任务②）

- `src/predictor/eval/backtest.py` 新增 `json_safe(obj)`：递归把非有限 float
  （NaN/±Inf）替换为 null；
- 两个脚本序列化统一改为 `json.dumps(json_safe(report), ..., allow_nan=False)`：
  先清洗 NaN→null，`allow_nan=False` 兜底——任何残留非有限值直接抛错而非静默产坏 JSON；
- `build_compare_report`（从脚本提为纯函数放回 eval 模块）在任一侧无样本时把三个
  delta_brier 对比项显式置 None，避免 NaN 差值混入报告；
- `scripts/backtest.py` 同款 NaN 缺陷顺手修复（同样加 skipped 计数）。

### 2.3 连带修复：零样本提示词模板花括号

工作区 `ZERO_SHOT_TEMPLATE` 曾被改成单花括号 `'{"probability": ...}'`（未提交的坏
改动，git HEAD 为正确的 `'{{...}}'`），`.format(title=...)` 会抛
`KeyError: '"probability"'`——零样本臂一旦有真值可算就崩。该 bug 恰好被「全部
outcome=None → 全部跳过」掩盖。已恢复双花括号。

## 3. 单元测试（任务③）

`tests/eval/test_backtest.py` 13 用例全绿（新增 9 个，覆盖三处）：

- 跳过+计数：`test_zero_shot_skips_none_outcome_and_counts`、
  `test_zero_shot_all_none_outcome_is_nan`；
- 防御：`test_require_outcome_rejects_none`（None 抛 ValueError）、
  `test_require_outcome_passthrough`；
- NaN 序列化：`test_json_safe_replaces_nonfinite_floats_with_null`、
  `test_json_safe_output_is_valid_json_round_trip`（json.loads 实测通过）、
  `test_json_dumps_allow_nan_false_raises_on_nan`（兜底语义）；
- 报告组装：`test_compare_backtest_report_counts_and_valid_json`（跳过数进输出、
  NaN→null、delta 置 None）、`test_compare_backtest_report_delta_with_samples`
  （有样本时 delta 数值正确）。

## 4. 实测验证（任务④）

命令（遵守项目铁律）：
`env -u PYTHONPATH .venv/Scripts/python.exe -E -X utf8 scripts/compare_backtest.py`

- 默认 `--db :memory:`，未触碰 `data/foresight.db`（无锁风险）；fetch 网络降级走本地
  seed；全部题 outcome=None → 零 LLM/零 GDELT 调用；
- 输出三臂：`n=0`、`skipped_outcome_missing=30/30/30`、`constant_base_rate_skipped=0`、
  brier_mean/ci95 全 null、三个 delta 全 null；
- `data/compare_backtest.json` 798 bytes：`json.loads` 通过、无 `NaN`/`Infinity`
  字面量、计数断言全过；
- `scripts/backtest.py` 同测：`n=0, skipped_outcome_missing=50, brier_mean=null`，
  `json.loads` 通过。

质量门：

- `uv run pytest tests/eval/test_backtest.py -q` → **13 passed**；
- `uv run ruff check scripts/compare_backtest.py scripts/backtest.py
  src/predictor/eval/backtest.py tests/eval/test_backtest.py` → **All checks passed**。

## 5. 变更文件

| 文件 | 变更 |
|---|---|
| `src/predictor/eval/backtest.py` | +`require_outcome`/`json_safe`/`build_compare_report`；BacktestReport +skipped 字段；模板恢复双花括号 |
| `scripts/compare_backtest.py` | 两处显式跳过+计数；require_outcome 兜底；json_safe+allow_nan=False |
| `scripts/backtest.py` | 同款 NaN 修复 + skipped 计数 |
| `tests/eval/test_backtest.py` | +9 用例（现 13 个全绿） |
| `data/compare_backtest.json`、`data/backtest_baseline.json` | 实跑产物（合法 JSON） |

未 commit（遵守任务硬约束）；未碰 .env / shell/pi/ / .foresight/SYSTEM.md。

## 6. 备注与后续

- **根本性限制**：fb_seed 无任何地面真值，修复后脚本诚实报告「无可计分样本」。
  要让三臂对比真正出数字，需接入外部答案源（如 manifold API）回填 resolution，
  `benchmarks.py` 模块 docstring 已有此方向注释，建议另立任务。
- **既有问题（非本次引入，未动）**：tests 里 `import scripts.*` 的模式在
  `uv run pytest <单文件>` 下必炸 ModuleNotFoundError（`tests/test_daily.py:30` 实测
  同炸），只有 `python -m pytest`（cwd 进 sys.path）能跑通。本次测试通过把报告组装
  逻辑移入 `predictor.eval.backtest` 规避，未修该既有模式。
- 工作区另有并行会话未提交改动（docs/STATUS.md、web/static/*），本任务未触碰；
  docs/STATUS.md 未同步（避免跨会话文件冲突）。
