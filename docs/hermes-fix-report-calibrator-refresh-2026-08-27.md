# Hermes 修复报告：校准器刷新时机 + resolver 异常日志（CC §2.4 / §2.7③）

- 日期：2026-08-27
- 任务：CC 评审报告 P2 两项（合计 1h）：①§2.4 16:30 自动揭晓路径不刷新校准器；②§2.7③ auto_resolve 吞 resolver 异常无日志
- 结论：两项均已修复，tests/resolution/ 81 用例全绿，ruff 干净，未 commit

## ① §2.4 校准器刷新时机缺口（已修复）

**问题**：`refresh_from_storage` 只在人工路径调用（scripts/resolve.py:83-90、scripts/pm_resolve.py:130）；
每日 16:30 最大宗自动揭晓路径 `evolve.resolve_round → auto_resolve` 揭晓后不刷新
`data/calibrator.json`，一旦跨过 30 样本生产 websearch 概率将用陈旧校准器。

**修法**（按任务边界，刷新逻辑放 auto_resolve 尾部，未改 resolve_round/evolve.py）：

- `src/predictor/resolution/auto_resolve.py` 模块顶部新增
  `from predictor.calibration.calibrate import refresh_from_storage`
  （已核实无 import 循环：calibrate.py 只依赖 stdlib-only 的 isotonic.py）
- 揭晓循环末尾、`return stats` 之前：`stats["resolved"] > 0` 时调用 `refresh_from_storage(storage)`
  （签名 `refresh_from_storage(storage, *, path=DEFAULT_CALIBRATOR_PATH) -> bool`，
  样本不足返回 False 不写盘；异常上抛）
- 失败降级：`except Exception` 记 `evolution_log` 事件 `calibrator_refresh_failed`
  （detail 含异常类型+消息），不抛不阻塞本轮结果
- 新增事件类型已核实无消费方白名单约束（storage.log_evolution 接受任意 TEXT event_type）

## ② §2.7③ resolver 异常吞日志（已修复）

**问题**：`auto_resolve.py:56` `except Exception: outcome=None` 吞掉 resolver 内部异常无日志，
与其它路径 resolution_failed 日志不一致。

**修法**：

- `except Exception as e` 捕获异常，记录 `resolve_error = f"{type(e).__name__}: {e}"`
- else 分支（计 degraded）优先使用 `resolve_error` 作 detail（异常路径不做数据窗口分类，
  那是业务性 None 专属）；每题仍只记一条 resolution_failed，与既有口径一致
- detail 含题 id（`"qid": q.id`）+ 异常类型 + 消息

## 测试（tests/resolution/test_auto_resolve.py 新增 5 例）

1. `test_auto_resolve_resolver_exception_logs_qid_and_type` — mock resolver 抛异常：
   断言 degraded=1、单条 resolution_failed 含 qid + RuntimeError + 消息
2. `test_auto_resolve_resolver_exception_keeps_round_alive` — 两题一炸一正常：
   断言整轮继续、resolved=1/degraded=1、异常题记 resolution_failed
3. `test_auto_resolve_refreshes_calibrator_when_resolved` — mock refresh 断言 resolved>0 时被调一次
4. `test_auto_resolve_no_refresh_when_nothing_resolved` — resolved==0 时不调 refresh（pytest.fail 哨兵）
5. `test_auto_resolve_refresh_failure_logs_and_does_not_crash` — mock refresh 抛 OSError：
   断言揭晓结果不受影响 + 记 calibrator_refresh_failed 含异常类型/消息

## 验证

- `env -u PYTHONPATH .venv/Scripts/python.exe -E -X utf8 -m pytest tests/resolution/ -q`：**81 passed**
  （原 76 + 新增 5；既有 12 例含 resolved>0 的用例由真实 refresh 路径穿过——
  :memory: 样本 <30 → 返回 False 不写盘，无副作用）
- `ruff check src/predictor/resolution/auto_resolve.py tests/resolution/test_auto_resolve.py`：**All checks passed**
- 两文件行尾保持纯 CRLF（与仓库一致）；未跑全量 pytest（任务限定相关文件；flaky 名单见项目 skill）

## 变更文件

- `src/predictor/resolution/auto_resolve.py`（+34/-19：顶部 import、异常捕获、else 分支重构、尾部刷新）
- `tests/resolution/test_auto_resolve.py`（+117：5 个新用例 + 2 个 fake resolver 类）

## 边界与备注

- 未碰 evolve.py / resolve_round（并行任务占用）：16:30 路径刷新由 auto_resolve 尾部实现，
  覆盖等价（resolve_round 第②步即 `stats = auto_resolve(st, now)`）
- 未 commit、未碰 .env / shell/pi/ / .foresight/、未写生产库（测试全程 DuckDB :memory:）
- **uv.lock 侧效应已还原**：`uv run ruff check` 在用户级 uv.toml（Aliyun 镜像 index-url）下
  会自动把 uv.lock 的 registry 重解析为镜像 URL——本任务 lint 触发过一次，已 `git restore uv.lock`；
  后续任何 `uv run` 都会再次触发该环境性改写（非代码变更，建议父任务知晓）
- 并行观察：会话期间 src/predictor/calibration/isotonic.py、src/predictor/websearch_predictor.py
  有并发修改活动（19:46-19:47 mtime，属兄弟修复任务，非本任务改动）
- docs/STATUS.md 未动（文件边界外），如需可补一条「16:30 自动揭晓后校准器自动刷新已闭环」
