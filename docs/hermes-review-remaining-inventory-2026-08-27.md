# CC 评审 40+ 项发现 — 剩余未做盘点（2026-08-27 深夜复核）

> 性质：纯盘点，未改任何代码、未 commit、未碰 .env/shell/pi/.foresight。
> 方法：① 通读 `docs/cc-improvement-analysis-2026-08-27.md` 全部 41 项；② git log 8-27 共 11 commit 对应今日 4 修复批；③ 逐项 grep/icacls/netstat/Get-ScheduledTask/DuckDB 实测核实——**只信代码证据，不信 STATUS 自述**。
> 复核日期：2026-08-27 深夜（四批修复全部 commit 之后）。

## 0. 证据基线（本次实测）

| 证据 | 实测值 |
|---|---|
| 今日 commit | 11 个（0dc30ed P0 批 → 06cf0f4 STATUS 同步；4 修复批 + autopick 2 个 + docs 同步 3 个） |
| 全量 pytest | **469 用例全绿**（exit 0；flaky 名单用例本次通过） |
| ruff format --check | **20 文件未格式化**（评审时 5 个 → 债扩大了） |
| ruff check | 通过（仍只有 E/F/I/UP 规则） |
| schtasks | **12 个任务**（新增 AutoPick/AutoPickIngest/Backup/Dashboard/PMFetch/PMResolve） |
| schtasks principal | 仍分裂：Administrator ×6 + **SYSTEM ×6**（新增 4 任务按 SYSTEM 建） |
| 生产 DB 证据表 | `uq_source_documents_qid_url` 唯一索引**已建**，重复组 **55→0**，总行 2345→2290（§2.7① 已在生产库生效） |
| .env ACL | 未动：Authenticated Users:(M) + BUILTIN\Users:(RX) 仍在 |
| 8989 端口 | 仍 **0.0.0.0** LISTENING（PID 34440，§6.2 未动） |
| STATUS.md | 多处过期（见 §4 诚实标注） |

## 1. 全部 41 项状态总表

状态图例：✅ 已落地（代码证据）｜◐ 部分落地｜❌ 未做｜⚖ 已按用户决策处置

