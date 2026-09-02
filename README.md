# Foresight — AI Event Probability Prediction Engine

[中文文档](README.zh-CN.md) | English

Opinion/event probability prediction system: event in → probability + timeline + sourced report out. Brier track record publicly verifiable.

Architecture = **pi-fork interactive agent shell** (`shell/pi`, renamed `@foresight/foresight-agent`) + **Python prediction engine** (`src/predictor`: Halawi-style RAG pipeline + multi-model ensemble + calibration + backtesting). The agent invokes the Python engine through an extension bridge (`.foresight/extensions/foresight-tools.ts`).

> Disclaimer: outputs are probabilistic estimates produced by statistical models and do **not** constitute investment, gambling, or any other decision advice. Prediction-market APIs are used only as internal priors.

## How it works

![Foresight prediction engine architecture](docs/architecture.svg)

[▶ Interactive version](https://finn763.github.io/foresight/architecture.html) (pan / zoom / theme switch / focus tracing) — generated with [Archify](https://github.com/tt-a1i/archify) (MIT, see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md))

## Highlights

- **Prediction pipeline**: Halawi five-step (search-term generation → retrieval → relevance filtering → summarization → base-rate + prediction) + 5–12 model ensemble + order-preserving calibration layer
- **Methods ranked by measured evidence**: RAG retrieval (+50%) > multi-model ensemble median > superforecaster prompting > probability extremization > market/crowd priors
- **Resolution loop**: class A dual-source market-data comparison / class B LLM resolver (web_search evidence + dual-sampling agreement + confidence guardrail) / class C manual fallback; Brier scores and calibration curves update automatically as questions resolve
- **Backtesting**: zero-shot on the public ForecastBench question bank (leak-safe benchmark; seed data in `data/fb_seed/`)
- **Question sourcing**: Polymarket Gamma API pipeline **plus an autonomous topic-selection engine** (`scripts/autopick.py`) — RSS aggregation (20 sources) → LLM screening & scoring → question drafting with verifiable resolution criteria → registry-based dedup → daily brief (`data/daily-brief.md`)
- **Ops**: daily prediction/evolution rounds + health checks (queue-on-lock via `predictor.ops.lock.wait_acquire` instead of blind-fail retries); headless Windows schtasks orchestration via `scripts/run_silent.py`

## Directory layout

```
foresight/
├── shell/pi/            # pi coding-agent fork (MIT, upstream: earendil-works/pi)
│   └── BRANDING.md      #   rebrand rationale & upstream-sync procedure
├── src/predictor/       # Python engine (pipeline / calibration / resolution / backtest / web / ops)
├── scripts/             # ops scripts (daily/evolve rounds, health check, web entry, autopick engine)
├── tests/               # full pytest suite
├── data/fb_seed/        # ForecastBench seed question snapshots (backtesting)
├── .foresight/          # agent guardrails SYSTEM.md + tool extension foresight-tools.ts
├── docs/                # technical docs (ForecastBench submission survey)
└── pyproject.toml       # uv-managed Python dependencies (3.12+)
```

## Quick start

### 0. Quick install (npm, no Python or uv needed)

```bash
npm i -g foresight-agent
foresight "Will the Fed cut rates in September?" --closes 2026-09-17
```

The npm installer fetches a prebuilt, self-contained binary for your platform (Windows x64, macOS x64/arm64, Linux x64) — the Python runtime and all dependencies are bundled inside, so no Python, uv, or package manager setup is required. To use your own API key, copy `.env.example` to `.env` and set `DEEPSEEK_API_KEY`, or set it as an environment variable.

### 1. Python engine

```bash
# Requires Python 3.12+ and uv
uv sync
uv run pytest            # full test suite
```

Configuration: copy `.env.example` to `.env` and set `DEEPSEEK_API_KEY` (default LLM); optional `FRED_API_KEY` / `EIA_API_KEY` / `NEWSAPI_KEY` enhance data sources — sources degrade gracefully and are skipped when keys are absent.

Local dashboard:

```bash
python scripts/web_server.py                 # internal mode http://127.0.0.1:8765
python scripts/web_server.py --mode public   # public scoreboard (internal APIs 404)
```

Command-line prediction:

```bash
python scripts/predict_cli.py '{"question": "…", "closes": "2026-12-31"}'
```

Autonomous topic selection (drafts 1–2 questions per day from today's news):

```bash
python scripts/autopick.py --dry-run         # full pipeline, no files written
python scripts/autopick.py                   # run for real (idempotent per day)
```

### 2. foresight agent (interactive shell)

```bash
cd shell/pi
npm install && npm run build
cd packages/coding-agent && npm link    # global `foresight` command
```

Start `foresight` from the project root; its config directory is `.foresight/` (guardrails `SYSTEM.md`, tool extension `extensions/foresight-tools.ts`). The extension exposes `questions` / `leaderboard` / `predict` / `resolve` tools as thin wrappers around the Python engine (venv python + `-E -X utf8`; see the header comment in the extension file).

Fork note: `shell/pi` is based on [earendil-works/pi](https://github.com/earendil-works/pi) (MIT License © Mario Zechner) v0.84.1, modified in three places in `package.json` (name/bin/piConfig) plus one extension-loader change (`fsCache:false`). See `shell/BRANDING.md`.

## Docs

| File | Purpose |
|---|---|
| `docs/forecastbench-submission-survey.md` | Survey of official ForecastBench submission channels |
| `shell/BRANDING.md` | pi fork rebrand checklist & upstream-sync procedure |

## License

MIT. The `shell/pi` subdirectory follows its upstream MIT license (© Mario Zechner); the notice is kept in `shell/pi/LICENSE`.
