# Foresight 修复报告（2026-08-27）— CC §3.4 dashboard 守护 + 日志编码混血（P2）

> 性质：按评审报告 `docs/cc-improvement-analysis-2026-08-27.md` §3.4 执行修复（含 §3.3 的
> server 日志读取部分）。
> 未 commit、未 push、未改数据。文件边界严格遵守：仅新建 `scripts/ensure_dashboard.py`、
> 改 `src/predictor/web/server.py` 一处日志读取编码、新建 `tests/test_ensure_dashboard.py`。
> 未触碰 storage.py 等并行任务占用文件、.env、shell/pi、.foresight、STATUS.md（留给协调方同步）。

---

## 1. 结论摘要

| 项 | 结果 |
|---|---|
| dashboard ensure 脚本 | ✅ 新建 `scripts/ensure_dashboard.py`，仿 `ensure_signsrv.py` 幂等三态自检/拉起 |
| 计划任务 | ✅ **Foresight-Dashboard**（SYSTEM/Highest，每天 00:00 + PT30M repetition），配置逐字段对齐 Foresight-Resolve |
| 任务实测 | ✅ demand start → LastTaskResult=0；ensure 日志记录「alive on 8765, skip」；NextRunTime=21:30（30 分钟节奏） |
| 日志编码 | ✅ server.py 日志读取 `gbk`→`utf-8` + `errors="replace"`（仅此一行，告警横幅等未动） |
| 单测/lint | ✅ 新增 10 用例全绿；相关 6 文件 pytest 全绿；ruff 三文件干净 |

---

## 2. §3.4a — dashboard 无守护：接入 ensure 体系

### 根因
8765 服务 8/25 17:36 手工启动（PID 15752，控制台子系统 python），无 ensure 机制，机器重启
即丢，与 signsrv 待遇不对称；`open_dashboard.bat` 用 `start cmd /k` 弹常驻控制台。

### 改动：`scripts/ensure_dashboard.py`（新增，仅标准库）
仿 `ensure_signsrv.py` 的探测/拉起/独立日志模式：

1. **探测三态** `probe()`：`listening(8765)`（connect_ex）+ 深度校验 `GET /api/health` == 200。
   - `alive`（端口在 + HTTP 200）→ 静默 exit 0（幂等）；
   - `occupied`（端口被占但 /api/health 非 200）→ exit 1，让 LastTaskResult 可见，**绝不误拉**；
   - `down` → 进入拉起路径。
   - `/api/health` 是刻意设计的探活端点（缺库/被锁返回 200+degraded，见 server.py:117-128），
     200 即视为降级存活。
2. **拉起** `launch()`：`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW |
   CREATE_BREAKAWAY_FROM_JOB`（同 ensure_signsrv 四旗标），命令
   `.venv\Scripts\python.exe -E -X utf8 scripts\web_server.py`（cwd=项目根），env 中
   **移除 PYTHONPATH**（项目铁律）+ `PYTHONIOENCODING=utf-8`（防中文 banner 在 cp936 下崩溃）；
   stdout/stderr → `data/web_server.log`（新增独立服务日志，首次真实拉起时创建）。
3. **拉起后验证** `_wait_up(30s)`：轮询端口 + HTTP 200，防启动崩溃假成功（signsrv 同款）。
4. **独立自检日志** `data/dashboard-ensure.log`（不入服务日志）。
5. 决策函数 `main(probe_fn, launch_fn, wait_fn, venv)` 全部可注入——单测 mock 探测函数即可
   覆盖全部分支；`launch(argv)` 支持 `--port` 透传用于隔离演练。

### 计划任务 Foresight-Dashboard（PowerShell Register-ScheduledTask 创建，读回验证）

