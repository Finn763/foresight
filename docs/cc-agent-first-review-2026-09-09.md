# CC 复核报告 — agent-first 审计（2026-09-09）

> 复核方：Claude Code（只读分析：未改任何代码/配置，未跑 pytest / uv，未安装任何东西）。
> 被复核对象：`docs/agent-first-audit-2026-09-09.md`（Hermes，2026-09-09）。
> 方法：superpowers brainstorming 的批判性复核——先摸项目实态，再逐条质疑前提、找遗漏、按 ROI 排序。
> 证据约定：结论一律以仓库/系统实态为准并引用 `path:line`；无法实机核验的显式标「未验证」。
> 主要一手证据：Task Scheduler 任务 XML（`C:\Windows\System32\Tasks\*`）、`shell/pi/.git/` 内部文件、`data/daily.log` 原始字节、`.foresight/extensions/foresight-tools.ts` 全文（1500 行）。

---

## 一、总评

**立场对，方法有硬伤。** "agent 不在环里"是真问题：12 个定时任务 0 个走 agent（已读任务 XML 核验），业务三环节确实全由 Python + 硬编码 prompt 驱动。

**但审计采信了 2026-08-15 的旧计划文档而未核验仓库实态**，导致第 7 项完全失效（fork 早已 git 化并完成过一次上游 rebase）、第 6 项把"模型自称 coding assistant"当成现存问题（实际 `SYSTEM.md` 已整体替换内置 prompt）。

**第 1 项的最小改法（`--yolo`）方向可商榷**：它会连同"禁止改代码"的边界一起关掉，不该作为无人值守每日轮的默认姿势。

---

## 二、逐项复核

### 1｜每日轮 agent 化 — **部分同意**

**前提成立（已实机核验）**：直读 Task Scheduler XML，抽样 6 个任务的 Action 段全部是 pythonw 跑脚本，无一调用 `foresight` CLI：

| 任务 | Action（XML 原文摘录） |
|---|---|
| `foresight-daily` | `pythonw.exe "D:\code\Foresight\scripts\run_silent.py" "scripts\daily.py" "data\daily.log"` |
| `Foresight-Predict` | `run_silent.py "scripts\evolve.py" "data\evolve.log" predict` |
| `Foresight-Resolve` | `run_silent.py "scripts\evolve.py" "data\evolve.log" resolve` |
| `Foresight-AutoPick` | `run_silent.py "scripts\autopick.py" "data\autopick.log"` |
| `Foresight-Dashboard` | `run_silent.py "scripts\ensure_dashboard.py" "data\dashboard-ensure.log"` |
| `foresight-signsrv` | 直跑 `D:\code\Foresight\scripts\ensure_signsrv.py`（**不经 run_silent**） |

全量 12 个任务（Glob `C:\Windows\System32\Tasks\*oresight*`）：`foresight-daily` / `Foresight-Predict` / `Foresight-Resolve` / `Foresight-Health` / `Foresight-Health-PM` / `foresight-signsrv` / `Foresight-AutoPick` / `Foresight-AutoPickIngest` / `Foresight-PMFetch` / `Foresight-PMResolve` / `Foresight-Backup` / `Foresight-Dashboard`，与 `docs/STATUS.md:71` 的「12 个」一致。
→ 审计的「12 个」「0 个走 agent」成立；但「**全部** pythonw run_silent.py」不准确（signsrv 直跑）。

**最小改法有三个具体问题**：

