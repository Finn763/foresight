# STATUS — Foresight 预测引擎

> 最后更新：2026-08-20（修复轮：建题近似去重 + 揭晓管线排障 + 健康巡检撞库加固 + signsrv 复活/小时级自检（内部修复报告，未随开源仓库发布））
> 一句话状态：**系统运行正常** —— 327 测试全绿、ruff 零告警、定时任务与 dashboard 均在运转。

## 项目定位

Halawi 式 RAG 管线 + 多模型集成 + 市场先验的 AI 事件预测引擎，Brier 战绩追踪 + B/C 端展示。
当前处于 12 个月规划中的**验证期→产品化过渡**（管线端到端已通、dashboard 已上线）。

**产品形态（2026-08-15 纠偏确认）**：`foresight` = pi-fork 的交互 agent（先 agent 后预测 agent）——
`shell/pi/`（pi.dev v0.84.1 fork，改名 @foresight/foresight-agent，configDir `.foresight`）
+ 人格护栏 `.foresight/SYSTEM.md` + 工具扩展 `.foresight/extensions/foresight-tools.ts`
（predict/leaderboard/resolve 调 Python 引擎）。Python 引擎（src/predictor）是后端，入口
仅 `python -m predictor.cli` / `scripts/predict_cli.py`（单行 JSON 契约）。fork 维护见 `shell/BRANDING.md`。

## 已完成（近期）

- **修复轮（2026-08-20）**：①建题去重失效修复——agent 建题入口仅精确标题判重，新增事件签名近似判重（`selection/dedup.py`，标的×方向×绝对日期），存量重复题 #94/#98 备份后删除（备份 `data/backup/foresight-20260820-pre-dedup-clean.db`）；②揭晓管线——#69 宽限降级链路实为正常（8-17 已降级 C 待人工，非 bug）、#9 低置信拒判属宁缺毋滥（宽限 3 天后自动降级人工），EIA 模板 closes 由周三 00:00 修正为周四（发布后）、降级目标类按 `spec.degrade_to`、C 类 spec 校验放行、积压告警口径 A→A/B；③健康巡检撞库加固——DuckDB 跨进程独占锁下 9:35 巡检裸崩无痕，改重试 6×10s + 异常落 `alert-*-health-error.md`；④signsrv 复活——LogonTrigger 失效停摆 6 天后 DETACHED 重启（端口 8989/pong 验证），新增 `scripts/ensure_signsrv.py` 幂等自检 + schtasks 改每日 00:05 起每小时触发
- **fork 工程化收尾（2026-08-16）**：shell/pi git 化——分支 `foresight-fork`（上游基线 40a3d85 + 3 补丁 commit：rebrand/fsCache/lockfile），origin+upstream 指向 pi（push=no_push 防误推）；Python predictor 全局命令残留确认清零（`which foresight` 唯一指向 npm fork）
- **foresight agent 上线（2026-08-15）**：fork 全局命令 `foresight`（npm link，DeepSeek 凭据入 `~/.foresight/agent/auth.json`）；工具扩展重写（venv python + `-E -X utf8` 修复 PYTHONPATH 污染崩溃与中文乱码；leaderboard src 路径、resolve csv 写入修复）；fork jiti 扩展缓存 bug 修复（`fsCache:false`，编辑扩展后 /reload 生效）
- **对抗测试加固（2026-08-15）**：行为层 13 项（提示注入/硬规则/模糊澄清/工具失败/重复请求/假揭晓）PASS 10；3 项 FAIL 的根因已修——SYSTEM.md 明示「点位预测≠博彩可接」「每条请求独立判定」「工具失败只报错不自行修代码/删数据/出非工具概率」；脚本层 10 项，修 2 bug（过去日期拒绝建题、坏 resolutions csv 报错退出）
- Polymarket 拉题管线（824ed5d）：`scripts/pm_fetch.py`（Gamma API 分页拉活跃市场 → horizon 三档 ≤14/≤45/≤90 天 → volume≥$1000 + 二值市场过滤 → 日期语境去重 → LLM 译中文 → 入题池 resolution_class=B）+ `scripts/pm_resolve.py`（混合揭晓：市场决议优先 → 独占窗口 3 天 → B 类 LLM 兜底 → 刷新校准器）；首批 10 题入库（#75–#84，is_public=False）
- P1 预测质量包（65a031d）：校准闭环（已揭晓题最后一条预测 fit 保序校准器 → `data/calibrator.json`，websearch 入口自动加载，<30 样本 identity；resolve 揭晓后自动刷新）+ 在线模型权重（brier_ema → 1/(ema+0.01) 归一化接 ensemble）+ 统计基线扩覆盖（黄金/布伦特/上证/道琼斯/人民币升破五类题族）+ 回测第三臂常数基线（compare_backtest 三方对比）；3 轮对抗审查修 2 Blocking + 4 Should-fix
- 项目内自检盯梢：`src/predictor/ops/health.py` 三件套复用 + 24h 事件规则 → `data/alerts/` 落盘 + Windows toast；schtasks 每日 9:35/16:40 两巡（正常静默、异常告警）
- 后台预测轮切 LLM 原生搜索引擎（daily/evolve 共用 `predict_with_websearch`，classic 仅回测）
- 气温/气候/天气类事件禁出（用户拍板：对 B 端无增量价值）
- A/B/C 三级自动揭晓（A 行情 / B LLM / C 人工）+ 存量 42 题 resolution_spec 回填
- Web dashboard（内网 8765 / 公开 8766）+ 5 道演示题入库（Task 24）
- ForecastBench 零样本回测脚本 + 官方题库获取通道（详见"已知待办"）

