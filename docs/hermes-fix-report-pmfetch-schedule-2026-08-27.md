# Foresight-PMFetch 每日调度接入报告

日期：2026-08-27 | 任务：Polymarket 拉题脚本接入 schtasks | 执行：Hermes 子代理

## 一、问题

`scripts/pm_fetch.py` 是 Polymarket 题池唯一拉题入口（Gamma API 拉活跃市场 →
horizon 三档筛选 → LLM 译中文 → 入题池），但此前全靠手工跑，schtasks 无
pm_fetch 任务——每日 21:00 的 Foresight-PMResolve 只揭晓、不拉新题，Polymarket
题源入池实质断链。本任务建每日 20:00 调度（赶在 21:00 揭晓轮之前）。

## 二、pm_fetch.py 审计与修复（scripts/pm_fetch.py）

审计结论（幂等与错误处理现状）：
- market_id 判重 ✓：`known = st.source_market_ids("polymarket")`（读 resolution_spec
  JSON 中已入库 market_id 集合），`fresh = [c for c in candidates if c.market_id
  not in known]`——重跑不重复建题；入库时 market_id 写入 resolution_spec，闭环。
- 日期语境去重 ✓：`select_candidates` 内 `_norm_question` 只通配日期数字做全局
  去重，同主题仅日期不同（"GPT-6 by Aug 21" vs "Aug 31"）只保留最早结束一个，
  跨档、跨 event 均去重（既有测试覆盖）。
- dry-run 不落库 ✓；LLM 译失败回退英文题面 ✓；单事件市场拉取失败跳过 ✓。
- 退出码：原 `main() -> None` 恒 exit 0；Storage 构造撞锁裸 traceback；
  事件列表网络失败打印消息后 exit 0（失败被掩盖为成功）；argparse 用法错误 exit 2。

修复 5 处（幂等语义不变）：

1. **DB 撞锁优雅退出**：`Storage + create_schema + source_market_ids` 包
   try/except，失败打印「DB 初始化失败（可能被其他轮持锁，下一轮自动重试）：{e}」
   并 return 1。此前裸 traceback。
2. **网络失败 exit 1**：事件列表拉取失败仍打印「网络降级，本轮终止」可读消息，
   但退出码 0 → 1（LastTaskResult 非零可查，不再掩盖失败）。
3. **退出码统一 0/1**：`main() -> int`（0=整轮完成含单题降级，1=DB 初始化失败/
   网络失败）；argparse 用法错误从默认 exit 2 改为 exit 1；入口 `sys.exit(main())`。
4. **单题入库降级**：`add_question` 包 try/except，失败打印「入库失败（降级，待
   下一轮）」继续（与 pm_resolve 同纪律）——daily/evolve 轮中途抢锁时不再裸
   traceback，已入库题不受影响，未入库题由 market_id 判重保证下一轮不重复。
5. 保持：ThreadPoolExecutor 单事件失败跳过、LLM 客户端构造失败降级英文题面。

## 三、计划任务 Foresight-PMFetch（已创建并读回验证）

创建方式：PowerShell `Register-ScheduledTask`，Principal/Settings 整体拷贝自
现有 Foresight-Resolve 任务（`-Principal $src.Principal -Settings $src.Settings`），
仅 Action/Trigger/Description 不同。Get-ScheduledTask 读回全字段：

| 字段 | 值 |
|---|---|
| TaskName / TaskPath / State | Foresight-PMFetch / \ / Ready |
| Principal | UserId=SYSTEM（S-1-5-18）、LogonType=ServiceAccount、RunLevel=Highest |
| Trigger | CalendarTrigger Daily，StartBoundary=2026-08-27T20:00:00+08:00，DaysInterval=1，无 EndBoundary |
| Action.Execute | C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.13-windows-x86_64-none\pythonw.exe（base GUI pythonw，无窗口化） |
| Action.Arguments | "D:\code\Foresight\scripts\run_silent.py" "scripts\pm_fetch.py" "data\pm-fetch.log" |
| Action.WorkingDirectory | D:\code\Foresight |
| Settings | MultipleInstances=IgnoreNew、Compatibility=Win7、AllowDemandStart=True、AllowHardTerminate=True、Enabled=True、ExecutionTimeLimit=PT2H、Priority=7、StartWhenAvailable=True、StopIfGoingOnBatteries=True、DisallowStartIfOnBatteries=True、WakeToRun=False、RunOnlyIfIdle=False、RunOnlyIfNetworkAvailable=False、UseUnifiedSchedulingEngine=True、Hidden=False |
| Description | Foresight Polymarket 拉题：每日 20:00 跑 scripts/pm_fetch.py（拉活跃市场 -> 三档筛选 -> LLM 译中文 -> 入题池，默认 is_public=False），stdout/stderr 落 data\pm-fetch.log |
| NextRunTime（创建后读回） | 2026-08-28 20:00:00（创建时 20:52 已过当日 20:00，首跑顺延次日，属预期） |

