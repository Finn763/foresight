# Foresight — AI 事件概率预测引擎

[English](README.md) | 中文

舆情/事件概率预测系统：输入事件 → 输出概率 + 时间线 + 可溯源报告，Brier 战绩公开可验证。

架构 = **pi-fork 交互 agent 外壳**（`shell/pi`，改名 `@foresight/foresight-agent`）+ **Python 预测引擎**（`src/predictor`，Halawi 式 RAG 管线 + 多模型集成 + 校准 + 回测），agent 通过工具扩展桥（`.foresight/extensions/foresight-tools.ts`）调用 Python 引擎。

> 免责声明：本项目输出为统计模型生成的概率估计，**不构成投资、博彩或任何决策建议**。预测市场 API 仅作内部先验参考。

## 工作原理

![Foresight 预测引擎架构](docs/architecture.zh-CN.svg)

[▶ 交互版](https://finn763.github.io/foresight/architecture.zh-CN.html)（平移 / 缩放 / 主题切换 / 焦点追踪）

## 技术要点

- **预测管线**：Halawi 五步（搜索词生成 → 检索 → 相关性过滤 → 摘要 → 基准率+预测）+ 5–12 模型集成 + 保序校准层
- **有效方法按证据强度**：RAG 检索（+50%）> 多模型集成取中位数 > 超级预测者提示词 > 概率极端化 > 市场/人群先验
- **战绩闭环**：A 类双源行情比对 / B 类 LLM 揭晓器（web_search 证据 + 双采样一致 + 置信护栏）/ C 类人工兜底；Brier 分数与校准曲线随揭晓自动更新
- **回测**：ForecastBench 公开题库零样本回测（防泄漏基准；种子数据见 `data/fb_seed/`）
- **选题管线**：Polymarket Gamma API 拉活跃市场 → horizon 三档筛选 → LLM 译中文入题池；另有**自主选题引擎** `scripts/autopick.py`（RSS 聚合 20 源 → LLM 筛选打分 → 出题含可查证揭晓条件 → 注册表判重 → 每日简报）
- **运维**：每日预测轮/揭晓轮/健康巡检三件套；健康巡检 `wait_acquire` 排队等锁（杜绝对撞失明）；Windows schtasks 无窗口编排见 `scripts/run_silent.py`

## 目录结构

```
foresight/
├── shell/pi/            # pi coding agent fork（MIT, upstream: earendil-works/pi）
│   └── BRANDING.md      #   fork 改名原理与上游同步方式（见 shell/BRANDING.md）
├── src/predictor/       # Python 预测引擎（管线/校准/揭晓/回测/web/ops）
├── scripts/             # 运维脚本（daily/evolve 轮、健康自检、web 入口、autopick 自主选题）
├── tests/               # 全量单测（pytest）
├── data/fb_seed/        # ForecastBench 种子题库快照（回测用）
├── .foresight/          # agent 人格护栏 SYSTEM.md + 工具扩展 foresight-tools.ts
├── docs/                # 技术文档（ForecastBench 提交渠道调研）
└── pyproject.toml       # uv 管理的 Python 依赖（3.12+）
```

## 快速开始

### 1. Python 引擎

```bash
# 需要 Python 3.12+ 与 uv
uv sync
uv run pytest            # 全量单测
```

配置：复制 `.env.example` 为 `.env`，填入 `DEEPSEEK_API_KEY`（默认 LLM）；可选 `FRED_API_KEY` / `EIA_API_KEY` / `NEWSAPI_KEY` 增强数据源，无 key 时对应源自动降级跳过。

本地 dashboard：

```bash
python scripts/web_server.py                 # 内部模式 http://127.0.0.1:8765
python scripts/web_server.py --mode public   # 对外战绩榜（内部 API 一律 404）
```

命令行预测：

```bash
python scripts/predict_cli.py '{"question": "…", "closes": "2026-12-31"}'
```

自主选题（每天从当日新闻出 1–2 道题）：

```bash
python scripts/autopick.py --dry-run   # 全流程试跑，不落盘
python scripts/autopick.py             # 正式跑（按日幂等）
```

### 2. foresight agent（交互外壳）

```bash
cd shell/pi
npm install && npm run build
cd packages/coding-agent && npm link    # 全局命令 foresight
```

在项目根目录启动 `foresight`，配置目录为 `.foresight/`（人格护栏 `SYSTEM.md`、工具扩展 `extensions/foresight-tools.ts`）。扩展提供 questions / leaderboard / predict / resolve 等工具，薄壳调用 Python 引擎（venv python + `-E -X utf8`，详见扩展文件头注释）。

fork 说明：本仓库 `shell/pi` 基于 [earendil-works/pi](https://github.com/earendil-works/pi)（MIT License © Mario Zechner）v0.84.1，仅改 package.json 三处（name/bin/piConfig）+ 扩展加载器一处 `fsCache:false`，详见 `shell/BRANDING.md`。

## 文档地图

| 文件 | 用途 |
|---|---|
| `docs/forecastbench-submission-survey.md` | ForecastBench 官方提交渠道调研 |
| `shell/BRANDING.md` | pi fork 的品牌化改动清单与上游同步流程 |

## License

MIT。`shell/pi` 子目录遵循其上游 MIT 许可（© Mario Zechner），版权声明保留于 `shell/pi/LICENSE`。
