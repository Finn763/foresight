# Foresight 运维硬化修复报告（2026-08-27）

对应 CC 评审报告 §3.5（运维自动化：备份/告警消费）+ §3.6（日志治理），P2 三项。
全部实测通过：49 个相关 pytest 全绿（29 新增 + 20 回归）、ruff 干净、计划任务实测触发
LastTaskResult=0、备份文件与源库 SHA-256 一致。

## 变更文件清单（文件边界内）

| 文件 | 动作 | 内容 |
|---|---|---|
| `scripts/backup_db.py` | 新建 | 文件级 DuckDB 备份 + 7 天清理 |
| `scripts/run_silent.py` | 改 | 日志句柄 1MiB 轮转（.1 后缀重开） |
| `scripts/health_check.py` | 改 | 告警落盘统一入口：同日同类合并去重 + 30 天清理 |
| `src/predictor/web/server.py` | 改 | `/api/ops/alerts`、`/api/ops/alerts/ack`、首页告警横幅注入 |
| `tests/test_backup_db.py` | 新建 | 备份脚本单测（7 例） |
| `tests/test_alert_dedup.py` | 新建 | 告警去重/清理单测（10 例） |
| `tests/test_web_alert_banner.py` | 新建 | dashboard 横幅 API/注入/ack 单测（8 例） |
| `tests/test_run_silent_rotation.py` | 新建 | 日志轮转单测（4 例） |
| `docs/hermes-fix-report-ops-hardening-2026-08-27.md` | 新建 | 本报告 |

未碰任何边界外文件；未 commit；未碰 .env / shell/pi/ / .foresight/。

---

## ① 自动 DB 备份（§3.5）

### 脚本设计
- `scripts/backup_db.py`：**不连接 DuckDB**，`shutil.copy2` 直接拷贝文件
  `data/foresight.db → data/backup/foresight-YYYYMMDD-HHMM.db`。DuckDB 在 Windows
  上跨进程排他访问（连 read_only 连接都 IOException），文件拷贝是唯一不受持锁轮次
  影响的方式。
- 保留窗口 7 天：备份后按**文件名日期**清理（copy2 保留源 mtime，mtime 是源库最后
  写入时刻，不能作清理依据）。
- 同秒重名/DB 缺失/拷贝被拒 → 显式失败 exit 1（schtasks 记失败，不留假成功）。
- CLI：`--db / --backup-dir / --keep-days / --now`（测试注入用）。

### 计划任务 Foresight-Backup（配置照抄 Foresight-Resolve，Get-ScheduledTask 实读比对）

| 字段 | Foresight-Resolve（源） | Foresight-Backup（新建，读回实测） |
|---|---|---|
| Trigger | Daily 16:30, DaysInterval=1 | **Daily 02:30（避开全部轮次）**, DaysInterval=1 |
| Action.Execute | base GUI pythonw（uv 的 cp313-x64 目录） | 同 |
| Action.Arguments | `"...\run_silent.py" "scripts\evolve.py" "data\evolve.log" resolve` | `"...\run_silent.py" "scripts\backup_db.py" "data\backup.log"` |
| WorkingDirectory | D:\code\Foresight | D:\code\Foresight |
| Principal | SYSTEM / ServiceAccount / Limited / Default | 同（读回全同） |
| Settings | IgnoreNew / Win8 / PT2H / StartWhenAvailable=True；IdleSettings PT10M/PT1H/StopOnIdleEnd=True | 同（读回全同） |

### 实测
- 注册后 `Get-ScheduledTask` 读回逐字段验证（上表右列即读回值），State=Ready。
- `Start-ScheduledTask` 实测触发一次：
  - **LastTaskResult = 0**，LastRunTime 2026-08-27 20:01:55，NextRunTime **2026-08-28 02:30:00**，NumberOfMissedRuns=0。
  - 产物 `data/backup/foresight-20260827-2001.db`：7,090,176 字节，与源 `data/foresight.db` **SHA-256 完全一致**（备份时点无生产轮持锁，20:01 远离 09:00-16:30 轮次窗口）。
  - `data/backup.log` 落盘正常（run_silent 横幅 + `BACKUP: ...` 行）。

## ② 告警消费（§3.5）

**落点选择**：health_check.py（告警落盘处）。理由：①任务文本预授权「并行任务不占用
它可改」；②去重必须在写入点做才闭环（独立治理脚本需再挂一个调度钩子，侵入更大）。
四个写点（等锁超时/锁竞争/撞库异常/常规告警）全部收敛到 `write_alert_file()` 统一入口。