1. **`--yolo` 是一刀切，不是最小授权。** `foresight-tools.ts:159-164`：`-p` 无 UI 时任何写工具一律拒绝，要放行只能开 YOLO。而 YOLO 打开后：`before_agent_start` 注入 `YOLO_SYSTEM_BLOCK`（`foresight-tools.ts:63`，注入点 `:756-759`），且 `tool_call` 钩子不再拦截内置 `bash/edit/write`（`foresight-tools.ts:763-774`）。等于让无人值守的定时任务拥有整仓写权限。
2. **每日轮的核心工作不在 `predict` 工具里。** `predict` 命中已存在的未揭晓题会短路返回 `reused` 而**不预测**（`src/predictor/cli.py:80-87`）；新建题默认草稿 `is_public=False`（`cli.py:88`、`.foresight/APPEND_SYSTEM.md:3`）。agent 要"轮预测存量题"只能调 `run_round daily`（`foresight-tools.ts:1200-1201`），那与 schtasks 的 daily 是同一条 Python 路径，agent 增量只剩"选题 + 判断"。
3. **并发纪律未写。** `daily.py:166` 用 `acquire_lock`，锁被占时直接 `raise SystemExit`（`src/predictor/ops/lock.py:77-82`），**不等待**；而 `evolve.py:139-168` 有 90 分钟等待重试、`autopick_ingest.py:72` 有 `wait_acquire`。agent 轮与 Python daily 同跑必然对撞，需要显式定序或标记文件跳过。

**另有一处观察面缺口**：`src/predictor/ops/probes.py:82` 的 `_TASKS` 只含 3 个任务（`foresight-daily`/`foresight-predict`/`foresight-resolve`），`probes.py:105` 据此过滤，detail 固定为「三任务正常」（`probes.py:135`）。新增 agent 任务对健康巡检不可见（详见遗漏项 M2）。

**结论**：方向同意，改法要重写——先补"ops 模式"（M1）与"autopick 通道"（条目 2），再谈把 prompt 写进每日轮。

---

### 2｜选题 agent 化 — **部分同意**

**前提成立**：两段硬编码 prompt 在 `scripts/autopick.py:96`（STAGE1_PROMPT）与 `scripts/autopick.py:125`（STAGE2_PROMPT）。

**审计未说清的两点**：

1. **agent 目前根本没有跑 autopick 的通道。** `foresight-tools.ts:292-505` 的 22 条通道里没有 autopick；`run_round` 只映射 daily/evolve（`:1200-1201`）；YOLO 关闭时内置 bash 被硬拦（`:765-772`）。所以"autopick 降为工具"不是顺手的事：要新增通道 + 参数白名单 + 同步改 `tests/test_foresight_tools_contract.py:40-70` 的 `EXPECTED_CHANNELS`（该测试断言契约与注册双向一致，见 `:22-24`）。
2. **"选题"不止 autopick 一路。** 规则题族 `src/predictor/selection/families.py:29-161` 由 `daily.py:143`、`evolve.py:61` 调用；另有 `scripts/schedule_questions.py` 与 Polymarket `scripts/pm_fetch.py:87-122`。把"选题"写进 agent prompt 覆盖不到这三路——需要先定义"agent 选题是增量还是替代"。

**结论**：同意"让 agent 参与选题判断"，但必须把"加 autopick 工具通道"作为前置项单列，并明确与题族/Polymarket 三路的分工。

---

### 3｜Prompt 资产收敛到 `.foresight/skills/` — **部分同意**

**前提成立但少算了**：硬编码 prompt 至少 **7 处、6 个文件**（审计说 3 个文件）：

| 文件:行 | 常量 | 是否在用 |
|---|---|---|
| `scripts/autopick.py:96` | STAGE1_PROMPT | ✅ `:319` |
| `scripts/autopick.py:125` | STAGE2_PROMPT | ✅ `:341-349` |
| `src/predictor/websearch_predictor.py:27` | `_FORECAST_INSTRUCTIONS` | ✅ `:99` |
| `src/predictor/resolution/llm_resolver.py:16` | `_JUDGE_INSTRUCTIONS` | ✅ `:67` |
| `src/predictor/inference/filter.py:5` | FILTER_PROMPT | ✅ `:22` |
| `src/predictor/inference/forecast.py:6` | FORECAST_PROMPT | ✅ `:52` |
| `src/predictor/inference/search_terms.py:5` | SEARCH_TERMS_PROMPT | ✅ `:13` |
| `src/predictor/inference/summarize.py:6` | SUMMARIZE_PROMPT | ✅ `:15` |

