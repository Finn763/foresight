# Foresight-PMResolve 每日调度接入修复报告

日期：2026-08-27 | 任务：CC 评审报告 §3.2（P1）| 执行：Hermes 子代理

## 一、问题

`src/predictor/resolution/auto_resolve.py:33-42` 对 source=polymarket 的题显式
`resolution_skipped_polymarket` 跳过（evolution_log 实测 ×6），Polymarket 题揭晓
唯一路径是 `scripts/pm_resolve.py`，但此前全靠手工跑，schtasks 无 pm_* 任务——
题到期后若无人手跑，市场决议窗口过后只能降级 C 走人工，B 类自动揭晓链路对
Polymarket 题实质断链。

## 二、pm_resolve.py 审计与修复（scripts/pm_resolve.py）

审计结论（幂等与错误处理现状）：
- 幂等：`q.outcome is not None → continue` 跳过已揭晓题，重跑不重复回填 ✓
- 单题 try/except：市场决议与 LLM 兜底回填各包 try/except，单题失败不击垮整轮 ✓
- 网络失败：`market_outcome` 任何异常 → None → 走独占窗口/兜底分支，优雅降级 ✓
- 退出码：原 main() 恒 exit 0；Storage 构造撞锁裸 traceback（进程退出码 1 但日志不可读）

修复 4 处（幂等语义不变）：

1. **DB 撞锁优雅退出**：`Storage(args.db) + create_schema()` 包 try/except，
   失败打印「DB 初始化失败（可能被其他轮持锁，下一轮自动重试）：{e}」并 return 1。
   此前裸 traceback。
2. **LLM 客户端构造失败不断链**：原逻辑 `break` 会跳过后续全部题的市场决议检查
   （含本可市场决议的题）。改为 `llm_unavailable` 哨兵：仅 LLM 兜底失效，剩余题
   市场决议照常处理。
3. **退出码统一 0/1**：`main() -> int`（0=整轮完成，1=DB 初始化失败）；
   `argparse` 用法错误从默认 exit 2 改为 exit 1；入口 `sys.exit(main())`。
4. LLM 不可用时打印「市场未决议且 LLM 兜底不可用（待下一轮）」继续，不崩溃。

## 三、计划任务 Foresight-PMResolve（已创建并读回验证）

创建方式：PowerShell `Register-ScheduledTask`，Principal/Settings 整体拷贝自
现有 Foresight-Resolve 任务（`-Settings $src.Settings`），仅 Action/Trigger/Description 不同。
读回（Get-ScheduledTask + Export-ScheduledTask XML）确认全字段：

| 字段 | 值 |
|---|---|
| TaskName / TaskPath / State | Foresight-PMResolve / \ / Ready |
| Principal | UserId=SYSTEM（S-1-5-18）、LogonType=ServiceAccount、RunLevel=Highest |
| Trigger | CalendarTrigger Daily，StartBoundary=2026-08-27T21:00:00+08:00，DaysInterval=1，无 EndBoundary |
| Action.Execute | C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.13-windows-x86_64-none\pythonw.exe（base GUI pythonw，无窗口化） |
| Action.Arguments | "D:\code\Foresight\scripts\run_silent.py" "scripts\pm_resolve.py" "data\pm-resolve.log" |
| Action.WorkingDirectory | D:\code\Foresight |
| Settings | MultipleInstances=IgnoreNew、Compatibility=Win7、AllowDemandStart=True、AllowHardTerminate=True、Enabled=True、ExecutionTimeLimit=PT2H、Priority=7、StartWhenAvailable=True、StopIfGoingOnBatteries=True、DisallowStartIfOnBatteries=True、WakeToRun=False、RunOnlyIfIdle=False、RunOnlyIfNetworkAvailable=False、UseUnifiedSchedulingEngine=True、Hidden=False |
| Description | Foresight Polymarket 揭晓：每日 21:00 跑 scripts/pm_resolve.py（市场决议优先 + LLM 兜底），stdout/stderr 落 data\pm-resolve.log |
| NextRunTime（创建后读回） | 2026-08-27 21:00:00 |

与 Foresight-Resolve 逐项对照：Principal（SYSTEM/ServiceAccount/Highest）、Settings
全部一致；差异仅 TaskName、触发时刻（16:30→21:00）、action 参数（evolve.py resolve →
pm_resolve.py）、Description。

## 四、实测触发

- 触发方式：`Start-ScheduledTask -TaskName Foresight-PMResolve`（PowerShell，
  等效 schtasks /Run；项目约定 bash 里 schtasks 输出不可靠，查询一律 PowerShell）。
- 结果：**LastRunTime=2026-08-27 18:56:41，LastTaskResult=0 (0x00000000)**。
- 实机日志（data/pm-resolve.log，run_silent 落盘）：
  ```
  ===== run_silent 2026-08-27 18:56:41 → pm_resolve.py =====
  #75 市场未决议且 LLM 兜底不可判（待人工/下一轮）：GPT-6 会在2026年8月21日前发布吗？
  本轮揭晓 0 题
  ```
- 实机行为说明：本次无 DuckDB 撞锁（18:56 不在生产轮窗口；若 daily/evolve 持锁，
  现为修复 1 的优雅 exit 1 路径，LastTaskResult 非零可查）。唯一到期未揭晓题 #75
  市场未决议、LLM 兜底护栏不可判 → 打印待下一轮、整轮退出 0，符合设计。

## 五、测试与静态检查

新增单测（tests/data/test_polymarket_source.py，沿用该文件 importlib 加载
scripts/pm_resolve.py 的既有模式）：

- `test_main_db_init_failure_returns_1`：Storage 构造抛异常（模拟撞锁）→ main()==1、
  输出含「持锁」、无 Traceback。
- `test_main_llm_unavailable_does_not_block_market_and_rerun_idempotent`：
  3 题（市场决议/超窗未决议/市场决议），LLMClient 构造强制抛异常 → 首轮 m1/m3
  照常市场决议、m2 跳过；重跑 main()==0 且「本轮揭晓 0 题」→ 幂等。

结果：
- `pytest tests/data/test_polymarket_source.py`：**15 passed**（13 既有 + 2 新增）
- `pytest tests/resolution/test_auto_resolve.py`：**9 passed**（确认跳过链路未受影响）
- `ruff check scripts/pm_resolve.py tests/data/test_polymarket_source.py`：**All checks passed**

## 六、文件清单与边界

- 改：`scripts/pm_resolve.py`（4 处，见 §二）
- 改：`tests/data/test_polymarket_source.py`（+2 测试 + 2 个 import）
- 新：`docs/hermes-fix-report-pmresolve-schedule-2026-08-27.md`（本报告）
- 系统状态：新建 schtasks 任务 Foresight-PMResolve（每日 21:00）
- 未动：src/**（含 auto_resolve.py）、.env、shell/pi/、.foresight/、data/foresight.db
  无写入性变更（实测运行仅走正常揭晓逻辑）；未 commit。

## 七、遗留建议（超出本次文件边界，供决策）

- `docs/STATUS.md` 的任务清单（§「定时任务」）建议补一行 Foresight-PMResolve 21:00，
  本次未改（文件边界仅限 scripts/pm_resolve.py + tests/ + 本报告）。