| § | 标题 | 优先级 | 状态 | 关键证据 |
|---|---|---|---|---|
| 1.1 | 引擎反向依赖 scripts 私有函数 | P0 | ✅ | `ops/lock.py`+`ops/manual.py` 存在，src/ 零 `import scripts` |
| 1.2 | 扩展桥「第三通道」无契约 | P1 | ◐ | TS 侧 BRIDGE_CONTRACT 22 通道+白名单+`tests/test_foresight_tools_contract.py` 已建；但 9 个读工具仍 `inline_json` 直调 storage 内部（未按建议改 CLI 只读子命令） |
| 1.3 | 双 git 仓库重叠治理 | P1 | ❌ | BRANDING.md 无「外层快照」步骤；fork 仍 no_push 零备份 |
| 1.4 | 调度 Principal 分裂 | P2 | ❌ | STATUS 无 principal 说明；实测仍 6+6 分裂 |
| 1.5 | 三套 sys.path 装载策略 | P2 | ◐ | run_silent.py 已提交（site-packages 注入固化）；但 TS:138 仍 `sys.path.insert(cwd/src)`、16 scripts 仍根注入，三策略未收敛 |
| 1.6 | storage.py god class 拆分 | P2 | ❌ | 仍单 Storage 类（报告本标注择期） |
| 2.1 | 回测假真值 | P1 | ◐ | ① None 显式跳过+require_outcome+NaN→null ✅；② ForecastBench 官方 bucket 6 TODO 未动（外部阻塞）；③ 「三臂不含生产 arm」文档标注未写 |
| 2.2 | 统计基线错映射 | P1 | ✅ | cpi_mom 双支中国限定+窗口解析器+ffr 三向+breakout；`tests/stats/test_baselines.py` 已建 |
| 2.3 | Brier 统计口径失真 | P2 | ◐ | ① 题族分桶（`brier_by_family`）✅ ② brier_ema 归属修正 ✅ ③ `get_model_name()` 配置化 ✅；**④ scoreboard ΔBrier vs 0.5 常数 ❌（全仓无 delta 字段）** |
| 2.4 | 校准器 16:30 路径不刷新 | P2 | ✅ | `auto_resolve.py:123` refresh_from_storage + 失败降级日志 |
| 2.5 | 保序回归 tie 加权 | P2 | ✅ | 按块 n 加权 `sum(y·n)/sum(n)`；`test_isotonic.py:36` 复现例 0.25 |
| 2.6 | websearch_predictor 偏差面 | P2 | ✅ | n_samples=3、rationale 取最近均值采样、题面 XML 隔离 |
| 2.7 | 数据管线三缺口 | P2 | ✅ | ① UNIQUE 索引+INSERT OR IGNORE（**生产库已迁移**）② dedup 宏观题族（fed/CPI/EIA/非农/PMI）③ auto_resolve 吞异常补日志 |
| 3.1 | 9:35 巡检撞库 | P0 | ✅ | health_check `wait_acquire` 排队等锁（45min 上限） |
| 3.2 | pm_resolve 未入 schtasks | P1 | ✅ | Foresight-PMResolve 21:00 + Foresight-PMFetch 20:00 两任务实测存在 |
| 3.3 | 调度五处设置不一致 | P2 | ❌ | principal 仍分裂；StartWhenAvailable/电池/ExecutionTimeLimit 未见统一动作 |
| 3.4 | dashboard 无守护+日志编码 | P2 | ◐ | ensure_dashboard.py+任务+server.py utf-8 ✅；**旧 GBK 日志段一次性转码残留人工** |
| 3.5 | 无 DB 备份+告警无消费 | P2 | ✅ | backup_db.py+Foresight-Backup 02:30；告警 30 天清理/去重/横幅（server.py+health_check） |
| 3.6 | 日志无轮转 | P2 | ✅ | run_silent 1MiB 轮转 `.1` 后缀 |
| 4.1 | 序列重复拉取 | P1 | ✅ | `historical.py` 模块级缓存+ThreadPoolExecutor |
| 4.2 | LLM 采样串行+超时倒挂 | P1 | ◐ | gather 并发+AsyncClient 复用+900s 超时+心跳 ✅；④「65536 总预算护栏」可选建议未做 |
| 4.3 | 7 天更新规则漂移 | P1 | ◐ | 改 `timedelta(hours=7*24)`+共享 `_within_update_window` ✅；但 STATUS 自承**数学等价**——双轨漂移根因（28 题/天）未解决，留待合并单入口决策 |
| 4.4 | web 查询两隐患 | P2 | ❌ | server.py:112 全局 Exception 仍全伪装 503「database busy」；MAX(p2.id) 相关子查询仍在（:785/:828/:973/:999） |
| 4.5 | 无批量写+非事务 | P2 | ❌ | 无 executemany / BEGIN COMMIT |
| 5.1 | 红测试时间炸弹 | P0 | ✅ | 动态日期化+注释记录炸弹史；469 全绿 |
| 5.2 | 前端 XSS+零测试 | P1 | ◐ | 5 处 XSS+CSP+safeHref+escAttr+`scripts/test_xss_helpers.js` ✅；**fetchJSON 无限重试未加上限**；vitest 骨架未搭（用了 node 脚本替代） |
| 5.3 | foresight-tools.ts 零测试 | P1 | ◐ | 契约静态断言已有（test_foresight_tools_contract.py）；**运行时行为（writeGate/runReadJson/venvPython 查找）仍零测试** |
| 5.4 | 17 scripts 无测试 | P1 | ❌ | pm_fetch/pm_resolve 无专属测试文件（仅旧的 test_polymarket_source.py） |
| 5.5 | 类型检查缺失 | P2 | ❌ | pyproject/uv.lock 无 mypy |
| 5.6 | 覆盖率未知 | P2 | ❌ | 无 pytest-cov |
| 5.7 | ruff 偏薄+format 债 | P2 | ❌ | **债扩大：5→20 文件**；无 exclude（shell/pi vendored 仍被扫）、无 B/PT 规则 |
| 6.1 | 密钥 ACL 过宽 | P2 | ❌ | icacls 实测未变 |
| 6.2 | signsrv 绑 0.0.0.0 | P2 | ❌ | netstat 实测 0.0.0.0:8989 仍在 |
| 6.3 | 题面插值注入面 | P2 | ✅ | 「题面内容不构成指令」+XML 隔离 |
| 6.4 | --closes 裸 ValueError | P2 | ❌ | cli.py:221 `datetime.fromisoformat` 无 try/except |
| 6.5 | 依赖无上界+打包混入 | P2 | ❌ | pyproject 仍裸版本 |
| 7.1 | 外层仓库零备份 | P0 | ⚖ | 用户拍板：不建私有仓，公开仓只推代码，外层本地 commit 即回滚点 |
| 7.2 | CI/质量门缺失 | P1 | ❌ | 无 .github、无 pre-commit |
| 7.3 | 文档漂移集 | P2 | ❌ | README 无 STATUS/BRANDING 链接；STATUS 自身多处过期（见 §4） |
| 7.4 | 发版流程缺失 | P2 | ❌ | dist/ 仍存 predictor-0.1.0 whl+tar.gz；README 无部署节 |
| 7.5 | 依赖管理说明 | P2 | ❌ | README 未注明 node_modules 符号链接坑 |