**机制上可行但收益要分清**：`.foresight/skills` 与 `.foresight/prompts` 确实会被 pi 发现（`shell/pi/packages/coding-agent/src/core/resource-loader.ts:819-820`）。但 skills 是**给模型读的 markdown**，不是给 Python 读的模板——把 `websearch_predictor.py:27` 的 instructions 挪进 skill，Python 发出去的 prompt 一个字节都不会变。真正有收益的只有"agent 侧 prompt"（每日任务口径、选题规范）；引擎侧要改就得另加加载器与测试。

**结论**：agent 侧收敛可延后；**引擎侧迁移建议判 YAGNI**——现状每个 prompt 就在唯一使用它的模块内，且被回测/单测覆盖，迁移只增加一层间接。

---

### 4｜跨天记忆 hook 注入 — **同意**

**前提成立**：`before_agent_start` 钩子存在但只做 YOLO 注入（`foresight-tools.ts:756-759`）；`data/daily-brief.md` 由 `scripts/autopick.py:657-663` 写出，**全仓无人读**（grep `daily-brief` 仅命中 README、审计文档与 autopick 自身）。

**机制可行（已核验 API）**：`BeforeAgentStartEvent` 带 `prompt` 与 `systemPrompt`（`shell/pi/packages/coding-agent/src/core/extensions/types.ts:717-727`），注册签名 `types.ts:1282`。

**补充建议**：
- 另有更省事的替代——`--session-id <id>`（`shell/pi/packages/coding-agent/src/cli/args.ts:288`，"creating it if missing"）复用固定会话即可获得跨天记忆，但上下文无限膨胀、成本不可控。**推荐 hook 注入**（有界、可审计）。
- 注入内容建议"简报摘要 + 池子计数 + 取详情用哪个工具"：`questions`/`events` 工具已能取池子（`foresight-tools.ts:634-643`、`:712-721`），无需把全文塞进 system prompt。
- 钩子触发条件是 `before_agent_start`，对 `-p` 与 TUI 都生效；但注意它**只在 YOLO 开启时才有实现**（`:757` 的 `if (!yolo) return undefined;`），改造时要另加分支。

---

### 5｜健康告警进环 — **部分同意**

**前提成立**：告警落 `data/alerts/alert-*.md` + Windows toast（`scripts/health_check.py:8`），只在 dashboard 呈现（`src/predictor/web/server.py:45-103`、`:152-156`、`:248-268`）。agent 的 `health` 工具算的是 `assess(build_facts(...))` + probes（`foresight-tools.ts:695-710`），**不读告警文件**。

**偏差（审计未说）**：告警还包含 `health` 工具**不覆盖**的规则——24h 事件规则（`health_check.py:200-226`，含"认证失败特征"判定）与"当日 predictions 零新增"业务断言（`health_check.py:229-266`）。所以"读 health"≈但≠"看到告警"。

**结论**：方向同意，最小改法应写明验收口径——建议每日轮 prompt 同时读 `health` + `events`（`types=['prediction_skipped','llm_resolve_failed']`，`foresight-tools.ts:712-721` 已支持），或给 alerts 目录加一个只读通道。

---

### 6｜对外口径 — **部分同意**

**成立部分（已核验）**：
- `README.md:23` 把"autonomous topic-selection engine"写成 `scripts/autopick.py`。
- `foresight --help` 横幅：`` `${APP_NAME} - AI coding assistant with read, bash, edit, write tools` ``（`shell/pi/packages/coding-agent/src/cli/args.ts:262`）。
- 包描述"Coding agent CLI with read, bash, edit, write tools…"（`shell/pi/packages/coding-agent/package.json:4`）。
- 首次引导"Welcome to ${APP_NAME}, the minimal coding agent."（`shell/pi/packages/coding-agent/src/modes/interactive/components/first-time-setup.ts:56`）。