与 Foresight-Resolve 逐项对照：Principal（SYSTEM/ServiceAccount/Highest）、Settings
全部一致；差异仅 TaskName、触发时刻（16:30→20:00）、action 参数（evolve.py resolve
→ pm_fetch.py）、Description。20:00 早于 21:00 的 Foresight-PMResolve，拉题先于揭晓。

## 四、实测触发

- 触发方式：`Start-ScheduledTask -TaskName Foresight-PMFetch`（PowerShell）。
- 结果：**LastRunTime=2026-08-27 20:54:31，LastTaskResult=0 (0x00000000)**，
  任务 33 秒内完成，状态回 Ready。
- 实机日志（data/pm-fetch.log，run_silent 落盘，UTF-8 无乱码无 Traceback）：
  ```
  ===== run_silent 2026-08-27 20:54:31 → pm_fetch.py =====
  警告：事件扫描达上限 500，90 天窗口可能未扫完（长档候选不完整），建议提高 --max-events
  事件 500 个 → 市场 4386 个
  候选 1（去重后新增 1）
    #109 [2026-09-15] vol=$1,677 俄罗斯会在9月15日前进入多布罗皮利亚吗？
  本轮入库 1 题
  ```
- 实机行为说明：20:54 不在生产轮窗口，无 DuckDB 撞锁，整轮走完整链路成功——
  扫满 --max-events 上限 500 事件（触发内置警告）、并发拉 4386 市场、三档筛选+
  日期语境去重后仅 1 个候选、LLM 翻译成功、market_id 判重入库 1 题（#109）。
- 入库读回验证（生产库 read_only 连接）：#109 is_public=False、
  resolution_class=B、resolution_spec source=polymarket /
  market_id=3759151 / slug=will-russia-enter-dobropillia-by-september-15；
  库内 polymarket 题共 11（10 道既有 + 本次 1 道，market_id 判重未重复建题）。
- 撞锁场景（本次未发生，已由单测覆盖）：DB 初始化撞锁 → 优雅 exit 1；
  入库中途撞锁 → 单题降级继续，整轮 exit 0。

## 五、测试与静态检查

新增单测（tests/data/test_polymarket_source.py，沿用该文件 importlib 加载
scripts/pm_fetch.py 的既有模式）：

- `test_pm_fetch_main_db_init_failure_returns_1`：Storage 构造抛异常（模拟撞锁）
  → main()==1、输出含「持锁」、无 Traceback。
- `test_pm_fetch_main_network_failure_returns_1`：fetch_events 抛 ConnectError
  → main()==1、输出含「网络降级」、无 Traceback。
- `test_pm_fetch_main_ingest_and_rerun_idempotent`：首轮入库 1 题
  （source_market_ids == {"m-idem-1"}）；重跑 main()==0、「去重后新增 0」、
  「本轮入库 0 题」、库内仍恰 1 题 → market_id 判重幂等。
- `test_pm_fetch_main_add_failure_degrades_gracefully`：add_question 抛异常
  （模拟中途撞锁）→ main()==0、输出含「降级」、无 Traceback、不击垮整轮。

结果：
- `pytest tests/data/test_polymarket_source.py`：**19 passed**（15 既有 + 4 新增）
- `pytest tests/data/test_storage.py tests/test_storage_readonly.py`：**22 passed**
  （pm_fetch 依赖的 Storage 接口未受影响）
- `ruff check scripts/pm_fetch.py tests/data/test_polymarket_source.py`：**All checks passed**

## 六、文件清单与边界

- 改：`scripts/pm_fetch.py`（5 处，见 §二）
- 改：`tests/data/test_polymarket_source.py`（+4 测试 + 2 个辅助函数）
- 新：`docs/hermes-fix-report-pmfetch-schedule-2026-08-27.md`（本报告）
- 系统状态：新建 schtasks 任务 Foresight-PMFetch（每日 20:00）
- 未动：src/**、.env、shell/pi/、.foresight/；data/foresight.db 无直接写入
  （实测运行仅走正常拉题入库逻辑，只读回读验证）；未 commit。

## 七、遗留建议（超出本次文件边界，供决策）

- 实机扫描命中 `--max-events` 上限 500 且警告「90 天窗口可能未扫完（长档候选
  不完整）」：若长档题源重要，可将任务 action 追加 `--max-events 1500`（经
  run_silent 透传），或直接调脚本默认值。
- 实机 4386 市场仅 1 个候选过筛（volume≥$1000 + 二值 + 90 天窗 + 日期语境去重），
  筛子偏严属参数问题非缺陷，如需更多题可调 `--min-volume`/`--per-tier`。
- `docs/STATUS.md` 的任务清单（§「定时任务」）建议补一行 Foresight-PMFetch
  20:00，本次未改（文件边界仅限 scripts/pm_fetch.py + tests/ + 本报告）。
