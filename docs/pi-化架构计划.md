# Foresight pi 化架构计划

> 状态：**已研究确认，待开工**。开工前置：另一会话「对抗层验收」（task3，adversarial-plan.md）完成后。
> 研究日期：2026-08-15。本文档 = 可行性研究结论 + 分阶段实施计划，研究过程未改任何代码。

## 一、目标

整体基于 pi fork（shell/pi，已 rebrand 为 @foresight/foresight-agent，configDir=.foresight），
把 Foresight 做成个人 agent 项目：`foresight` 命令启动，可对话、可自动预测，预测功能按 pi
扩展开发规格落地，定时任务走系统调度（schtasks）调用 agent 非交互模式。

## 二、可行性结论（已研究验证）

| 需求 | pi 能力 | 验证状态 |
|---|---|---|
| 和 agent 对话 | 交互 TUI + 消息队列 + session 自动保存/恢复 | ✅ task1 冒烟已通 |
| 预测功能扩展化 | registerTool + 事件钩子 + skills + prompt templates | ✅ predict/leaderboard/resolve 17/17 PASS |
| 自动预测 | `-p/--print`、`--mode json`、`--mode rpc` 三种 headless 模式 | ✅ 冒烟已验证 |
| 命令入口 | fork piConfig（name=foresight, configDir=.foresight）+ npm link | ✅ task1 完成 |

## 三、架构分层

1. **pi fork（外壳）**：agent 循环、对话、会话管理、多 provider、工具调度。管"怎么想"。
2. **Python predictor（引擎，不动）**：DuckDB 存储、概率计算、Brier/校准、回测、行情源。
   管"怎么算"。现有自研 agent 逻辑很薄（websearch_predictor.py 仅 292 行），pi 接管
   agent 循环后 predictor 收敛为纯领域引擎。
3. **扩展层（桥，已有）**：`.foresight/extensions/foresight-tools.ts` 注册
   predict / leaderboard / resolve 三工具，薄壳调 Python 脚本（venv python + `-E -X utf8`）。
4. **调度层（schtasks，已有底座）**：6 个计划任务（daily/predict/resolve/health×2/signsrv）。

**关键决策**：预测引擎不重写为 TS。extension 是壳，Python 是引擎。重写=复杂度翻倍，违背初衷。

## 四、定时任务落点（定稿）

分环节，不能一刀切：

| 环节 | 方式 | 理由 |
|---|---|---|
| 每日预测轮（9:00） | `foresight -p "每日预测任务…" --mode json` | agent 参与：选题 + 预测 + 简报。冷启动不丢智能，预测任务不需要强跨天上下文 |
| 揭晓轮（16:30） | 纯 Python（现状不动） | 确定性环节：查官方数据写 outcome 计 Brier。agent 参与有幻觉风险，死板是优点 |
| 健康巡检 | 纯 Python（现状不动） | 同上 |
| pm_fetch 拉题 | 纯 Python（现状不动） | 拉取+筛选已含 LLM，agent 壳无实质增量 |

**agent 增量（本次计划新增）**：每日 prompt 加入"自主选题"——扫新闻挑 1-2 件值得预测的
新事件，建题、预测、入榜。这是纯脚本做不到、agent 价值最大的一步。每日流程：
选题建题 → 轮预测存量题 → 写简报落盘（data/daily-brief.md，供人随时读）。

**pi-loop 不用（已研究源码，定论）**：7 天硬过期（MAX_LOOP_EXPIRY_MS 写死）、fire 依赖
进程常驻、当前任务组合用不上事件/任务系统长处。列入"未来主动性改造"备选：等 Foresight
进化成主动型个人助理（自主选题、自主节奏、多步骤）时再上，届时形态 = schtasks 开机拉起
常驻 foresight + dynamic loop（nextInterval 续期模式）。

## 五、分阶段计划

| 阶段 | 内容 | 预估 |
|---|---|---|
| 0 | 等另一会话对抗层验收（task3）完成 | 前置 |
| 1 | Python 撤销：删 `~/.local/bin/foresight-predictor.exe.bak`、`uv tool uninstall predictor` | 0.5h |
| 2 | 上游同步改造：shell/pi 重挂 git remote（clone 上游 + rebrand 补丁化：package.json name/bin/piConfig 三处），之后 `git pull --rebase` 即可同步 | 1d |
| 3 | 扩展完善：补 backtest 工具 + skills（预测工作流）+ 事件钩子（揭晓提醒/自动落盘） | 1-2d |
| 4 | cron 改造：预测轮改 `foresight -p --mode json`（含自主选题 prompt + 简报落盘）；揭晓/健康/校准留 Python | 0.5d |
| 5 | 体验收尾：SYSTEM.md 打磨、STATUS.md 同步、文档 | 0.5d |

## 六、风险清单

1. **shell/pi 无 git remote**（最大维护隐患）：上游 5,685 commits 很活跃，现状同步靠手工。
   阶段 2 解决：重挂上游 + rebrand 改动补丁化。
2. **对抗层验收未完成**：另一会话进行中，卡在扩展热加载缓存 bug。本计划全部步骤定位为
   验收完成后开工，不并行冲突。
3. **fork 残留 branding**：system prompt 自称"pi 编码助手"（BRANDING 已知残留，不改）。
4. **成本**：每日 agent 轮 = 一次 LLM 会话费。选题+预测+简报预算可控，量级与现有
   predict_with_websearch 轮相当。

## 七、参考证据

- task1-report.md：全局命令恢复 + `-p` 冒烟 + auth.json 配置
- task2-report.md：扩展 17/17 断言 + `-E -X utf8` 双 bug 修复
- pi-loop 源码审查（npm 包 0.7.3 dist）：`MAX_LOOP_EXPIRY_MS = 7*24h`、runtime timer fire、
  进程退出即停摆、持久化仅恢复状态不拉起进程
- pi docs：usage.md（`-p`/`--mode json`/`--mode rpc`）、extensions.md（registerTool + 事件钩子）
