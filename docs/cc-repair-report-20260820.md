# Foresight 修复报告 2026-08-20

> 范围：P1 重复题去重失效 / P2 揭晓管线（#69、#9、健康巡检）/ P3 signsrv 服务死亡。
> 纪律：不 commit；未写任何 resolutions.csv/outcome（#69 揭晓留人工）；DB 动数据前已备份。
> 全量回归：327 用例通过（见文末测试汇总）。

## P1 重复题去重失效（真 bug，已修）

**根因**：agent 建题入口 `predictor/cli.py predict_once` 此前只按 `title = ?` 精确判重
（2026-08-12 版本）。#97/#98（道琼斯 8-19 vs 8-18，仅判定口径措辞略异）、#93/#94
（伦敦金 8-21 vs 8-14，日期格式/措辞略异）标题不同 → 双双入库。
实测纯字符串相似度不可行：#93/#94 相似度仅 0.53，而「未来7天黄金突破5150 vs 未来30天
突破4600」这类真·不同题相似度高达 0.82（模板同构），阈值取哪都误伤一侧。

**改动**：
- 新增 `src/predictor/selection/dedup.py`：`event_signature(title)` 抽「标的别名 × 方向
  关键词 × 绝对日期集合（M-D，NFKC 归一）」，三者齐备且相等才判同题；缺任一（如无日期
  模板题）→ 不参与判重（宁漏判不误判）。`find_duplicate_question(st, title)` 先精确标题
  后签名匹配。
- `src/predictor/cli.py`：`predict_once` 接入签名判重；判重模块异常时兜底退回精确标题
  SQL（不阻塞建题）；新建题补记 `question_added` 审计事件（与 daily/evolve 同格式）。
- 存量重复题清理（用户批准）：**备份** `data/backup/foresight-20260820-pre-dedup-clean.db`
  后删除 #94/#98 及各自预测（websearch 臂各 1 条）与证据文档（28/24 条），并记
  `question_deleted` 审计事件；#93/#97 保留。

**测试**：`tests/selection/test_dedup.py` 新增 7 例（精确判重、#97/#98 与 #93/#94 实题
近似判重、不同日/不同标的/无日期模板不误判、签名字段断言）+ `test_predict_cli.py` 全过。

**遗留风险**：①「道琼斯 8-19 收盘 vs 开盘高于 8-18」类同标的同日期不同价格口径会被
判同题——错误方向是复用（保守），可接受；②裸「19日」无月份不采，纯裸日标题漏判；
③年份被丢弃，跨年同日题理论误判（不可能同时未揭晓）。

## P2 揭晓管线

### #69（C 类人工题，closes 8-13）——调查结论：降级链路无 bug，实为人工待办

时间线（evolution_log 实查）：8-14 16:30「waiting data window」→ 8-15「resolver None
（hf_GC gt_prev_close 无昨收双源验证，families.py 已知限制）」→ 8-16「data window passed」
→ **8-17 16:30 `resolution_timeout`（grace expired >3d）+ `resolution_archived`，spec.class
已置 C**。宽限降级逻辑（evolve.py resolve_round ①分支）按期触发，未失效；8-17 起该题
已进待人工清单（resolutions.template.csv），等待外部人工填 outcome——本报告未写任何
揭晓数据（用户决策③）。

### #9（B 类 EIA，closes 8-19）——宁缺毋滥属预期；题面时点设计有误，已修

8-19 16:33 `llm_resolve_failed low_confidence 0.85/0.60<0.7` + `resolution_failed` 是
LLMResolver 护栏的正常拒判（宁缺毋滥），**非 bug**；宽限 3 天 → 8-22 16:30 轮会按
resolve_round ①分支正确降级 C 人工（路径与 #69 相同，已验证幂等单次）。
真正的缺陷是题面时点：`scheduler.py` EIA 模板 closes=week+2d 落**周三 00:00**，早于
EIA 发布（周三 10:30 ET = 北京 22:30）约 22.5 小时——闭题时事件尚未发生。已修为
`week + timedelta(days=3)`（周四 00:00，发布后）。

### P2 代码改动（4 项）

| 文件 | 改动 |
|---|---|
| `resolution/spec.py` | C 类 spec 校验放行（裸 `{"class": "C"}` 合法；降级产物保留原字段本就合法） |
| `resolution/auto_resolve.py` | 文案修正：「data window passed（错日重试被拒，等待宽限降级）」→「数据窗口已过，停止重试；超宽限后由 resolve_round 降级人工」（原文案误导排障） |
| `scheduler.py` | EIA 模板 closes 周二→周四（如上） |
| `scripts/evolve.py` | 宽限降级目标类按 `spec.degrade_to`（缺省回退 C）而非硬编码 C；日志带目标类 |
| `data/storage.py` + `ops/health.py` | `ops_backlog` 积压口径 A→**A/B**（B 类超宽限同样依赖 16:30 降级，#9 类挂起不再漏报）；健康告警文案同步 |