| 字段 | 值 | 与 Foresight-Resolve 对比 |
|---|---|---|
| Action.Execute | `C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.13-windows-x86_64-none\pythonw.exe` | 相同（base GUI pythonw 无窗口铁律） |
| Action.Arguments | `"D:\code\Foresight\scripts\run_silent.py" "scripts\ensure_dashboard.py" "data\dashboard-ensure.log"` | 同形（run_silent 包装） |
| Action.WorkingDirectory | `D:\code\Foresight` | 相同 |
| Trigger | Daily，StartBoundary=2026-08-27T00:00:00+08:00，**Repetition PT30M / P365D / StopAtDurationEnd=False** | Resolve 无 repetition；repetition 形态照抄 foresight-signsrv（PT1H/P365D） |
| Principal | SYSTEM / ServiceAccount / **Highest** | 相同 |
| Settings | Enabled；PT2H；MultipleInstances=IgnoreNew；DisallowStartIfOnBatteries=True；StopIfGoingOnBatteries=True；StartWhenAvailable=True；AllowHardTerminate=True；RestartCount=0；Priority=7；Compatibility=Win7 | 逐字段相同（读回确认） |

### 实测记录（2026-08-27 21:06）
- 注册后 `Get-ScheduledTask` 读回：上表全部字段逐一核对 ✓。
- `Start-ScheduledTask` 实测触发，20s 后 `Get-ScheduledTaskInfo`：**LastTaskResult=0**，
  LastRunTime=21:06:01，NextRunTime=21:30:00（30 分钟节奏成立），State=Ready。
- `data/dashboard-ensure.log` 实况：
  ```
  ===== run_silent 2026-08-27 21:06:01 → ensure_dashboard.py =====
  [ensure 2026-08-27 21:06:01] alive on 8765 (/api/health 200), skip
  ```
- `data/web_server.log` 未创建——探测发现手工实例存活即跳过，无多余拉起 ✓。
- 手工实例 PID 15752 全程未动（Get-NetTCPConnection 复核仍归其所有，/api/health 200）。
- 无弹窗：任务 action 为 GUI 子系统 pythonw + run_silent 进程内 runpy，无控制台可弹。
- **拉起路径实机演练**（不碰 8765）：把 `ed.PORT` 临时改为 8799 调真实 `launch(argv=["--port","8799"])`
  → DETACHED 子进程成功绑定并应答 /api/health 200 → `_wait_up` 在 30s 内确认 `alive` → terminate
  干净退出。证明「down→拉起→验证」链路真实可用，非仅单测。

### 已知行为
- 重启后手工实例（15752）消失，下一个 30 分钟窗口由任务以 **SYSTEM** 身份拉起（.env ACL 含
  SYSTEM:(F)，DuckDB read-only 访问无碍）；此后 8765 归 SYSTEM 管理。若用户手动跑
  `open_dashboard.bat`，bat 的 netstat 预检会检测到 LISTENING 只开浏览器，不冲突。

---

## 3. §3.4b — 日志编码混血：server.py 读日志改 utf-8

### 根因
`src/predictor/web/server.py` 的 `/api/ops/log-files` 端点（评审报告 §3.4 写行号 143，本次
核实实际在 **:234**）仍用 `encoding="gbk", errors="replace"` 读 daily.log/evolve.log；
而 `run_silent.py` 自 2026-08-21 起全部以 **utf-8** append 写日志（新行中文正常），GBK 读取
utf-8 新行 → dashboard 日志页乱码。

### 改动（仅一行）
```diff
-            text = log_path.read_text(encoding="gbk", errors="replace")
+            text = log_path.read_text(encoding="utf-8", errors="replace")
```
告警横幅、health、其它端点一律未动。

### 实测（TestClient，临时目录，不碰真实 data/）
- 混血文件（utf-8 新行 + GBK 旧行）→ 200；utf-8 新行「新行: 预测轮启动完成 ✓」完整可读；
- GBK 旧行读出为替换字符乱码但**不崩**（errors=replace 兜底）——属预期，见下。