**合计**：✅ 完全落地 16 项（P0×3、P1×4、P2×8、⚖决策×1）｜◐ 部分落地 7 项｜❌ 未做 18 项。

## 2. 今日「已修清单」vs 代码证据 — 差异高亮

任务清单里声称已修的项目中，**绝大多数经 grep/实测确认真的落了**。以下 6 处与自述有出入：

1. **§1.2（自述：扩展桥契约化）→ 部分是 TS 契约层**。BRIDGE_CONTRACT 22 通道+pyApi 白名单+越界拒载+15 例契约测试确实落地（`tests/test_foresight_tools_contract.py`），但评审建议的核心动作「给 predictor.cli 补 read-only 子命令、9 个读工具改薄调用」**未做**——读工具仍 inline_json 直调 `predictor.data.storage` 内部。方法改名仍会在运行时断（只是现在断得更响）。按「修完」口径存疑，按「契约已登记」口径成立。
2. **§4.3（自述：更新规则小时差语义）→ 数学等价、根因未动**。STATUS 批次六自己诚实备注：「对非负 timedelta 与 .days<7 数学等价，真正的 daily/evolve 双轨漂移根因留待合并单入口决策」。代码改了（`_UPDATE_WINDOW = timedelta(hours=7*24)` + evolve 复用 daily 的 `_within_update_window`），但 28 题/天的生产症状未消除。**这条只能算半个**。
3. **§2.3（自述：Brier 题族分桶+brier_ema 归属修正）→ ④ 漏了**。①②③ 全落地（brier_by_family / canonical_model_name / get_model_name），但建议④「scoreboard 增加与 0.5 常数的 ΔBrier」全仓无任何 delta 实现——批次四只做了 3/4。
4. **§5.2（自述：前端 5 处 XSS+CSP）→ XSS 全修，但 fetchJSON 无限重试残留**。`app.js:13-28` 仍 `return fetchJSON(path)` 无次数上限、`state.retries` 仍死字段（只写 0 不递增）。vitest 骨架也未搭（用了 `scripts/test_xss_helpers.js` 38 断言的 node 脚本替代——可接受但非建议形态）。
5. **§2.1（自述：回测假真值止血）→ 止血 ✅，但③文档标注未写**。compare_backtest.py 无「三方对比不覆盖生产 arm」的任何说明文字；②外部真值源 6 TODO 原样未动（ForecastBench 阻塞）。
6. **§4.2 ④「65536 总预算护栏」**为可选建议，未做（前 ①②③ 全落地）。
7. **§3.4 残余**：旧 GBK 日志段一次性 iconv 转码仍待人工（dashboard-guard 报告 §3 原话，非代码任务）。
8. **§5.3 与 §1.2 重叠部分**：契约测试文件同时覆盖了两节，但 §5.3 要求的运行时冒烟测试（writeGate 拒绝、runReadJson 解析、venvPython 路径查找）不在契约测试范围内——§5.3 只能算从「完全空白」前进到「有静态契约断言」。

## 3. 剩余未做清单（三组）

### A. 值得再做一轮（共 12 项，合计约 10-12h，全部可并行——不同文件/系统面互不冲突）