**误判部分（关键）**：**模型侧不是 coding assistant。** `.foresight/SYSTEM.md` 一旦加载就**整体替换**内置 prompt——`system-prompt.ts:46-47` 的 `if (customPrompt)` 分支直接返回自定义 prompt，`system-prompt.ts:128` 那句 "You are an expert coding assistant…" 根本不进入本项目模型上下文。加载路径 `resource-loader.ts:1023-1035`（项目级需 trusted），本项目已信任（`C:\Users\Administrator\.foresight\agent\trust.json:2` 为 `"D:\\code\\Foresight": true`）。因此 `shell/BRANDING.md:55-58` 把这类残留标为"不改，无功能影响"是有依据的——审计把 CLI 帮助文本与模型人格混为一谈。

**审计漏掉的真问题（更伤"agent 形象"）**：**npm bin 名冲突。** 公开包 `packaging/npm/foresight-agent/package.json:6-8` 的 bin 也叫 `foresight`，与 fork 的 bin（`shell/pi/packages/coding-agent/package.json:10-12`）同名。README 快速安装（`README.md:46`）先给用户的其实是**引擎 CLI**，agent 外壳的安装写在后面（`README.md:101`）；`shell/BRANDING.md:51-53` 自己警告过"不要把 npm 预测包 link 到 foresight 名下抢占"。
**另**：`README.md:106` 写基线 v0.84.1，而 `shell/BRANDING.md:10` 已是 v0.84.4——文档漂移。

---

### 7｜上游同步 — **反对（前提已失效）**

直接核验 `shell/pi` 的 git 实态：

- `shell/pi/.git/config:9-19`：`origin` 与 `upstream` 均指向 `https://github.com/earendil-works/pi`，`pushurl = no_push`。
- `shell/pi/.git/HEAD:1`：`ref: refs/heads/foresight-fork`。
- `shell/pi/.git/logs/HEAD:4-10`：clone → reset 到 `40a3d85` → 3 个补丁 commit（rebrand / fsCache / lockfile）→ **2026-09-04 成功 rebase 到 `b79e4cc8`（v0.84.4）**。
- `shell/BRANDING.md:64-72` 已写明 git 化后的同步流程；`docs/STATUS.md:29` 记录 2026-08-16 完成 git 化。

审计引用的 `docs/pi-化架构计划.md:66`（"shell/pi 无 git remote（最大维护隐患）"）是 **2026-08-15** 的陈述，次日即被推翻。**这一项应删除，不是延后**——保留它只会误导下一轮排期。

---

## 三、遗漏项（审计未覆盖、但"agent 为主"这条线上真正值得做）

### M1｜缺"ops 模式"：无人值守 agent 的权限闸门

- **现状证据**：`-p` 无 UI 时写工具一律拒绝（`foresight-tools.ts:159-164`）；唯一放行方式是 `--yolo`，而它同时解除 coding 边界并放开 `bash/edit/write`（`foresight-tools.ts:63`、`:756-759`、`:763-774`）。`.foresight/SYSTEM.md:17-32` 的边界因此只有"全开 / 全关"两档。
- **风险/收益**：没有中间档，条目 1 只能二选一——要么 agent 干不了活，要么定时任务拿到整仓写权限。补上后，无人值守每日轮可用"注册过的写工具放行、内置 bash/edit/write 继续拦"的最小授权姿态运行。
- **最小改法**：扩展加 `--ops` flag（或复用 YOLO 状态加 `opsOnly` 位）：`writeGate` 放行注册写工具，`tool_call` 钩子保持拦截 `bash/edit/write`；`before_agent_start` 注入一段"运维轮"说明而非整段 YOLO 块。
- **工时**：2-4h（含契约测试）。