## 进行中

- 无活跃代码任务；系统在自动运转（daily/evolve/health 定时任务正常，signsrv 小时级自检运行中）

## 下一步

1. **人工揭晓待办**：#69（伦敦金 8-13，8-17 已降级 C）与 #9（EIA，8-22 宽限降级后）查官方数据填 `data/resolutions.csv` 后跑 `scripts/resolve.py`；#93/#97（保留的近似题）同属人工清单
2. Polymarket 题首次到期 8-21（#75 GPT-6）：跑 `python scripts/pm_resolve.py` 验证市场决议回填链路；后续考虑 pm_fetch/pm_resolve 接入 schtasks（未接，手工触发）
3. 观察后续 16:30 揭晓轮（LLM 揭晓 api_error 已定位为截断并修复，验证不再复现）
4. ForecastBench 官方提交渠道：邮件注册拿 bucket 后配置 `.env`（阻塞项见下）
5. 战绩榜数据积累（当前 `latest_scoreboard.json` buckets 为空，属正常——预测轮 0 新增，待揭晓题入账；校准器需 ≥30 已揭晓题才启用，当前远未达到）

## 阻塞 / 待外部条件

- **ForecastBench 官方 bucket 未配置**：`forecastbench_official.py` 中 6 处 TODO 均为此事（邮件注册后配置 `.env`；GitHub raw 被本机网络掐断，URL 待复核）
- 类型检查器未配置（mypy/pyright），可择期补上

## 已知问题（观察中）

- ~~2026-08-14 02:49 告警：LLM 揭晓失败 1 次（api_error/护栏）~~ → 已定位为 deepseek-v4-flash 推理吃光输出预算的截断，修复见 b86b206（截断跳 65536 上限 + reasoning effort=low）
- starlette TestClient 弃用警告（上游 fastapi 建议换 httpx2，等上游动作）
- fork system prompt 仍自称「pi 编码助手」（BRANDING.md 已知残留，不改）
- **2026-08-16 观察：predict 工具返回后 agent 生成最终展示回复时挂起**（对抗回归 1.6 场景实测：工具链数据全落库——建题 #94、概率 0.655、+77 证据文档，但 TUI 卡 "Working..." 17 分钟无输出，人工 kill）。挂点在 deepseek-v4-flash 生成长回复环节，疑似 77 条证据注入后生成挂起或 API 超时。与 fork git 化无关（工具链数据全通）。待查：predict 工具调用的 LLM 超时配置 + 长回复流式处理

## 环境速查

- Python 3.13（`.venv`，uv 管理）；测试 `env -u PYTHONPATH uv run pytest`（327 用例，约 5 分钟）
- lint/format：`uv run ruff check .` / `uv run ruff format --check .`（全仓 3 个既有文件未格式化，择期修）
- 定时任务：daily 9:00 / predict 9:05 / resolve 16:30 / health 9:35+16:40，均为 schtasks
- 交互 agent：`foresight`（pi-fork 全局命令，LLM=deepseek-v4-flash）；扩展改动后 /reload 即生效