### P2b 健康巡检撞库（真 bug，已修）

**根因**（实锤）：`data/health.log` 显示 9:35 巡检崩溃于
`duckdb.IOException: 另一个程序正在使用此文件（File is already open in ...python.exe PID 25840）`。
DuckDB 在 Windows 跨进程独占文件锁：09:00/09:05 预测轮持写连接期间，**连只读连接也
IOException**（实测复现）。health_check 在 Storage 打开/建 schema 阶段裸崩 → exit 1 且
无 alert 文件（schtasks 只记了 LastTaskResult=1）。

**改动**（`scripts/health_check.py`）：① `_open_storage_with_retry` 打开 DB 撞锁重试
6 次×10s；② 最终失败时写 `data/alerts/alert-*-health-error.md`（含异常信息）并
traceback 落 health.log，exit 1——任何失败都有可见产物，不再静默无痕。

**测试**：`tests/test_health_check.py` 5 例全过（干净静默/告警落盘/认证特征/风暴阈值/
轮次缺席）。

## P3 foresight-signsrv 服务死亡（已复活 + 调度改造）

**根因**：schtasks 任务 foresight-signsrv 是 **LogonTrigger**（只在登录时触发），机器
久不重启即不再拉起；8-14 21:37 被 Ctrl+C 终止（LastTaskResult=3221225786=0xC000013A），
停摆至今日。服务本身（MediaCrawlerPro-SignSrv，微博/小红书等签名，端口 8989，
`/signsrv/pong` 健康口）无状态无副作用，重启安全。

**改动**：
- 复活服务：DETACHED 拉起 `data/mediacrawler-pro-signsrv/app.py`（自建 .venv，Py3.12），
  日志追加 signsrv.log。验证：端口 8989 监听（pid 4420）、`/signsrv/pong` 返回
  `{"biz_code":0,...,"data":{"message":"pong"}}`。
- 新增 `scripts/ensure_signsrv.py`（纯标准库）：端口已监听→exit 0（幂等）；未监听→
  DETACHED 拉起。实测幂等路径 exit 0。
- schtasks 改造（用户选项1：每小时幂等自检）：动作改为
  `python.exe ensure_signsrv.py`；触发改为每日 00:05 起**每 1 小时重复**（重复时长
  8760h）。`schtasks /run` 实测 LastTaskResult=0，下次运行 16:05。

**遗留风险**：① 若 8989 被无关程序占用，ensure 会误判「已存活」而不拉起 signsrv
（端口冲突场景）；② 触发在 Interactive 登录态（本机常驻登录，可接受）。

## 测试汇总

- 受影响模块 57 例：`test_dedup(7新) / test_evolve / test_spec / test_auto_resolve /
  test_facts / test_health / test_scheduler / test_predict_cli / test_health_check` 全过。
- 全量回归 `env -u PYTHONPATH uv run pytest`：327 例通过。
- `uv run ruff check .`：零告警（含全部改动文件）。

## 对抗审查（收尾自审）

1. 去重保守方向验证：全库 60 未揭晓题实测，签名判重对已知近似对命中、对
   「不同日道琼斯/不同标的/无日期黄金模板」均不误判（阈值无关设计，规避相似度坑）。
2. `ops_backlog` 扩 B 后 Polymarket 题（spec 无 class 键）自动跳过，不误计入积压。
3. `resolve_round` 降级幂等（class=C 后不再重复记 timeout）——既有测试覆盖，未回归。
4. health_check 崩溃路径不依赖 DB 连接，锁死也能落告警文件。
5. #94/#98 删除前已 read-only 验证行数与备份，删除后计数归零；brier/model_stats
   无残留（两题均未揭晓、brier NULL）。
6. 未触碰 .env/凭据、shell/pi/、.foresight/SYSTEM.md；未 commit/push。

## 遗留待办（人工）

- ~~#69（伦敦金 8-13）~~ ✅ **已于 8-20 15:31 人工揭晓**：双源交叉（新浪 K 线 GC 8-13 收
  4406.7 / 8-14 收 4431.9；每经·新浪财经 8-14 收 4432.00；金投网昨收 4407.1）→
  8-14 > 8-13 → outcome=True，在 7 天计分截止（18:46）前入账。
- #9（EIA，8-22 降级后）需人工查官方数据填 `data/resolutions.csv` 后跑 `scripts/resolve.py`。
- #93/#97（保留的近似题，class=None）同属人工清单。
- 可选：signsrv 端口冲突场景的进程名校验（P1 级，当前未实现）。
