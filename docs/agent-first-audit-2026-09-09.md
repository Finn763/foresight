# Agent-first 审计方案（待 CC 复核）

> 提出方：Hermes（2026-09-09）。复核方：Claude Code + superpowers。
> 你的任务：**批判性复核下面这份审计**，用 superpowers 的方法（brainstorming / 系统分析）找出遗漏、纠正误判、并给出可执行优先级。

## 一、项目背景（事实）

- 项目：`D:\code\Foresight`，事件概率预测引擎。**用户明确：一切以 agent 为主是这个项目的初衷。**
- 架构现状：pi fork 外壳（`shell/pi`，rebrand `@foresight/foresight-agent`，npm 全局命令 `foresight` v0.3.1）
  + Python 引擎（`src/predictor`，DuckDB）。
- agent 侧资产只有三样：`.foresight/SYSTEM.md`（人格/边界）、`.foresight/APPEND_SYSTEM.md`（工具清单）、
  `.foresight/extensions/foresight-tools.ts`（1500 行，24 个工具 + 3 个 hook + `/yolo` 命令）。
- 业务三环节全部由 Python 脚本 + 硬编码 prompt 驱动：
  - 选题：`scripts/autopick.py`（两段 prompt：初筛打分 + 出题）、`scripts/pm_fetch.py`（Polymarket）
  - 预测：`scripts/daily.py` / `scripts/evolve.py` → `src/predictor/websearch_predictor.py`（硬编码 prompt，3 采样）
  - 揭晓：`src/predictor/resolution/auto_resolve.py` + `llm_resolver.py`（硬编码 prompt）+ `scripts/pm_resolve.py`
- 调度：12 个 schtasks，**全部** `pythonw run_silent.py <script>`，0 个走 agent。
- 规划文档：`docs/pi-化架构计划.md`（阶段 4 = "预测轮改 `foresight -p --mode json`"，**未开工**）。
- 已验证可用：`foresight -p`（非交互）、`--mode json`、`--system-prompt`、session 恢复。

## 二、待复核的审计（Hermes 版）

| # | 环节 | 现状 | 差距 | 最小改法 |
|---|---|---|---|---|
| 1 | 每日轮 | daily/evolve 纯 Python，09:00 schtasks | agent 不在环里 | 新增 schtasks 跑 `foresight -p "<每日任务>" --mode json --yolo`，daily 留兜底 |
| 2 | 选题 | autopick 两段写死 prompt | agent 不参与判断 | 选题写进每日轮 prompt，autopick 降为工具 |
| 3 | Prompt 资产 | 散在 3 个 Python 文件 | 无法用 pi skills 迭代 | 收敛到 `.foresight/skills/` |
| 4 | 跨天记忆 | 每天冷启动，`data/daily-brief.md` 只落盘不回灌 | agent 不知道昨天选了什么 | `before_agent_start` hook 注入昨日简报+池子摘要 |
| 5 | 健康告警 | 红条只进 dashboard | agent 不知道要修什么 | 每日轮开头读 health |
| 6 | 对外口径 | README/架构图写"选题引擎=autopick.py"；CLI banner 自称 "AI coding assistant" | 对外不是 agent 形象 | 改 README 定位 + fork branding 残留 |
| 7 | 上游同步 | `shell/pi` 无 git remote，手工拷 | 维护隐患 | 阶段 2：clone 上游 + rebrand 三处补丁化 |

## 三、你要交付什么

写一份报告到 **`docs/cc-agent-first-review-2026-09-09.md`**，结构：

1. **总评**（3 行内）：这份审计的立场对不对？有没有方向性错误？
2. **逐项复核**：7 项每项给 `同意 / 部分同意 / 反对` + 理由（必须引用代码事实 `path:line`，不许只凭文档）。
3. **遗漏项**：审计没提到、但"以 agent 为主"这条线上真正值得做的改进。每项给：现状证据（path:line）→ 风险/收益 → 最小改法 → 预估工时。
4. **优先级排序**：全部条目（7 + 遗漏）按 ROI 排序，标注"先做/可延后/不值得做"，并说明每项做完的**验收标准**（怎么证明它真的生效了）。
5. **Top 3 可执行动作**：每项写成"一句话任务 + 验收命令"。

## 四、硬约束

- **只读分析 + 写这一个报告文件**。不要改任何代码/配置，不要跑 `pytest`/`uv run`（耗时长），不要装东西。
- 结论必须有代码事实支撑（引用 `path:line`）；不确定就写"未验证"。
- 别重复文档里已有的说辞当证据——去读代码。
- 若你判断某条审计项根本不该做（YAGNI），直接说，并说明为什么现状已经够了。