### 遗留建议（按任务要求留给人工）
- 旧 GBK 段（约 8-22 边界之前）在日志页会显示乱码。一次性转码建议：
  `iconv -f GBK -t UTF-8 data/daily.log -o data/daily.log.new && mv data/daily.log.new data/daily.log`
  （evolve.log 同；转码前备份；dash 服务正在读日志文件，挑无人查看时执行）。
- 未在本任务内执行转码：涉及既有历史数据文件，超出本任务文件边界。

---

## 4. 测试与 lint

### 新增 `tests/test_ensure_dashboard.py`（10 用例，mock 探测/拉起/等待函数）
- 决策分支：alive 跳过（不拉起）/ occupied 失败（不拉起）/ down 拉起成功 exit 0 /
  down 拉起但 30s 未 up exit 1 / down 且 venv 缺失 exit 1（拉起前拦截）。
- `probe()` 三态：listening+http200→alive；listening+http 非 200→occupied；未监听→down。
- `launch()` 命令契约：`[venv python, -E, -X, utf8, web_server.py]`、env 无 PYTHONPATH、
  PYTHONIOENCODING=utf-8、cwd=项目根、DETACHED/NO_WINDOW/BREAKAWAY 旗标在位、
  stdout/stderr 进独立服务日志。
- 全部用例 mock 掉 `_log_line`，不写真实 data/ 日志。

### 执行结果
- `pytest -q tests/test_ensure_dashboard.py tests/test_web_server.py tests/test_web_api_internal.py tests/test_web_api_public.py tests/test_web_alert_banner.py tests/test_run_silent_rotation.py`：**全绿**（exit 0）。
- `uv run ruff check scripts/ensure_dashboard.py tests/test_ensure_dashboard.py src/predictor/web/server.py`：**All checks passed**。

---

## 5. 环境发现（供运维知悉，非本任务缺陷）

- 本机环回 8789/8798/8799 存在 **Hermes node 运行时占位**（PID 19508 等，自 20:03）：TCP
  connect 可成功但无 HTTP 应答，`Get-NetTCPConnection`/netstat 仅 8789 可见、8798/8799 不可见。
  隔离演练选 8799 时首探一度显示 occupied，即源于此；**真实端口 8765 探测不受影响**（实测
  alive 准确）。提示：今后在本机做端口演练，避用 8790±/8789 一带，且 `ensure_dashboard`
  若遇到「8765 被占但非 dashboard」会按设计 exit 1 暴露（LastTaskResult 可见），不会误拉误杀。
- PowerShell 5.1 踩坑记录（已绕开）：
  1) 无 BOM 的 UTF-8 `.ps1` 被按 GBK 解析，中文注释行尾多字节序列会吞掉换行把下一行代码
     吃进注释（表现为 `$settings` 神秘为 Null / 反引号续行断裂）——建任务脚本最终改纯 ASCII；
  2) 5.1 的 `New-ScheduledTaskSettingsSet` **没有** `-DisallowStartIfOnBatteries /
     -StopIfGoingOnBatteries / -AllowHardTerminate` 参数（反义开关默认值恰等于 Resolve 配置）；
  3) `MSFT_TaskDailyTrigger.Repetition` 初始为 $null，需从带 repetition 的 Once trigger 移植对象；
     且 `Repetition.StopAtDurationEnd` 移植后默认 True，已显式改 False（否则 P365D 到期后停摆）。
- WSL（prime-agent 开发环境）侧无 8765/8798/8799 相关监听，排除 WSL 转发干扰。

---

## 6. 边界遵守与交付清单

- 新建：`scripts/ensure_dashboard.py`、`tests/test_ensure_dashboard.py`、本报告。
- 修改：`src/predictor/web/server.py`（仅 :234 一处编码行）。
- 运行时新增（由脚本/任务自动产生）：`data/dashboard-ensure.log`（已产生）、
  `data/web_server.log`（首次真实拉起时产生）。
- 未 commit、未 push；未碰 .env、shell/pi、.foresight、data/foresight.db、storage.py 等
  并行任务占用文件；STATUS.md 与技能卡同步建议由协调方统一执行。