### 同日同类合并去重
- 签名 = 类别（error 类取首行标题；普通告警取「## 告警」小节要点）+ 数字归一化
  （「LLM 揭晓失败 2 次」与「…5 次」视为同类，计数升级时合并刷新内容而非新建文件）。
- 同一天同签名且未确认的告警 → 内容写回既有文件（保留原文件名、检出时间刷新），
  不再新建；不同类别同日共存；跨日不合并。
- **确认机制**：ack 后的文件改名 `*.ack.md`，不参与合并——确认后同类复发按新告警
  落盘、横幅重现；同秒不同类别落盘自动 `-N` 消歧（实测发现并修复的覆盖隐患）。

### 30 天清理
- `cleanup_stale_alerts()`：文件名日期判定（解析失败按 mtime 兜底），每次巡检开始
  （零告警日也执行）+ 每次告警落盘时各跑一次；目录不存在是 no-op。
- 已确认（.ack.md）的过期文件同样清理。

### dashboard 顶部横幅（最小侵入）
- `src/predictor/web/server.py` 新增 internal 端点：`GET /api/ops/alerts`（最新未确认
  告警，latest=null 表示无）+ `POST /api/ops/alerts/ack`（改名 .ack.md + 303 回首页）。
- `GET /` 服务端读 `data/alerts/` 最新未确认告警（跳 .ack.md），把横幅 HTML 注入
  `<main id="app">` 之前；仅 internal 模式注入（public 战绩榜零泄漏）；告警目录缺失/
  读取失败 → 无横幅，页面不受影响。
- **CSP 合规**：index.html 的 `script-src 'self'` 禁 inline JS——确认按钮用原生
  `<form method="post">` 提交 ack（303 回首页），零脚本；横幅样式用 inline style
  （`style-src 'unsafe-inline'` 允许），配色沿用深色主题 tokens（err #ff6b6b 等）。
- 告警内容经 `html.escape` 注入（单测覆盖 XSS 转义）；静态三件套（index.html/app.js/
  style.css）零改动，前端选择器契约不受影响。

## ③ 日志轮转（§3.6）

- `scripts/run_silent.py` 新增 `rotate_log_if_needed()`：打开日志前检查，**≥1MiB 时
  原文件改名 `.1`（丢弃旧 .1）再按原 append/utf-8/errors=replace 语义重开**；轮转
  失败（他进程占用）静默继续 append，不阻断任务；未超限零改动。
- 轮转在 stdout 重定向前执行（pythonw 下此刻 print 指向无效句柄，不输出）。

## 测试与质量

- **新增 29 例全绿**：备份拷贝/清理/边界日/CLI 端到端（含缺库 exit 1）；去重
  （同类合并/计数升级刷新/异类共存/跨日/ack 复发/error 类别/清理/子进程端到端两次
  巡检只落一个文件）；横幅（注入/转义/ack/已确认排除/最新选择/error 兜底/public 404
  与零横幅）；轮转（限内不动/超限 .1/二次轮转覆盖/缺失 no-op）。
- **回归 20 例全绿**：`tests/test_health_check.py`(8) + `test_web_server.py`(5) +
  `test_web_api_internal.py`(7)，health_check 原语义（clean 静默/告警 rc1/等锁兜底）
  与 web 骨架（index 200、public 404）零回归。
- **ruff 干净**：8 个改动文件 `ruff check` All checks passed（line-length=100）。
- 运行方式遵循环境铁律：`env -u PYTHONPATH uv run pytest ...` / `... ruff check ...`；
  子进程测试用 `.venv` python + `-E -X utf8`。

## 已知说明（不影响验收）

1. `Register-ScheduledTask` 在本机 XML schema 拒绝 `-ProcessTokenSidType Unrestricted`
   参数，注册时省略（默认值）；`Get-ScheduledTask` 读回 Foresight-Backup 与
   Foresight-Resolve 的 Principal 字段**完全一致**（SYSTEM/ServiceAccount/Limited/
   Default），对 ServiceAccount 任务无行为差异。
2. `data/backup/` 中历史文件 `foresight-20260820-pre-dedup-clean.db` 文件名带
   `foresight-YYYYMMDD-` 前缀，会被 prune 按 08-20 日期判定——现处 7 天边界保留；
   今后放入该目录的自定义文件若前缀日期超 7 天将被清理（备份目录内按备份规则治理，
   属预期）。
3. dashboard 横幅在页面加载时注入（服务端渲染）；若页面保持打开期间新告警落盘，
   需刷新可见——与既有 #error-banner 的 3s 重试语义互不干扰。
