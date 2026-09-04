# CC §4.3 修复报告：7 天更新规则按小时差判定（2026-08-27）

**任务**：修复「7 天更新规则按日历日差漂移」——`scripts/daily.py`、`scripts/evolve.py`
两处 `(now - last).days < 7` 改 7×24 小时差判定，补边界单测，定向测试全绿 + ruff 干净。

## 一、现场确认

CC 报告 §4.3 引用的行号（daily.py:164、evolve.py:137）因后续 P0 修复已偏移，
按内容精确核对到现场：

| 文件 | 原行号（报告） | 实际行号（修复时） | 内容 |
|---|---|---|---|
| scripts/daily.py | 164 | 145 | `if last is not None and (now - last).days < 7:` |
| scripts/evolve.py | 137 | 83 | `if last is not None and (now - last).days < 7:` |

内容与报告完全一致，两处均确认并修复。全仓（scripts/）复查无其它 `.days < N` 类门槛残留。

## 二、改动内容

### scripts/daily.py（唯一真源，双轨共用）
1. `from datetime import datetime, timedelta`（补 timedelta 导入）。
2. `_log_event` 之后新增共享纪律常量与判定函数：
   - `_UPDATE_WINDOW = timedelta(hours=7 * 24)` —— 7×24 小时窗口常量；
   - `_within_update_window(now, last) -> bool` —— 距上次预测未满 7×24h 返回 True（跳过更新）。
     判定式 `(now - last) < _UPDATE_WINDOW` 即小时差比较（与
     `(now - last).total_seconds() < 7*24*3600` 等价）；naive 同域按流逝时长，
     aware 同域跨 DST 也按真实秒差（不随墙钟日历日漂移）。
3. 预测循环门槛改 `if last is not None and _within_update_window(now, last): continue`，
   注释同步改为「未满 7×24h」。

### scripts/evolve.py
1. `predict_round` 内 `from scripts.daily import ...` 增加 `_within_update_window`
   （与既有 `_log_event`/`_predict_safely` 同一既有模式，保持 daily 为单一真源）。
2. 预测循环门槛改同一调用。
3. 模块 docstring 与 `predict_round` docstring 的「7天更新」措辞同步为「7×24h 更新」。

### tests/test_daily.py（+2 用例）
- `test_update_window_boundary_7x24h`：6.9 天（6d21h36m）→ 窗口内跳过；
  恰满 7×24h → 窗口外允许更新（阈值 >= 语义，防边界悬空永不更新）；
  7.1 天 → 窗口外允许更新。
- `test_update_window_dst_independent_elapsed_semantics`：跨夏令时语义——2026-03-08
  美东春令时拨快 1h，`3-08 00:30 EST → 3-15 01:00 EDT` 墙钟日历日差恰为 7
  （date 相减 .days==7），但真实流逝 6d23h30m < 7×24h → 仍判窗口内跳过；
  若回归为日历日差实现会在此误判为可更新。用固定 UTC offset 构造（不依赖 tzdata）。

### tests/test_evolve.py（+1 用例）
- `test_predict_round_7day_update_window_elapsed_hours`：编排层接线验证——建题并回填
  `predictions.created_at` 控制「上次预测时间」（monkeypatch `predict_with_websearch`
  落库 fake Prediction），`predict_round` 下：6.9 天前 → 跳过（预测数不变 ==1）；
  7.1 天前 → 补跑（预测数 ==2）。断言只看目标题计数，不受题族补充噪声影响。

## 三、测试与 lint 证据

```
env -u PYTHONPATH uv run pytest -p no:cacheprovider tests/test_daily.py tests/test_evolve.py
→ 24 passed in 6.25s            （exit 0，全绿）

env -u PYTHONPATH uv run ruff check scripts/daily.py scripts/evolve.py tests/test_daily.py tests/test_evolve.py
→ All checks passed!            （exit 0，干净）
```

新加 3 个用例均含在上述 24 个通过用例内；`-p no:cacheprovider` 规避并行任务
互踩 .pytest_cache。

## 四、诚实说明（语义等价性 + 遗留风险）

1. **表达式改写对当前 naive 数据路径运行时行为不变**：对非负 timedelta，
   `days = floor(total_seconds/86400)`，故 `.days < 7 ⟺ total_seconds < 7×24h`
   数学恒等价（实测：8-20 09:39 → 8-27 09:00，`.days==6` 与小时差判定同为跳过）。
   本次改动的价值在：① 语义显式锚定（7×24h 常量 + 单一真源 helper，daily/evolve 共用）；
   ② 时区/DST 无关的流逝时长语义被测试锁定，防未来回归为 date 相减的日历日差；
   ③ 阈值边界（恰满 7×24h 即允许更新）有了回归测试。
2. **CC §4.3 描述的漂移现象（daily 09:00 漏题→evolve 16:30 补跑、更新时刻逐日滑向
   16:30、日预测量 28 题超设计）不会被本次阈值改写消除**：其根因是「轮次开始时刻
   09:00 vs 上次预测时间戳 09:39」的时分错位——无论哪种阈值表达式，6.96 天未满
   7×24h 都会跳过。报告给出的另一条路径（合并 daily/evolve 单入口消除双轨漂移）
   超出本任务文件边界与指定改法，留作上层决策（需动调度与编排入口，非本次 P1 范围）。

## 五、结论

- 两处判定已改为 7×24 小时差（共享 `scripts.daily._within_update_window`，evolve 复用）；
- 边界（6.9 天/恰满 7×24h/7.1 天）与跨夏令时流逝时长语义均有单测锁定；
- `tests/test_daily.py` + `tests/test_evolve.py` 24 用例全绿，ruff 干净；
- 未 commit、未碰 .env / shell/pi/ / .foresight/，未动 DB（全部测试用 :memory:/tmp 库）。