### M2｜健康探针只覆盖 3/12 个调度任务

- **现状证据**：`src/predictor/ops/probes.py:82` `_TASKS = ("foresight-daily", "foresight-predict", "foresight-resolve")`；`probes.py:105` 按前缀过滤；正常态文案固定"三任务正常"（`probes.py:135`）。系统实有 12 个任务（见 §2.1 表格）。
- **风险/收益**：autopick / autopick_ingest / pm_fetch / pm_resolve / backup / dashboard / health 自身的失败对健康巡检不可见；新增的 agent 任务同样不可见（直接削弱条目 1 的验收能力）。
- **最小改法**：`_TASKS` 扩为 12 项（或改为可配置清单），同步 `tests/ops/test_probes.py`。
- **工时**：1h。

### M3｜`logs` 工具编码损坏——agent 诊断链路断点

- **现状证据**：`scripts/run_silent.py:78` 以 **utf-8** 打开日志；`foresight-tools.ts:733` 却以 **gbk** 读取（工具描述还写着"GBK 容错"，`:1016`）。实测 `data/daily.log`：文件头部（cmd 时代旧段）按 UTF-8 读是乱码，尾部（新段）按 UTF-8 读是正常中文（"今日新增预测 19"等）——**混合编码文件**。
- **风险/收益**：agent 唯一的日志读取工具，对**最新几行**返回乱码，恰好是排障最需要的部分；条目 5"每日轮读 health/日志"的验收会被它直接卡住。
- **最小改法**：逐行混合解码（先 utf-8、失败回退 gbk）或先做日志轮转把新文件统一为 utf-8；同步改工具描述与 `tests/test_foresight_tools_contract.py`。
- **工时**：1h。

### M4｜无人值守 agent 轮没有预算护栏

- **现状证据**：全仓无预算/花费上限（grep `budget|max_cost|spend` 在 `src/`、`scripts/` 零命中，仅命中文档散文）；用量只做事后记账——`src/predictor/llm/client.py:52-59` 的 `dump_daily_usage`，由 `scripts/daily.py:158` 落一行到 `data/daily.log`（实测当日 `prompt=2862443 completion=319970 calls=57`）。`LLMClient` 只有重试上限 `max_retries=2`（`client.py:116`），无调用次数/花费熔断。
- **风险/收益**：agent 轮是**新增的**一整段自主会话，叠加每次 predict 3 路采样（`websearch_predictor.py:153`）；一旦循环或反复重试，没有任何东西会叫停它。这是自主性提高后最先暴露的风险。
- **最小改法**：`system` 工具暴露当日 usage；每日轮开头做一次"当日用量 vs 阈值"预检，超限即中止并记 `evolution_log`。
- **工时**：2-3h。

### M5｜`predict` 工具语义与"每日轮预测"不匹配

- **现状证据**：命中已存在未揭晓题 → 返回 `reused` 且**不预测**（`src/predictor/cli.py:80-87`）；新建题默认草稿（`cli.py:88`、`.foresight/APPEND_SYSTEM.md:3`）；工具 schema 只有 `question` / `closes` 两个参数（`foresight-tools.ts:1087-1090`），无法对指定 qid 出预测。
- **风险/收益**：按条目 1/2 写的每日轮 prompt 很容易"看起来跑了、其实什么都没预测"（命中判重短路），或产出不进公开榜的草稿——直接损害"公开战绩"这条硬规则。
- **最小改法**：短期在 `APPEND_SYSTEM.md` 写清工具映射（新题→`predict`；存量轮→`run_round daily`），中期给通道加 `predict --qid`。
- **工时**：1h（纯 prompt）/ 3h（加 `--qid`）。

### M6｜agent 轮没有验收与回归

