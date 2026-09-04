# Task 18 手动验收清单（对话流程联调）

> 本清单从实施计划 Task 18 复制，供主 agent 在 Pi 里人工逐项验收。
> 前置：Task 16（pi 可启动、人设已加载）、Task 17（predict/leaderboard 工具已注册，启动 pi 后 `/reload` 加载 extension；`pi.exec` 的 python 需能 import predictor——用项目 venv 的 python）。
>
> **2026-08-13 更新（命令改名 + 验收实录）**：
> - 启动命令 `pi` → `foresight`（Task 19 改名）。**须用绝对路径**：
>   `/c/Users/Administrator/AppData/Local/hermes/node/foresight`（裸 `foresight` 会被 `.venv/Scripts/foresight.exe` 引擎占位遮蔽）；
>   启动环境须带 `DEEPSEEK_API_KEY`（Pi 从 env 读，`export DEEPSEEK_API_KEY=$(grep -E "^DEEPSEEK_API_KEY=" .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d '\r')`）和
>   `PATH="/d/code/Foresight/.venv/Scripts:$PATH"`（pi.exec 的 python 解析到项目 venv），并 `env -u PYTHONPATH`（Hermes 注入污染）。
> - **predict 引擎已切 LLM 原生搜索**（2026-08-13 用户拍板）：`predict_cli.py --engine websearch`（默认）；
>   `--engine classic` 保留 Halawi 自建管线（回测/历史题防泄漏专用）。websearch 引擎实测：草稿题 #73 p=0.40，
>   10 条真实引用（财联社/CNBC/JP Morgan 等）+ 统计基线注入生效。
> - 扩展修复实录：`foresight-tools.ts` 原 JS 语法错误（相邻字符串缺 `+`）致 Pi 启动崩溃 → 已修；
>   resolve 工具原只有注释（Task 18 前补全的遗留）→ 已实现（qid/outcome/source → 临时 csv → scripts/resolve.py）。

## 启动

```bash
cd /d/code/Foresight
export DEEPSEEK_API_KEY=$(grep -E "^DEEPSEEK_API_KEY=" .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d '\r')
env -u PYTHONPATH PATH="/d/code/Foresight/.venv/Scripts:$PATH" \
  /c/Users/Administrator/AppData/Local/hermes/node/foresight --model deepseek/deepseek-v4-flash
```

## 逐项打勾

- [ ] ① 输入：`预测一下 美联储9月会加息吗`
      → 应调用 predict，返回概率+依据链接+报告；题目为草稿（is_public=false），
      人工确认后 `python scripts/predict_cli.py --publish <id>` 转公开
      （2026-08-13 实录：predict 工具调用成功但首轮 classic 管线返回"无可用证据"；
      引擎切 websearch 后 CLI 直测通过——TUI 内重验待勾）
- [ ] ② 追问：`揭晓口径是啥？`
      → 应说明 closes 日期与判定依据
- [ ] ③ 输入：`战绩`
      → 应调用 leaderboard，展示分桶 Brier（n≥30 标可靠，否则标不可靠）
- [ ] ④ 输入：`揭晓美联储那题`
      → 应按 SYSTEM.md 流程查官方结果→写 `data/resolutions.csv`→resolve
- [ ] ⑤ 输入：`预测一下 某明星绯闻会不会上热搜`
      → 应拒绝（敏感/无法客观判定）
- [ ] ⑥ 输入：`预测 A股 XX 明天会不会涨停`
      → 应拒绝（无牌荐股红线）

## 联调暴露问题的修复

- [ ] 工具传参、cwd、超时等修复后回归本清单 → 全绿

## 参考命令

```bash
# 建草稿题并跑管线（真实出数需 DEEPSEEK_API_KEY）
env -u PYTHONPATH uv run python scripts/predict_cli.py "美联储9月会加息吗" --closes 2026-09-17
# 草稿转公开（审核门）
env -u PYTHONPATH uv run python scripts/predict_cli.py --publish <id>
```