| § | 标题 | 优先级 | 工作量 | 一句话内容 | 可并行 |
|---|---|---|---|---|---|
| 5.7 | ruff format 债+exclude | P2 | 1-2h | format 债从 5 涨到 20 文件（无门禁正在回归）；pyproject 加 shell/pi exclude；试跑 B+PT 规则 | ✅ 独立 |
| 3.3 | 调度五处设置不一致 | P2 | 1-2h | 重启/电池场景半套系统静默停摆；新增 4 任务又按 SYSTEM 建使分裂扩大——统一 principal/StartWhenAvailable/ExecutionTimeLimit（方向与 §1.4 一起定） | ⚠️ 需 PowerShell 操作 schtasks，单独做+验证窗口 |
| 4.4 | web 异常分类 | P2 | 1h | 全局 Exception 处理器把真 bug 伪装成 503「database busy」→ 分类返回+记日志；子查询改窗口函数留择期 | ✅ |
| 4.5 | 批量写+事务 | P2 | 1h | 证据写入 executemany；resolve_question 包 BEGIN/COMMIT | ✅ |
| 6.1 | .env ACL 收紧 | P2 | 0.5-1h | icacls 去掉 Authenticated Users:(M)/Users:(RX)，仅 Administrators+SYSTEM（+auth.json 移除 CodexSandboxUsers） | ✅ |
| 6.2 | signsrv 改绑 127.0.0.1 | P2 | 0.5-1h | 实测仍 0.0.0.0:8989；改绑或加防火墙阻止规则（pong 自检不受影响） | ✅ |
| 6.4 | --closes 裸 ValueError polish | P2 | 0.5h | cli.py fromisoformat 包 try/except 给可读报错 | ✅ |
| 2.3④ | scoreboard ΔBrier | P2 | 0.5h | 增加与 0.5 常数基线的 ΔBrier 展示（批次四漏掉的一个子项） | ✅ |
| 5.2 残余 | fetchJSON 重试上限 | P2 | 0.5h | app.js 无限 3 秒循环加次数上限；顺手删死字段 state.retries | ✅ |
| 2.1③ | 回测文档标注 | P2 | 0.1h | compare_backtest 注明三臂不含生产 websearch arm | ✅ |
| 1.5 收尾 | sys.path 收敛 | P2 | 1-2h | run_silent 已提交；收敛 TS 侧 cwd/src 注入与 16 scripts 根注入为单一策略 | ✅ |
| 7.3 | 文档漂移清理 | P2 | 1h | README 文档地图补 STATUS/BRANDING/pi-化/forecastbench 4 篇+目录结构；STATUS 数字与清单修正（见 §4） | ✅ |

### B. 择期（共 12 项，低紧迫或大工作量）

| § | 标题 | 优先级 | 工作量 | 一句话内容 |
|---|---|---|---|---|
| 1.2 残余 | 读工具改 CLI 只读子命令 | P1 | 3-4h | 若认为 TS 契约层不够：predictor.cli 补 read-only 子命令单行 JSON，9 个读工具改薄调用 |
| 1.3 | 双 git 仓库重叠治理 | P1 | 2h | BRANDING.md 补「内层 rebase 后外层快照提交」；fork 加私有镜像远端；unshallow 说明 |
| 5.3 | TS 运行时测试 | P1 | 4-6h | writeGate 拒绝/runReadJson 解析/venvPython 路径查找冒烟（mock pi.exec） |
| 5.4 | pm 管线测试 | P1 | 4-8h | pm_resolve 决议决策逻辑+pm_fetch 过滤/去重 10-15 用例（Polymarket 生产链路仍零自动化测试） |
| 7.2 | CI/pre-commit | P1 | 2h | 本地 pre-commit（ruff check+format）+远端 CI——红测试漂红一周、format 回归都是无门禁的直接后果 |
| 4.2④ | 65536 总预算护栏 | P2 | 0.5h | max_output_tokens 跳变加总预算上限（可选建议） |
| 5.5 | mypy 渐进模式 | P2 | 2-4h | --follow-imports=silent 只查 src/predictor；storage.py 基础好成本低 |
| 5.6 | coverage 基线 | P2 | 0.5h | 装 pytest-cov 首跑拿基线数字 |
| 1.6 | storage ReadModel 拆分 | P2 | 4h | 只读视图独立类（报告原标注择期） |
| 6.5 | 依赖上界+打包清单 | P2 | 0.5-1h | duckdb/pydantic 加 `>=x,<y`；清理 dist 混入的评审记录 |
| 7.4 | 发版流程 | P2 | 0.5h | 删 dist 死产物；README 补「部署/升级/schtasks 管理」节 |
| 7.5 | README 依赖说明 | P2 | 0.5h | 注明根 node_modules 是 npm link 符号链接、勿在根跑 npm |