- **现状证据**：Python 侧有兜底与事件（`daily.py:78-99` 的 `_predict_safely` + `round_started/completed`），工具侧有契约测试（`tests/test_foresight_tools_contract.py`）；但**没有任何东西**断言"agent 轮跑完会产生预测"。
- **风险/收益**：条目 1 上线后如果 agent 只是空转（未选中题、工具被拒、超时），配合 M2 的探针盲区，可以静默失败很久而无人察觉。
- **最小改法**：一个冒烟脚本：临时 DB 下跑 `foresight -p "<每日任务>" --mode json --ops`，断言 `questions`/`predictions` 有新增或 session 里出现预期工具调用。
- **工时**：2-3h。

---

## 四、优先级排序（7 项 + 6 遗漏，按 ROI）

排序原则：先做能**解锁**后面所有事的（闸门/观测），再做**低成本高确定收益**的，最后做**收益真实但可等**的；已完成或无效的直接剔除。

| # | 条目 | 判定 | 工时 | 为什么这个位置 | 验收标准（怎么证明真的生效） |
|---|---|---|---|---|---|
| 1 | **M3** logs 混合解码 | 先做 | 1h | 最便宜，且是条目 1/5 验收的前提 | `foresight -p "用 logs 工具读 daily 日志最后 20 行" --mode json` 输出含「今日新增预测」等中文且无替换符；文件头部旧段不抛异常 |
| 2 | **条目 4** 简报注入 | 先做 | 2h | 纯增益、零调度风险、独立可交付 | `foresight -p "复述昨日简报要点" --mode json` 输出含 `data/daily-brief.md` 里的选题标题；session jsonl 的 systemPrompt 含注入标记 |
| 3 | **M1** ops 模式 | 先做 | 2-4h | 条目 1 的硬闸门，不做就只能 `--yolo` 全开 | `foresight -p "用 run_round 跑 daily 轮" --mode json --ops` 返回 `exit=0` 且 `data/latest_scoreboard.json` mtime 更新；同模式下 `foresight -p "用 bash 执行 echo hi" --mode json --ops` 的会话记录里该调用被 `block:true` |
| 4 | **条目 2 + autopick 通道** | 先做 | 0.5d | agent 选题是审计自己认定的最大增量，且是条目 1 的依赖 | `uv run pytest tests/test_foresight_tools_contract.py` 全绿且 `EXPECTED_CHANNELS` 含 autopick；`foresight -p "用 autopick 工具跑一次 dry-run" --mode json --ops` 输出含候选计数 |
| 5 | **M2** 探针覆盖 12 任务 | 先做 | 1h | 新增任务的可观测性，先于条目 1 | `uv run pytest tests/ops/test_probes.py` 全绿；`health` 工具 detail 不再固定"三任务正常"；禁用任一非核心任务后 `health` 返回 error |
| 6 | **条目 5** 健康告警进环 | 先做（随条目 1 的 prompt） | 0.5h | 成本极低，但需先有 agent 轮才有落点 | 每日轮 prompt 里出现 `health` + `events(types=['prediction_skipped','llm_resolve_failed'])` 调用；当日有告警时 agent 输出中点到告警内容 |
| 7 | **M5** `predict` 语义澄清 | 可延后 | 1h（prompt）/ 3h（加 `--qid`） | 不阻塞功能，但写错 prompt 会静默空转 | `APPEND_SYSTEM.md` 写明映射；`foresight -p "预测某已存在未揭晓题" --mode json` 返回 `reused` 时，agent 不得声称"已出新预测" |
| 8 | **M6** agent 轮验收/回归 | 先做（随条目 1） | 2-3h | 没有它，条目 1 失败不可见（叠加 M2 盲区） | 冒烟脚本在临时 DB 跑 `foresight -p … --mode json --ops`，断言 `questions`/`predictions` 有新增或 session 含预期工具调用；脚本可重复执行 |
| 9 | **条目 1** 每日轮 agent 化（旗舰） | 先做 | 0.5-1d | 依赖 #3/#4/#5，并吸收 #6/#7/#8 | 新任务跑 `foresight -p … --mode json --ops` 后：`questions` 见当日新增、`events` 有 `prediction_added`、`health` 的 scheduler probe 覆盖该任务；断网/无 key 时 agent 轮失败但 Python 兜底仍产出当日预测 |
| 10 | **M4** 预算护栏 | 可延后 | 2-3h | 收益随自主性上升，但可在条目 1 之后补 | `system` 工具返回当日 usage；阈值调至 1 时 agent 轮在第二次 predict 前中止并记事件 |
| 11 | **条目 6 + npm bin 冲突** | 可延后 | 2h | 真实但不阻塞功能 | `foresight --help` 首行不再是 "AI coding assistant"；npm 引擎包改名后 `npm i -g` 不覆盖 `foresight` 这个 bin；`README.md:106` 基线版本与 `BRANDING.md:10` 一致 |
| 12 | **条目 3** prompt 收敛 | 部分不值得做 | agent 侧 0.5h / 引擎侧 0 | agent 侧可延后；引擎侧 YAGNI（见 §2.3） | 仅当做 agent 侧：`.foresight/skills/<name>/SKILL.md` 存在且会话里 `/skill:<name>` 可见 |
| 13 | **条目 7** 上游同步 | **不值得做（已完成）** | 0 | 前提已被仓库实态推翻 | 删掉 `docs/pi-化架构计划.md:66` 的失效风险项即可；`git -C shell/pi remote -v` 仍显示双 remote + `no_push` |

---

## 五、Top 3 可执行动作

> 三条都满足：不依赖对方、当天可验证、不改动调度。第 4 名（autopick 通道）与第 9 名（每日轮改造）紧随其后。

**1. 修 `logs` 工具混合编码**
- 一句话任务：把 `foresight-tools.ts:733` 的 `encoding='gbk'` 改为逐行 utf-8→gbk 回退解码，并同步工具描述（`:1016`）与契约测试。
- 验收命令：`foresight -p "用 logs 工具读 daily 日志最后 20 行" --mode json` —— 输出含「今日新增预测」等可读中文、无替换符。

**2. `before_agent_start` 注入昨日简报**
- 一句话任务：在 `foresight-tools.ts:756` 的钩子里（YOLO 分支之外）追加：读 `data/daily-brief.md` 摘要 + 未揭晓题计数，拼到 `systemPrompt`。
- 验收命令：`foresight -p "复述昨日简报要点" --mode json` —— 输出含 `data/daily-brief.md` 中的选题标题。

**3. 加 `--ops` 模式（写工具白名单放行，bash/edit/write 继续拦）**
- 一句话任务：扩展加 `--ops` flag：`writeGate`（`:153-175`）放行注册写工具，`tool_call` 钩子（`:763-774`）保持拦截内置 `bash/edit/write`，`before_agent_start` 注入"运维轮"说明。
- 验收命令：`foresight -p "用 run_round 跑 daily 轮" --mode json --ops` —— 返回 `exit=0`；再跑 `foresight -p "用 bash 执行 echo hi" --mode json --ops` —— 会话记录中该调用为 `block:true`。

---

## 附：本次复核未验证项

- `foresight -p` / `--mode json` / `--system-prompt` / session 恢复的**端到端**行为：仅在源码确认参数存在（`shell/pi/packages/coding-agent/src/cli/args.ts:95,110,121,157,288`），未实机执行（任务约束：只读）。
- 12 个任务中未逐一读 XML 的 6 个（Health / Health-PM / AutoPickIngest / PMFetch / PMResolve / Backup）：其 Action 形制由 `docs/hermes-fix-report-*.md` 与 `docs/STATUS.md:71` 佐证，非本次直读。
- `health` 工具输出与 dashboard 告警横幅的**逐项一致性**：只确认两者数据源不同（`foresight-tools.ts:695-710` vs `health_check.py:8`），未逐条比对。