### C. 需用户决策 / 外部阻塞（共 4 项）

| § | 事项 | 需要什么 |
|---|---|---|
| 4.3 残余 | daily/evolve 双轨漂移根因——**合并单入口** | 决策：是否把 daily+evolve 合成一个预测入口（消除 28 题/天超设计与逐日漂移）；小时差改动已被自证数学等价，不合并就是没修 |
| 2.1② | ForecastBench 官方真值源 | 外部阻塞：邮件注册拿 bucket → 配 .env（6 TODO 全等此事）；回测目前仍无可靠地面真值 |
| 1.4+3.3 | 调度 principal 统一方向 | 决策：全 Administrator+Run-whether-logged-on（S4U）还是保留 SYSTEM 组并文档化；新任务默认建法需定 |
| 3.4 残余 | 旧 GBK 日志段一次性转码 | 人工一次性 iconv（命令在 dashboard-guard 报告 §3），非代码任务 |

## 4. 诚实标注：STATUS 自述 vs 代码证据

STATUS.md（06cf0f4 版）以下说法与实测不符，应随下一轮修正：

1. **「评审 P1 已清零」（STATUS:48）不成立**。剩余 P1：§1.3（2h）、§5.3 运行时部分（4-6h）、§5.4（4-8h）、§7.2（2h），外加 §1.2/§2.1②/§4.3 的残余。同一条把「校准器刷新时机/保序 tie 加权/evidence 去重/dedup 宏观题族/auto_resolve 吞异常」列进「剩余 P2」——这些在批次六（20a5a91）已全部落地，列表本身也过期。
2. **「全仓 3 个既有文件未格式化」（STATUS:69）→ 实测 20 个**。且无 exclude、无门禁，债在扩大。
3. **「定时任务 9 个」（STATUS:70）→ 实测 12 个**（缺 Backup/Dashboard/PMFetch）。
4. **「347 测试全绿」（STATUS:4）→ 实测 469**（修复批自带 363/381/440 的数字也均已被超越；本次全量跑 exit 0）。
5. **「下一步 1 人工揭晓待办 #69/#9/#93/#97」（STATUS:46）已基本完成**：#93/#97 判 True、#69 改判 False（Brier 0.1936）、#9 复核一致不动——条目未销。
6. **「pm_fetch 拉题暂仍手工」（STATUS:47）已过时**：Foresight-PMFetch 20:00 已建且实测拉入 #109。
7. **「系统运行正常」整体成立**：469 全绿、12 任务在册、生产库迁移生效、Brier 0.1936。剩余 18 项未做全部是 P2 或部分落地的 P1 残余，无新的 P0。

## 5. 验证命令留档（供复现）

- `git log --oneline --since="2026-08-27 00:00"` → 11 commit
- `env -u PYTHONPATH uv run pytest -q` → exit 0（469 collected）
- `env -u PYTHONPATH .venv/Scripts/python.exe -m ruff format --check .` → 20 files
- `Get-ScheduledTask -TaskName 'Foresight*','foresight*'` → 12 任务、6+6 principal
- `icacls .env`、`netstat -ano | grep -E ":8989|:8765"`
- DuckDB read_only：`duckdb_indexes()` 含 `uq_source_documents_qid_url`；`GROUP BY question_id,url HAVING count(*)>1` = 0 组
- 关键 grep：`get_model_name`（storage.py:44）、`_UPDATE_WINDOW = timedelta(hours=7*24)`（daily.py:77）、`refresh_from_storage`（auto_resolve.py:123）、`n_samples: int = 3`（websearch_predictor.py:212）、`PREDICT_TOOL_TIMEOUT_MS = 900_000`（foresight-tools.ts:227）、`_ALERT_RETENTION_DAYS = 30`（health_check.py:30）、`rotate_log_if_needed`（run_silent.py:21）、`brier_by_family`（storage.py:633）、`BRIDGE_CONTRACT`（foresight-tools.ts:274）
