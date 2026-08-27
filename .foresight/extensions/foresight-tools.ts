// .foresight/extensions/foresight-tools.ts
// Foresight（pi fork @foresight/foresight-agent，configDir=.foresight）工具扩展。
//
// 只读工具（随时可用）：questions / question / leaderboard / scoreboard / system /
//   calibration / health / events / logs / probe_quotes / backtest_report
// 写工具（白名单 + 人机确认，YOLO 开启后免确认）：
//   predict（用户显式请求，免确认） / resolve / publish / run_round /
//   schedule_questions / pm_fetch / pm_resolve / crawl_social / backtest /
//   compare_backtest / fb_dry_run
// YOLO：/yolo on|off 或启动参数 foresight --yolo；默认关闭，/reload、重启或新会话回退。
//
// 用法：启动 foresight 后 /reload 加载（loader 自动发现 cwd/.foresight/extensions/*.ts）。
//
// ===== 类型 import 约定（rebrand 后）=====
// 保持上游包名 @earendil-works/pi-coding-agent，不要改成 @foresight/foresight-agent：
//   - import type 会被 jiti 在加载时擦除，运行时零依赖；
//   - 加载器的别名表（core/extensions/loader.ts）只注册了 @earendil-works/pi-* 与
//     @mariozechner/pi-* 两个上游名字，fork 只改了 package.json 的 name/bin/piConfig；
//   - 类型检查经 .foresight/extensions/tsconfig.json 的 paths 解析。
//
// ===== Python 调用链约定 =====
//   - predictor 包装在项目 .venv（uv 管理，.venv/Scripts/python.exe，Python 3.13）；
//   - 一律带 python -E -X utf8：-E 忽略外部 PYTHONPATH 污染，-X utf8 防 Windows cp936 乱码；
//   - Python 进程 cwd 固定在项目根（Settings().db_path、.env 都是相对 cwd 解析）；
//   - 只读工具经 runReadJson 把参数作为 sys.argv[1] JSON 传入，不把用户输入拼进 -c 代码（防注入）。
//
// ===== 桥契约（CC 评审 §1.2 / §5.3 修复）=====
// 扩展桥存在三条通道 + 一条历史无契约通道（「第三条通道」）：
//   - 通道 A：predict → scripts/predict_cli.py（引擎公开入口，单行 JSON 契约）；
//   - 通道 B：leaderboard → runReadJson 内联 Python（只读）；
//   - 通道 C：resolve → scripts/resolve.py（引擎公开入口）。
//   - 第三条通道（§1.2 定性）：9 个只读工具（questions/question/scoreboard/system/
//     calibration/health/events/logs 等）经 runReadJson 内联 Python 直调
//     predictor.data.storage / predictor.ops.* 内部 API，绕过引擎公开入口契约——
//     引擎方法改名/签名变化会静默破坏工具，且历史上零测试覆盖（§5.3）。
// 本文件已把全部通道收敛为单一契约常量 BRIDGE_CONTRACT（通道清单 + 参数白名单 +
// 超时 + 内联体 pyApi 白名单），并实施三层运行时校验：
//   - 注册门 contractTool()：工具名/参数名/必填标记与契约逐项比对，失配抛错拒载；
//   - 内联体门 readBody()：只读工具 Python 代码 import 白名单 + _out 信封强制（加载期）；
//   - 完备门 assertBridgeContract()：契约登记 ⇔ 实际注册双向一致，加载末校验。
// 结构性收尾（另开任务，需动 src/）：predictor.cli 补 read-only 子命令，9 个只读工具改薄调用。
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";

// 扩展文件位于 <项目根>/.foresight/extensions/（或全局 agentDir/extensions/），
// 项目根 = 文件所在目录上两级。
const EXTENSION_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(EXTENSION_DIR, "..", "..");

// YOLO 运行时状态：只由「用户输入 /yolo 命令」或「--yolo 启动 flag 在 session_start 应用」修改。
// /reload、/new、重启进程会回到 session_start 重新按 flag 决定；聊天历史里的旧通知不代表当前状态。
let yolo = false;

// 写操作输出上限：轮次类脚本 stdout 可能数百行，只保留首尾，防止撑爆上下文。
const OUTPUT_CLIP = 20_000;

// 写工具并发纪律：DuckDB 单写者，所有写工具串行执行。
const SEQUENTIAL = "sequential" as const;

const YOLO_SYSTEM_BLOCK = `\n\n# YOLO MODE（本回合生效）\n用户已通过 /yolo on 或启动参数 --yolo 显式开启 YOLO 模式。\n本回合起，SYSTEM.md 中的行为边界（预测 agent 不是 coding agent、不得修改扩展/脚本/数据库、不得绕过 predict 工具跑探索性命令）暂停适用：你可以自主使用内置 bash/edit/write 工具完成任务，也可以自主运行白名单运维工具（run_round/pm_*/crawl_social/backtest 等）且不再逐次确认。\n仍硬性保留：合规红线（政治敏感/涉财物竞猜/无牌荐股/个人隐私）、公开战绩真实性（绝不用历史事件回填战绩）、概率只原样展示工具返回值。\nYOLO 是信任模式：改代码/数据前建议先 git 留回滚点；完成运营任务后建议让用户 /yolo off。写工具的实际权限仍以扩展运行时状态为准。`;

function firstExisting(...candidates: string[]): string | undefined {
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return undefined;
}

/** 项目 .venv 的 python（Windows 下 venv 解释器在 .venv/Scripts/python.exe）。 */
function venvPython(cwd: string): string {
  return (
    firstExisting(
      path.join(cwd, ".venv", "Scripts", "python.exe"),
      path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe"),
    ) ?? "python"
  );
}

/**
 * Python 进程工作目录：优先 ctx.cwd（若是项目根），否则回退扩展所在的项目根。
 * db_path / .env 都相对进程 cwd 解析，必须落在项目根才能跑对库。
 */
function pythonCwd(cwd: string): string {
  if (fs.existsSync(path.join(cwd, "scripts", "predict_cli.py"))) return cwd;
  if (fs.existsSync(path.join(PROJECT_ROOT, "scripts", "predict_cli.py"))) return PROJECT_ROOT;
  return cwd;
}

/** 只展示首尾：长脚本输出（run_round/crawl 等）不能整段进模型上下文。 */
function clipText(s: string, max = OUTPUT_CLIP): string {
  if (s.length <= max) return s;
  const head = 800;
  return `${s.slice(0, head)}\n...[已截断：原始 ${s.length} 字符，仅保留首尾]...\n${s.slice(-(max - head))}`;
}

/** 统一封装：venv python + -E（防 PYTHONPATH 污染）+ -X utf8（防 cp936 乱码）+ 项目根 cwd。 */
async function runPython(
  pi: ExtensionAPI,
  cwd: string,
  args: string[],
  timeout: number,
  signal?: AbortSignal,
): Promise<{ stdout: string; stderr: string }> {
  const r = await pi.exec(venvPython(cwd), ["-E", "-X", "utf8", ...args], {
    signal,
    timeout,
    cwd: pythonCwd(cwd),
  });
  if (r.code !== 0 || r.killed) {
    throw new Error(
      `python 调用失败 (code ${r.code}${r.killed ? ", killed/timeout" : ""})\nstdout: ${clipText(r.stdout)}\nstderr: ${clipText(r.stderr)}`,
    );
  }
  return { stdout: r.stdout, stderr: r.stderr };
}

/**
 * 只读 JSON 工具统一入口：代码必须定义 _out，payload 经 sys.argv[1] 以 JSON 传入。
 * 不把用户输入直接拼进 -c 代码（防注入）；datetime 用 default=str 序列化。
 */
async function runReadJson(
  pi: ExtensionAPI,
  cwd: string,
  body: string,
  payload: Record<string, unknown>,
  timeout: number,
  signal?: AbortSignal,
): Promise<string> {
  const indented = body
    .split("\n")
    .map((line) => `    ${line}`)
    .join("\n");
  const code = [
    "import sys, os, json",
    "sys.path.insert(0, os.path.join(os.getcwd(), 'src'))",
    "try:",
    indented,
    "except Exception as _e:",
    "    _out = {'error': f'{type(_e).__name__}: {str(_e)[:800]}'}",
    "print(json.dumps(_out, ensure_ascii=False, default=str))",
  ].join("\n");
  const { stdout } = await runPython(pi, cwd, ["-c", code, JSON.stringify(payload)], timeout, signal);
  return stdout;
}

/**
 * 写操作闸门：YOLO 开 → 放行；YOLO 关 → 交互模式弹确认框，-p/无 UI 一律拒绝。
 * 返回 undefined 表示允许，否则返回拒绝说明（由调用方作为工具结果原样返回）。
 */
async function writeGate(
  ctx: { hasUI: boolean; ui: { confirm: (title: string, message: string) => Promise<boolean> } },
  operation: string,
  detail: string,
): Promise<string | undefined> {
  if (yolo) return undefined;
  if (!ctx.hasUI) {
    return (
      `已拒绝写操作「${operation}」：当前 YOLO 关闭且无交互 UI（-p/headless）。` +
      `未执行任何命令。需要无人值守执行请用 foresight --yolo 启动，或在 TUI 里 /yolo on。`
    );
  }
  try {
    const ok = await ctx.ui.confirm(
      `foresight 写操作确认：${operation}`,
      `${detail}\n\nYOLO 关闭状态，本次执行需要你确认。`,
    );
    if (!ok) return `用户取消了写操作「${operation}」，未执行。`;
  } catch {
    return `确认对话框不可用，已拒绝写操作「${operation}」，未执行。`;
  }
  return undefined;
}

/** 直接跑脚本（写路径）：通过写闸门后执行，返回截断后的机器可读结果。 */
async function runWriteScript(
  pi: ExtensionAPI,
  ctx: {
    hasUI: boolean;
    ui: { confirm: (title: string, message: string) => Promise<boolean> };
    cwd: string;
  },
  operation: string,
  detail: string,
  args: string[],
  timeout: number,
  signal?: AbortSignal,
): Promise<string> {
  const refused = await writeGate(ctx, operation, detail);
  if (refused) return refused;
  const r = await pi.exec(venvPython(ctx.cwd), ["-E", "-X", "utf8", ...args], {
    signal,
    timeout,
    cwd: pythonCwd(ctx.cwd),
  });
  if (r.code !== 0 || r.killed) {
    throw new Error(
      [
        `脚本执行失败 (exit=${r.code}${r.killed ? ", killed(timeout/abort)" : ""})`,
        "--- stdout ---",
        clipText(r.stdout),
        "--- stderr ---",
        clipText(r.stderr),
      ].join("\n"),
    );
  }
  return [
    `exit=${r.code}${r.killed ? " killed(timeout/abort)" : ""}`,
    "--- stdout ---",
    clipText(r.stdout),
    "--- stderr ---",
    clipText(r.stderr),
  ].join("\n");
}

// predict 工具超时必须 ≥ 引擎最坏时长（CC 评审 §4.2：工具层超时不得早于引擎内部完成，
// 否则 python 被 kill 后 agent 盲目重试 → TUI 挂起，STATUS 曾记「predict 挂起 17 分钟」）。
// 引擎最坏时长计算依据（src/predictor/llm/client.py + websearch_predictor.py）：
//   - 单次采样最坏 = responses API timeout 120s × 3 次尝试（max_retries=2）+ 指数退避 1s+2s
//     ≈ 363s；
//   - n_samples=2 采样已改 asyncio.gather 并发（2026-08-27）→ 采样阶段最坏 ≈ 363s（不再 ×2）；
//   - 加序列拉取（历史基线 fetch_series_map，实测 ~20s）→ 并发后引擎最坏 ≈ 6.4 分钟；
//   - 并发修复前串行最坏 ≈ 2×363 + 20 ≈ 12.5 分钟（180s 超时必然先触发，挂起实锤来源）。
// 取值 15 分钟：对并发最坏 ≈2.3× 余量，且完整覆盖串行最坏（防回退兜底）。
const PREDICT_TOOL_TIMEOUT_MS = 900_000; // 15 分钟 ≥ 引擎最坏时长

// ---------------------------------------------------------------------------
// 桥契约（CC 评审 §1.2 / §5.3）：单一常量，单一事实源。
// 任何工具通道（含第三条通道的全部内联只读体）必须先在此登记，否则加载即失败。
// ---------------------------------------------------------------------------

interface BridgeChannelContract {
  /** 工具名（必须与 contractTool 注册名一致） */
  name: string;
  /** 通道性质 */
  kind: "read" | "write";
  /** 后端：inline_json=TS 内联 Python 经 runReadJson（第三条通道本体，受 pyApi 白名单约束）；
   *  script=scripts/*.py 引擎公开入口契约；fs_json=纯文件读取不经 Python */
  backend: "inline_json" | "script" | "fs_json";
  /** backend=script 的脚本路径（backend=fs_json 时为数据文件说明） */
  entry?: string;
  /** 参数白名单（与 TypeBox schema 一一对应，注册期强校验） */
  params: ReadonlyArray<{ name: string; required: boolean }>;
  /** 静态超时 ms；缺省且无 dynamicTimeout 属契约缺陷（加载期报错） */
  timeoutMs?: number;
  /** 动态超时公式说明（crawl_social） */
  timeoutNote?: string;
  /** 动态超时参数（n 组合 × perComboMs + overheadMs） */
  dynamicTimeout?: { perComboMs: number; overheadMs: number };
  /** backend=inline_json 允许引用的 Python API 白名单（import 前缀匹配，加载期强校验） */
  pyApi?: readonly string[];
}

interface BridgeContract {
  version: number;
  /** Python 进程拉起契约（runPython / runWriteScript 两个唯一 spawn 点） */
  python: {
    executable: string;
    envFlags: readonly string[];
    cwdRule: string;
    argvJson: string;
  };
  /** 错误/输出语义契约 */
  errorSemantics: {
    nonzeroExit: string;
    readEnvelope: string;
    clipBytes: number;
    confirmGate: string;
    sequential: string;
  };
  /** 通道清单（22 条 = 11 只读 + 11 写；第三条通道的 9 个 inline_json 已全部契约化） */
  channels: readonly BridgeChannelContract[];
}

const BRIDGE_CONTRACT: BridgeContract = {
  version: 1,
  python: {
    executable: "项目 .venv（.venv/Scripts/python.exe，经 venvPython 解析）",
    envFlags: ["-E", "-X", "utf8"],
    cwdRule: "pythonCwd()：ctx.cwd 或 PROJECT_ROOT（以 scripts/predict_cli.py 存在为准）",
    argvJson: "只读工具参数经 sys.argv[1] 单行 JSON 传入（防注入）",
  },
  errorSemantics: {
    nonzeroExit: "throw：exit != 0 或 killed → Error（含 stdout/stderr 首尾截断）",
    readEnvelope: "inline_json 体必须产出 _out，输出单行 JSON（default=str），异常缺省 {'error': ...}",
    clipBytes: 20_000,
    confirmGate: "写工具经 writeGate：YOLO 开放行 / 关且无 UI 拒绝 / 关且有 UI 弹确认",
    sequential: "写工具 executionMode=sequential（DuckDB 单写者）",
  },
  channels: [
    {
      name: "questions",
      kind: "read",
      backend: "inline_json",
      params: [
        { name: "status", required: false },
        { name: "q", required: false },
        { name: "limit", required: false },
      ],
      timeoutMs: 60_000,
      pyApi: ["predictor.config.Settings", "predictor.data.storage.Storage"],
    },
    {
      name: "question",
      kind: "read",
      backend: "inline_json",
      params: [{ name: "qid", required: true }],
      timeoutMs: 60_000,
      pyApi: ["predictor.config.Settings", "predictor.data.storage.Storage"],
    },
    {
      name: "leaderboard",
      kind: "read",
      backend: "inline_json",
      params: [],
      timeoutMs: 60_000,
      pyApi: ["predictor.config.Settings", "predictor.data.storage.Storage"],
    },
    {
      name: "scoreboard",
      kind: "read",
      backend: "inline_json",
      params: [],
      timeoutMs: 60_000,
      pyApi: ["predictor.config.Settings", "predictor.data.storage.Storage"],
    },
    {
      name: "system",
      kind: "read",
      backend: "inline_json",
      params: [],
      timeoutMs: 60_000,
      pyApi: ["predictor.config.Settings", "predictor.data.storage.Storage"],
    },
    {
      name: "calibration",
      kind: "read",
      backend: "inline_json",
      params: [{ name: "limit", required: false }],
      timeoutMs: 60_000,
      pyApi: ["predictor.config.Settings", "predictor.data.storage.Storage"],
    },
    {
      name: "health",
      kind: "read",
      backend: "inline_json",
      params: [{ name: "refresh", required: false }],
      timeoutMs: 150_000,
      pyApi: [
        "predictor.config.Settings",
        "predictor.data.storage.Storage",
        "predictor.ops.facts.build_facts",
        "predictor.ops.health.assess",
        "predictor.ops.probes.get_probes",
        "predictor.ops.probes.refresh_probes",
        "datetime.datetime",
      ],
    },
    {
      name: "events",
      kind: "read",
      backend: "inline_json",
      params: [
        { name: "types", required: false },
        { name: "limit", required: false },
        { name: "before_id", required: false },
      ],
      timeoutMs: 60_000,
      pyApi: ["predictor.config.Settings", "predictor.data.storage.Storage"],
    },
    {
      name: "logs",
      kind: "read",
      backend: "inline_json",
      params: [
        { name: "name", required: true },
        { name: "lines", required: false },
      ],
      timeoutMs: 60_000,
      pyApi: ["predictor.config.Settings", "pathlib.Path"],
    },
    {
      name: "probe_quotes",
      kind: "read",
      backend: "script",
      entry: "scripts/probe_quotes.py",
      params: [],
      timeoutMs: 90_000,
    },
    {
      name: "backtest_report",
      kind: "read",
      backend: "fs_json",
      entry: "data/backtest_baseline.json、data/compare_backtest.json、data/latest_scoreboard.json（fs 直读，不经 Python）",
      params: [],
    },
    {
      name: "predict",
      kind: "write",
      backend: "script",
      entry: "scripts/predict_cli.py",
      params: [
        { name: "question", required: true },
        { name: "closes", required: false },
      ],
      timeoutMs: PREDICT_TOOL_TIMEOUT_MS,
    },
    {
      name: "resolve",
      kind: "write",
      backend: "script",
      entry: "scripts/resolve.py",
      params: [
        { name: "qid", required: true },
        { name: "outcome", required: true },
        { name: "source", required: true },
      ],
      timeoutMs: 60_000,
    },
    {
      name: "publish",
      kind: "write",
      backend: "script",
      entry: "scripts/predict_cli.py --publish",
      params: [{ name: "qid", required: true }],
      timeoutMs: 120_000,
    },
    {
      name: "run_round",
      kind: "write",
      backend: "script",
      entry: "scripts/daily.py | scripts/evolve.py",
      params: [{ name: "round", required: true }],
      timeoutMs: 7_200_000,
    },
    {
      name: "schedule_questions",
      kind: "write",
      backend: "script",
      entry: "scripts/schedule_questions.py",
      params: [{ name: "week", required: false }],
      timeoutMs: 300_000,
    },
    {
      name: "pm_fetch",
      kind: "write",
      backend: "script",
      entry: "scripts/pm_fetch.py",
      params: [
        { name: "dry_run", required: false },
        { name: "public", required: false },
        { name: "max_events", required: false },
        { name: "per_tier", required: false },
        { name: "min_volume", required: false },
        { name: "no_translate", required: false },
      ],
      timeoutMs: 900_000,
    },
    {
      name: "pm_resolve",
      kind: "write",
      backend: "script",
      entry: "scripts/pm_resolve.py",
      params: [{ name: "dry_run", required: false }],
      timeoutMs: 300_000,
    },
    {
      name: "crawl_social",
      kind: "write",
      backend: "script",
      entry: "scripts/crawl_social.py",
      params: [
        { name: "keywords", required: true },
        { name: "platforms", required: true },
        { name: "limit", required: false },
      ],
      timeoutNote: "动态：n 组合 × perComboMs + overheadMs（每组合最长 30 分钟）",
      dynamicTimeout: { perComboMs: 1_800_000, overheadMs: 120_000 },
    },
    {
      name: "backtest",
      kind: "write",
      backend: "script",
      entry: "scripts/backtest.py",
      params: [{ name: "sample", required: false }],
      timeoutMs: 1_800_000,
    },
    {
      name: "compare_backtest",
      kind: "write",
      backend: "script",
      entry: "scripts/compare_backtest.py",
      params: [{ name: "sample", required: false }],
      timeoutMs: 1_800_000,
    },
    {
      name: "fb_dry_run",
      kind: "write",
      backend: "script",
      entry: "scripts/fb_submit.py",
      params: [{ name: "limit", required: false }],
      timeoutMs: 1_800_000,
    },
  ],
};

// ---------------------------------------------------------------------------
// 契约运行时：注册门 / 内联体门 / 完备门（三层校验，违反即抛错拒载）
// ---------------------------------------------------------------------------

const REGISTERED_TOOLS = new Set<string>();

function findChannel(name: string): BridgeChannelContract | undefined {
  return BRIDGE_CONTRACT.channels.find((c) => c.name === name);
}

/** 从契约取静态超时（单一事实源；未登记/未定义即抛错）。 */
function channelTimeout(name: string): number {
  const c = findChannel(name);
  if (!c || c.timeoutMs === undefined) {
    throw new Error(`桥契约破坏：工具「${name}」未登记静态超时（BRIDGE_CONTRACT.channels[].timeoutMs）`);
  }
  return c.timeoutMs;
}

/** 从契约取动态超时参数（crawl_social）。 */
function channelDynamicTimeout(name: string): { perComboMs: number; overheadMs: number } {
  const c = findChannel(name);
  if (!c?.dynamicTimeout) {
    throw new Error(`桥契约破坏：工具「${name}」未登记动态超时（dynamicTimeout）`);
  }
  return c.dynamicTimeout;
}

type AnyToolDefinition = ReturnType<typeof defineTool>;

/**
 * 注册门：所有工具必须经此注册。契约化要求——无契约通道不得注册：
 *   - 工具名必须已登记 BRIDGE_CONTRACT.channels；
 *   - 参数白名单（TypeBox schema properties）必须与契约 params 逐项一致；
 *   - 必填参数标记（TypeBox required）必须与契约一致。
 */
function contractTool(pi: ExtensionAPI, def: AnyToolDefinition): void {
  const c = findChannel(def.name);
  if (!c) {
    throw new Error(`桥契约破坏：工具「${def.name}」未登记在 BRIDGE_CONTRACT.channels（无契约通道不得注册）`);
  }
  const schema = def.parameters as unknown as {
    properties?: Record<string, unknown>;
    required?: readonly string[];
  };
  const actual = schema.properties ? Object.keys(schema.properties) : [];
  const expected = c.params.map((p) => p.name);
  if (actual.length !== expected.length || expected.some((n) => !actual.includes(n))) {
    throw new Error(
      `桥契约破坏：工具「${def.name}」参数白名单不一致（契约=[${expected.join(",")}]，实际=[${actual.join(",")}]）`,
    );
  }
  const actualRequired = schema.required ?? [];
  const expectedRequired = c.params.filter((p) => p.required).map((p) => p.name);
  if (
    actualRequired.length !== expectedRequired.length ||
    expectedRequired.some((n) => !actualRequired.includes(n))
  ) {
    throw new Error(
      `桥契约破坏：工具「${def.name}」必填参数不一致（契约=[${expectedRequired.join(",")}]，实际=[${actualRequired.join(",")}]）`,
    );
  }
  pi.registerTool(def);
  REGISTERED_TOOLS.add(def.name);
}

/**
 * 内联体门（第三条通道收紧）：只读工具内联 Python 代码必须经此构建。
 *   - 通道必须登记为 backend=inline_json 且带 pyApi 白名单；
 *   - 体内所有 import 必须落在 pyApi 白名单（前缀匹配）——直调引擎内部 API 先登记契约；
 *   - 体内必须产出 _out（readEnvelope 错误语义契约）。
 * 违反即加载期抛错，扩展整体拒载（fail-fast，宁可不可用也不静默绕过契约）。
 */
function readBody(channel: string, lines: string[]): string {
  const c = findChannel(channel);
  if (!c) {
    throw new Error(`桥契约破坏：只读通道「${channel}」未登记在 BRIDGE_CONTRACT.channels`);
  }
  if (c.backend !== "inline_json") {
    throw new Error(`桥契约破坏：「${channel}」backend=${c.backend}，不允许经 readBody 内联 Python`);
  }
  if (!c.pyApi?.length) {
    throw new Error(`桥契约破坏：「${channel}」inline_json 缺 pyApi 白名单`);
  }
  const body = lines.join("\n");
  const importRe = /^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))/gm;
  let m: RegExpExecArray | null;
  while ((m = importRe.exec(body)) !== null) {
    const mod = m[1] ?? m[2];
    if (!c.pyApi.some((a) => a === mod || a.startsWith(`${mod}.`))) {
      throw new Error(`桥契约破坏：只读工具「${channel}」内联体 import ${mod} 不在 pyApi 白名单`);
    }
  }
  if (!body.includes("_out")) {
    throw new Error(`桥契约破坏：只读工具「${channel}」内联体未产 _out（readEnvelope 契约）`);
  }
  return body;
}

/** 完备门：契约登记 ⇔ 实际注册双向一致，加载末校验。 */
function assertBridgeContract(): void {
  for (const c of BRIDGE_CONTRACT.channels) {
    if (!REGISTERED_TOOLS.has(c.name)) {
      throw new Error(`桥契约破坏：${c.name} 已登记契约但未注册工具`);
    }
    if (c.backend === "script" && !c.entry) {
      throw new Error(`桥契约破坏：${c.name} backend=script 缺 entry`);
    }
    if (c.backend === "inline_json" && !c.pyApi?.length) {
      throw new Error(`桥契约破坏：${c.name} backend=inline_json 缺 pyApi 白名单`);
    }
    if (c.backend !== "fs_json" && c.timeoutMs === undefined && !c.dynamicTimeout) {
      throw new Error(`桥契约破坏：${c.name} 既无静态超时也无动态超时`);
    }
  }
  for (const name of REGISTERED_TOOLS) {
    if (!findChannel(name)) {
      throw new Error(`桥契约破坏：工具 ${name} 已注册但未登记契约`);
    }
  }
}

// ---------------------------------------------------------------------------
// 只读工具内联体（模块级构建 → 加载期过 readBody 白名单门，契约内联体清单见 BRIDGE_CONTRACT）
// ---------------------------------------------------------------------------

const READ_BODY_QUESTIONS = readBody("questions", [
  "from predictor.config import Settings",
  "from predictor.data.storage import Storage",
  "p = json.loads(sys.argv[1])",
  "s = Storage(Settings().db_path, read_only=True)",
  "st = p.get('status') or 'open'",
  "rows = s.list_questions_all(status=None if st == 'all' else st, q=p.get('q') or None)",
  "limit = max(1, min(int(p.get('limit', 20)), 100))",
  "_out = {'status': st, 'count': len(rows), 'items': rows[:limit]}",
]);

const READ_BODY_QUESTION = readBody("question", [
  "from predictor.config import Settings",
  "from predictor.data.storage import Storage",
  "p = json.loads(sys.argv[1])",
  "s = Storage(Settings().db_path, read_only=True)",
  "d = s.get_question_detail(int(p['qid']))",
  "if d is None:",
  "    _out = {'error': 'not found', 'qid': int(p['qid'])}",
  "else:",
  "    d['documents'] = s.list_question_documents(int(p['qid']))[:100]",
  "    _out = d",
]);

const READ_BODY_LEADERBOARD = readBody("leaderboard", [
  "from predictor.config import Settings",
  "from predictor.data.storage import Storage",
  "s = Storage(Settings().db_path, read_only=True)",
  "_out = s.brier_by_horizon_bucket()",
]);

const READ_BODY_SCOREBOARD = readBody("scoreboard", [
  "from predictor.config import Settings",
  "from predictor.data.storage import Storage",
  "s = Storage(Settings().db_path, read_only=True)",
  "_out = {'summary': s.scoreboard_summary(), 'resolved': s.list_resolved_public()[:100]}",
]);

const READ_BODY_SYSTEM = readBody("system", [
  "from predictor.config import Settings",
  "from predictor.data.storage import Storage",
  "s = Storage(Settings().db_path, read_only=True)",
  "_out = {",
  "    'levers': s.list_levers(),",
  "    'lessons': s.list_lessons()[:50],",
  "    'evolution_log': s.list_evolution_log()[:50],",
  "    'model_stats': s.model_stats(),",
  "    'arm_stats': s.arm_stats(),",
  "}",
]);

const READ_BODY_CALIBRATION = readBody("calibration", [
  "from predictor.config import Settings",
  "from predictor.data.storage import Storage",
  "p = json.loads(sys.argv[1])",
  "s = Storage(Settings().db_path, read_only=True)",
  "limit = max(1, min(int(p.get('limit', 200)), 1000))",
  "pairs = [{'probability': x[0], 'outcome': x[1]} for x in s.calibration_pairs()]",
  "_out = {'n': len(pairs), 'items': pairs[-limit:]}",
]);

const READ_BODY_HEALTH = readBody("health", [
  "from datetime import datetime",
  "from predictor.config import Settings",
  "from predictor.data.storage import Storage",
  "from predictor.ops.facts import build_facts",
  "from predictor.ops.health import assess",
  "from predictor.ops.probes import get_probes, refresh_probes",
  "p = json.loads(sys.argv[1])",
  "s = Storage(Settings().db_path, read_only=True)",
  "if p.get('refresh'):",
  "    refresh_probes()",
  "now = datetime.now()",
  "facts = build_facts(s, now)",
  "facts['probes'] = get_probes()",
  "_out = assess(facts, now)",
]);

const READ_BODY_EVENTS = readBody("events", [
  "from predictor.config import Settings",
  "from predictor.data.storage import Storage",
  "p = json.loads(sys.argv[1])",
  "s = Storage(Settings().db_path, read_only=True)",
  "types = [str(t) for t in (p.get('types') or []) if isinstance(t, str)][:20]",
  "limit = max(1, min(int(p.get('limit', 100)), 500))",
  "before = int(p['before_id']) if p.get('before_id') is not None else None",
  "_out = {'items': s.list_events(types=types or None, limit=limit, before_id=before)}",
]);

const READ_BODY_LOGS = readBody("logs", [
  "from pathlib import Path",
  "from predictor.config import Settings",
  "p = json.loads(sys.argv[1])",
  "name = p['name']",
  "if name not in ('daily', 'evolve'):",
  "    _out = {'error': 'unknown log name'}",
  "else:",
  "    log_path = Path(Settings().db_path).parent / f'{name}.log'",
  "    try:",
  "        lines = log_path.read_text(encoding='gbk', errors='replace').splitlines()[-int(p.get('lines', 100)):]",
  "        _out = {'name': name, 'count': len(lines), 'lines': lines}",
  "    except OSError:",
  "        _out = {'name': name, 'count': 0, 'lines': [], 'error': 'log file not found'}",
]);

export default function (pi: ExtensionAPI) {
  pi.registerFlag("yolo", {
    description: "启动即进入 YOLO 模式：写操作免确认，并解除预测 agent 的 coding 边界",
    type: "boolean",
    default: false,
  });

  // flags 在扩展加载期不可读（官方约定），session_start 时应用一次。
  pi.on("session_start", (_event, ctx) => {
    const flag = pi.getFlag("yolo");
    yolo = flag === true || flag === "true";
    if (yolo) {
      ctx.ui.notify("YOLO 模式已开启（--yolo 启动参数）", "warning");
    }
  });

  // 每一轮 agent 启动时，把当前 YOLO 状态注入 system prompt，模型无需猜测。
  pi.on("before_agent_start", (event) => {
    if (!yolo) return undefined;
    return { systemPrompt: `${event.systemPrompt}${YOLO_SYSTEM_BLOCK}` };
  });

  // YOLO 关闭时硬拦截内置 bash/edit/write：写闸门不能只包扩展工具，否则模型可用
  // 内置 bash 直接跑 scripts/*.py 绕过确认。read/grep/find 保留给最小只读诊断。
  pi.on("tool_call", (event) => {
    if (yolo) return undefined;
    if (event.toolName === "bash" || event.toolName === "edit" || event.toolName === "write") {
      return {
        block: true,
        reason:
          "YOLO 关闭：预测 agent 边界内禁止 bash/edit/write。状态查询请用只读工具；" +
          "预测请用 predict；运维/写库/改代码请先由用户在 TUI /yolo on，或重启时加 --yolo。",
      };
    }
    return undefined;
  });

  pi.registerCommand("yolo", {
    description: "YOLO 模式开关：/yolo on|off|status（只有用户能输入斜杠命令）",
    handler: async (args, ctx) => {
      const arg = args.trim().toLowerCase();
      if (arg === "on") {
        yolo = true;
        ctx.ui.notify("YOLO 已开启：本会话写操作免确认，coding 边界解除", "warning");
        pi.sendUserMessage(
          "[系统] 用户已通过 /yolo on 开启 YOLO 模式。本会话内（运行时状态为准）：" +
            "写工具免确认，SYSTEM.md 行为边界暂停适用，可自主 coding/运维；" +
            "硬规则（合规红线、公开战绩真实性、概率原样展示）仍有效。" +
            "/yolo off、/reload（未带 --yolo）或重启后恢复默认边界。",
          { deliverAs: "followUp" },
        );
      } else if (arg === "off") {
        yolo = false;
        ctx.ui.notify("YOLO 已关闭：恢复预测 agent 边界与逐次确认", "info");
        pi.sendUserMessage(
          "[系统] 用户已通过 /yolo off 关闭 YOLO 模式。立即恢复 SYSTEM.md 默认行为边界：" +
            "写工具恢复逐次弹框确认，coding 边界重新生效。",
          { deliverAs: "followUp" },
        );
      } else if (arg === "" || arg === "status") {
        ctx.ui.notify(`YOLO 当前状态：${yolo ? "开启" : "关闭"}`, "info");
      } else {
        ctx.ui.notify("用法：/yolo on|off|status", "warning");
      }
    },
  });

  // ---------------------------------------------------------------------------
  // 只读工具（全部经 contractTool 注册门；inline_json 体已在模块级过 readBody 白名单门）
  // ---------------------------------------------------------------------------

  contractTool(
    pi,
    defineTool({
      name: "questions",
      label: "Questions",
      description:
        "列出当前题目（只读）：默认 open 进行中；可选 pending/resolved/all，支持标题关键词搜索 q。",
      parameters: Type.Object({
        status: Type.Optional(
          Type.Union([
            Type.Literal("open"),
            Type.Literal("pending"),
            Type.Literal("resolved"),
            Type.Literal("all"),
          ]),
        ),
        q: Type.Optional(Type.String({ maxLength: 200, description: "标题关键词（ILIKE）" })),
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100, default: 20 })),
      }),
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const status = params.status ?? "open";
        const payload = { status, q: params.q ?? "", limit: params.limit ?? 20 };
        return {
          content: [
            {
              type: "text",
              text: await runReadJson(pi, ctx.cwd, READ_BODY_QUESTIONS, payload, channelTimeout("questions"), signal),
            },
          ],
          details: {},
        };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "question",
      label: "Question Detail",
      description: "单题详情（只读）：含判定口径 resolution_spec、最新预测、证据文档列表。",
      parameters: Type.Object({
        qid: Type.Integer({ minimum: 1, description: "题号（questions.id）" }),
      }),
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const payload = { qid: params.qid };
        return {
          content: [
            {
              type: "text",
              text: await runReadJson(pi, ctx.cwd, READ_BODY_QUESTION, payload, channelTimeout("question"), signal),
            },
          ],
          details: {},
        };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "leaderboard",
      label: "Leaderboard",
      description: "查看 Foresight 分桶 Brier 战绩（只读，只统计已揭晓并计分的公开题）",
      parameters: Type.Object({}),
      async execute(_toolCallId, _params, signal, _onUpdate, ctx) {
        return {
          content: [
            {
              type: "text",
              text: await runReadJson(pi, ctx.cwd, READ_BODY_LEADERBOARD, {}, channelTimeout("leaderboard"), signal),
            },
          ],
          details: {},
        };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "scoreboard",
      label: "Scoreboard",
      description: "公开战绩（只读）：汇总（题数/Brier/分桶）+ 已揭晓公开榜（最多 100 条）",
      parameters: Type.Object({}),
      async execute(_toolCallId, _params, signal, _onUpdate, ctx) {
        return {
          content: [
            {
              type: "text",
              text: await runReadJson(pi, ctx.cwd, READ_BODY_SCOREBOARD, {}, channelTimeout("scoreboard"), signal),
            },
          ],
          details: {},
        };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "system",
      label: "System Panel",
      description: "系统面板（只读）：levers / lessons / 进化日志 / model_stats / arm_stats（lessons 与进化日志截断最近 50 条）",
      parameters: Type.Object({}),
      async execute(_toolCallId, _params, signal, _onUpdate, ctx) {
        return {
          content: [
            {
              type: "text",
              text: await runReadJson(pi, ctx.cwd, READ_BODY_SYSTEM, {}, channelTimeout("system"), signal),
            },
          ],
          details: {},
        };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "calibration",
      label: "Calibration",
      description: "校准数据（只读）：已揭晓题最近预测的 (probability, outcome) 对，最近 N 条",
      parameters: Type.Object({
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 1000, default: 200 })),
      }),
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const payload = { limit: params.limit ?? 200 };
        return {
          content: [
            {
              type: "text",
              text: await runReadJson(pi, ctx.cwd, READ_BODY_CALIBRATION, payload, channelTimeout("calibration"), signal),
            },
          ],
          details: {},
        };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "health",
      label: "Health Check",
      description:
        "健康判定（只读）：轮次/积压/风暴/锁/行情源/LLM/任务计划器。refresh=true 时才触发真实外部探测（约 1 分钟，不写库）。",
      parameters: Type.Object({
        refresh: Type.Optional(Type.Boolean({ description: "是否触发真实探测；缺省只读缓存" })),
      }),
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const payload = { refresh: params.refresh ?? false };
        return {
          content: [
            {
              type: "text",
              text: await runReadJson(pi, ctx.cwd, READ_BODY_HEALTH, payload, channelTimeout("health"), signal),
            },
          ],
          details: {},
        };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "events",
      label: "Event Stream",
      description: "事件流（只读）：evolution_log 按 id 倒序，支持类型过滤与 before_id 游标",
      parameters: Type.Object({
        types: Type.Optional(Type.Array(Type.String(), { description: "事件类型列表，如 ['prediction_skipped']" })),
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 500, default: 100 })),
        before_id: Type.Optional(Type.Integer({ minimum: 1, description: "取 id 更早的事件" })),
      }),
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const payload = {
          types: params.types ?? [],
          limit: params.limit ?? 100,
          before_id: params.before_id ?? null,
        };
        return {
          content: [
            {
              type: "text",
              text: await runReadJson(pi, ctx.cwd, READ_BODY_EVENTS, payload, channelTimeout("events"), signal),
            },
          ],
          details: {},
        };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "logs",
      label: "Tail Logs",
      description: "运维日志尾部（只读）：daily.log 或 evolve.log 最后 N 行（GBK 容错）",
      parameters: Type.Object({
        name: Type.Union([Type.Literal("daily"), Type.Literal("evolve")]),
        lines: Type.Optional(Type.Integer({ minimum: 10, maximum: 500, default: 100 })),
      }),
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const payload = { name: params.name, lines: params.lines ?? 100 };
        return {
          content: [
            {
              type: "text",
              text: await runReadJson(pi, ctx.cwd, READ_BODY_LOGS, payload, channelTimeout("logs"), signal),
            },
          ],
          details: {},
        };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "probe_quotes",
      label: "Probe Quotes",
      description: "行情源连通性探测（只读）：逐端点拉一个报价并打印状态",
      parameters: Type.Object({}),
      async execute(_toolCallId, _params, signal, _onUpdate, ctx) {
        const { stdout } = await runPython(pi, ctx.cwd, ["scripts/probe_quotes.py"], channelTimeout("probe_quotes"), signal);
        return { content: [{ type: "text", text: clipText(stdout, 60_000) }], details: {} };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "backtest_report",
      label: "Backtest Reports",
      description:
        "历史回测/战绩快照（只读）：data/backtest_baseline.json、data/compare_backtest.json、data/latest_scoreboard.json",
      parameters: Type.Object({}),
      async execute() {
        const names = ["backtest_baseline.json", "compare_backtest.json", "latest_scoreboard.json"];
        const items: Record<string, unknown> = {};
        for (const name of names) {
          const p = path.join(PROJECT_ROOT, "data", name);
          try {
            items[name] = JSON.parse(fs.readFileSync(p, "utf8"));
          } catch {
            items[name] = { error: `${name} 不存在或不是合法 JSON` };
          }
        }
        return {
          content: [{ type: "text", text: clipText(JSON.stringify({ items }, null, 2), 60_000) }],
          details: {},
        };
      },
    }),
  );

  // ---------------------------------------------------------------------------
  // 写工具（全部经 contractTool 注册门 + writeGate 人机确认；超时一律从契约取值）
  // ---------------------------------------------------------------------------

  contractTool(
    pi,
    defineTool({
      name: "predict",
      label: "Predict",
      description: "Foresight 预测引擎：对给定事件出概率+依据+报告（建草稿题，输出单行 JSON）",
      parameters: Type.Object({
        question: Type.String({ maxLength: 2000, description: "预测问题，如：美联储9月会加息吗" }),
        closes: Type.Optional(Type.String({ description: "揭晓日期 YYYY-MM-DD，缺省 30 天后" })),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const args = [];
        if (params.closes) args.push("--closes", params.closes);
        args.push("--", params.question);
        const { stdout } = await runPython(
          pi,
          ctx.cwd,
          ["scripts/predict_cli.py", ...args],
          channelTimeout("predict"),
          signal,
        );
        return { content: [{ type: "text", text: stdout }], details: {} };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "resolve",
      label: "Resolve",
      description:
        "揭晓打分：写入 outcome 并计 Brier（人机回路：先查官方结果→调 scripts/resolve.py）。YOLO 关闭时每次弹确认框。",
      parameters: Type.Object({
        qid: Type.Integer({ minimum: 1, description: "题号（questions.id）" }),
        outcome: Type.Boolean({ description: "揭晓结果：true=成立 / false=不成立" }),
        source: Type.String({ minLength: 1, maxLength: 500, description: "判定依据来源（官方数据/链接）" }),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const refused = await writeGate(
          ctx,
          `resolve #${params.qid}`,
          `将执行 scripts/resolve.py：题 #${params.qid} 揭晓为 ${params.outcome ? "成立" : "不成立"}，` +
            `依据 ${params.source}。副作用：写 questions.outcome/brier_score，并重新 fit 校准器。`,
        );
        if (refused) return { content: [{ type: "text", text: refused }], details: {} };
        const cwd = pythonCwd(ctx.cwd);
        const csvPath = path.join(cwd, "data", `resolutions.${params.qid}.${Date.now()}.csv`);
        const source = String(params.source).replace(/[\",\r\n]/g, "");
        const csv = `id,outcome,source\n${params.qid},${params.outcome ? "1" : "0"},${source}`;
        try {
          fs.mkdirSync(path.dirname(csvPath), { recursive: true });
          fs.writeFileSync(csvPath, csv, { encoding: "utf-8" });
          const { stdout } = await runPython(
            pi,
            ctx.cwd,
            ["scripts/resolve.py", "--outcomes", csvPath],
            channelTimeout("resolve"),
            signal,
          );
          return { content: [{ type: "text", text: stdout }], details: {} };
        } finally {
          // 临时 csv 用完即清（失败也不留垃圾）
          try {
            fs.unlinkSync(csvPath);
          } catch {
            /* ignore */
          }
        }
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "publish",
      label: "Publish",
      description:
        "把草稿题转公开（写操作）：qid 为 questions.id。YOLO 关闭时弹确认框；-p 无 YOLO 拒绝。",
      parameters: Type.Object({
        qid: Type.Integer({ minimum: 1, description: "题号（questions.id）" }),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const out = await runWriteScript(
          pi,
          ctx,
          `publish #${params.qid}`,
          `将执行 python scripts/predict_cli.py --publish ${params.qid}。副作用：` +
            `该题 is_public 置为 TRUE（进入公开战绩）。`,
          ["scripts/predict_cli.py", "--publish", String(params.qid)],
          channelTimeout("publish"),
          signal,
        );
        return { content: [{ type: "text", text: out }], details: {} };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "run_round",
      label: "Run Round",
      description:
        "跑运维轮次（写操作，可能耗时最长 1 小时）：daily=每日双轨入口；predict/resolve/all=evolve 预测轮/揭晓轮/全量。YOLO 关闭时弹确认框。",
      parameters: Type.Object({
        round: Type.Union([
          Type.Literal("daily"),
          Type.Literal("predict"),
          Type.Literal("resolve"),
          Type.Literal("all"),
        ]),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const script = params.round === "daily" ? "scripts/daily.py" : "scripts/evolve.py";
        const args = params.round === "daily" ? [] : [params.round];
        const detail =
          params.round === "daily"
            ? "将执行 python scripts/daily.py：补预测 + 到期题人工模板 + 战绩快照。"
            : `将执行 python scripts/evolve.py ${params.round}：预测轮/揭晓轮编排。`;
        const out = await runWriteScript(
          pi,
          ctx,
          `run_round ${params.round}`,
          `${detail}\n副作用：写 DuckDB（题目/预测/揭晓/事件），调用 LLM 与外部行情/新闻源，` +
            `与 daily/evolve 共用 data/evolve.lock。`,
          [script, ...args],
          channelTimeout("run_round"),
          signal,
        );
        return { content: [{ type: "text", text: out }], details: {} };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "schedule_questions",
      label: "Schedule Questions",
      description: "生成当周短周期客观题并入库（写操作）。week 缺省为今天所在周。",
      parameters: Type.Object({
        week: Type.Optional(Type.String({ maxLength: 20, description: "周一日期 YYYY-MM-DD，缺省今天所在周" })),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const args = ["scripts/schedule_questions.py"];
        if (params.week) args.push("--week", params.week);
        const out = await runWriteScript(
          pi,
          ctx,
          "schedule_questions",
          `将执行 python scripts/schedule_questions.py${params.week ? ` --week ${params.week}` : ""}。` +
            `副作用：向 questions 表写入当周短周期题（is_public 按脚本默认）。`,
          args,
          channelTimeout("schedule_questions"),
          signal,
        );
        return { content: [{ type: "text", text: out }], details: {} };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "pm_fetch",
      label: "PM Fetch",
      description:
        "从 Polymarket 拉候选入题池。dry_run=true（缺省）只打印不落库；dry_run=false 才写库并弹确认框。",
      parameters: Type.Object({
        dry_run: Type.Optional(Type.Boolean({ default: true })),
        public: Type.Optional(Type.Boolean({ description: "入库为公开题（默认内部题）" })),
        max_events: Type.Optional(Type.Integer({ minimum: 50, maximum: 1000, default: 300 })),
        per_tier: Type.Optional(Type.Integer({ minimum: 1, maximum: 20, default: 6 })),
        min_volume: Type.Optional(Type.Number({ minimum: 0, default: 1000 })),
        no_translate: Type.Optional(Type.Boolean()),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const args = [
          "scripts/pm_fetch.py",
          "--max-events",
          String(params.max_events ?? 300),
          "--per-tier",
          String(params.per_tier ?? 6),
          "--min-volume",
          String(params.min_volume ?? 1000),
        ];
        if (params.no_translate) args.push("--no-translate");
        if (params.public) args.push("--public");
        const dry = params.dry_run !== false;
        if (dry) {
          args.push("--dry-run");
          const { stdout } = await runPython(pi, ctx.cwd, args, channelTimeout("pm_fetch"), signal);
          return { content: [{ type: "text", text: clipText(stdout) }], details: {} };
        }
        const out = await runWriteScript(
          pi,
          ctx,
          "pm_fetch（写库）",
          `将执行 python scripts/pm_fetch.py（非 dry-run）。副作用：候选题写入 questions 表` +
            `（默认 is_public=False，${params.public ? "本次加 --public 转为公开" : "不转公开"}），并调用 LLM 翻译。`,
          args,
          channelTimeout("pm_fetch"),
          signal,
        );
        return { content: [{ type: "text", text: out }], details: {} };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "pm_resolve",
      label: "PM Resolve",
      description:
        "Polymarket 到期题混合揭晓。dry_run=true（缺省）只打印判定不回填；dry_run=false 才写库并弹确认框。",
      parameters: Type.Object({
        dry_run: Type.Optional(Type.Boolean({ default: true })),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const dry = params.dry_run !== false;
        const args = ["scripts/pm_resolve.py"];
        if (dry) args.push("--dry-run");
        if (dry) {
          const { stdout } = await runPython(pi, ctx.cwd, args, channelTimeout("pm_resolve"), signal);
          return { content: [{ type: "text", text: clipText(stdout) }], details: {} };
        }
        const out = await runWriteScript(
          pi,
          ctx,
          "pm_resolve（写库）",
          "将执行 python scripts/pm_resolve.py（非 dry-run）。副作用：回填 Polymarket 到期题" +
            " outcome/brier_score，并重新 fit 校准器。",
          args,
          channelTimeout("pm_resolve"),
          signal,
        );
        return { content: [{ type: "text", text: out }], details: {} };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "crawl_social",
      label: "Crawl Social",
      description:
        "抓取中文社交平台公开内容（写操作，最长 30 分钟，脚本自带 12h 去重护栏）。platforms 逗号分隔。",
      parameters: Type.Object({
        keywords: Type.String({ minLength: 1, maxLength: 500, description: "关键词，多个用英文逗号分隔" }),
        platforms: Type.String({ minLength: 1, maxLength: 200, description: "weibo/xhs/bilibili/douyin/tieba/zhihu/reddit 等，逗号分隔" }),
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20, default: 10 })),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const allowed = new Set([
          "weibo",
          "xhs",
          "xiaohongshu",
          "bilibili",
          "bili",
          "douyin",
          "dy",
          "kuaishou",
          "ks",
          "tieba",
          "zhihu",
          "reddit",
        ]);
        const platforms = params.platforms
          .split(",")
          .map((s) => s.trim().toLowerCase())
          .filter(Boolean);
        const keywords = params.keywords
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
        if (!platforms.length || platforms.some((p) => !allowed.has(p))) {
          return {
            content: [
              {
                type: "text",
                text: `已拒绝 crawl_social：platforms 为空或含不支持平台。支持：${[...allowed].join("/")}`,
              },
            ],
            details: {},
          };
        }
        if (!keywords.length || keywords.length > 3 || platforms.length > 2 || keywords.length * platforms.length > 6) {
          return {
            content: [
              {
                type: "text",
                text:
                  `已拒绝 crawl_social：关键词需 1-3 个、平台 1-2 个且组合数 ≤6（当前 ${keywords.length} 关键词 × ${platforms.length} 平台）。` +
                  `MediaCrawler 每个组合最长 30 分钟，请拆分多次调用。`,
              },
            ],
            details: {},
          };
        }
        const dynamic = channelDynamicTimeout("crawl_social");
        const out = await runWriteScript(
          pi,
          ctx,
          `crawl_social ${platforms.join(",")}`,
          `将执行 python scripts/crawl_social.py --keywords "${params.keywords}" --platforms ${platforms.join(",")} ` +
            `--limit ${params.limit ?? 10}。副作用：MediaCrawler 抓取公开内容写入 data/crawler/ JSON；` +
            `脚本自带 12h 同关键词去重与 ≤20 条护栏。`,
          [
            "scripts/crawl_social.py",
            "--keywords",
            keywords.join(","),
            "--platforms",
            platforms.join(","),
            "--limit",
            String(params.limit ?? 10),
          ],
          keywords.length * platforms.length * dynamic.perComboMs + dynamic.overheadMs,
          signal,
        );
        return { content: [{ type: "text", text: out }], details: {} };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "backtest",
      label: "Backtest",
      description:
        "跑真实 LLM 零样本基线回测（写操作：写 data/backtest_baseline.json，耗 token）。sample 上限 200。",
      parameters: Type.Object({
        sample: Type.Optional(Type.Integer({ minimum: 10, maximum: 200, default: 50 })),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const sample = Math.min(Math.max(params.sample ?? 50, 10), 200);
        const out = await runWriteScript(
          pi,
          ctx,
          `backtest sample=${sample}`,
          `将执行 python scripts/backtest.py --sample ${sample}。副作用：调用 LLM（产生 token 费用），` +
            `写 data/backtest_baseline.json。`,
          ["scripts/backtest.py", "--sample", String(sample)],
          channelTimeout("backtest"),
          signal,
        );
        return { content: [{ type: "text", text: out }], details: {} };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "compare_backtest",
      label: "Compare Backtest",
      description:
        "泄漏受控对比：零样本 vs 完整管线（写操作：写 data/compare_backtest.json，默认内存库）。sample 上限 50。",
      parameters: Type.Object({
        sample: Type.Optional(Type.Integer({ minimum: 10, maximum: 50, default: 30 })),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const sample = Math.min(Math.max(params.sample ?? 30, 10), 50);
        const out = await runWriteScript(
          pi,
          ctx,
          `compare_backtest sample=${sample}`,
          `将执行 python scripts/compare_backtest.py --sample ${sample}。副作用：调用 LLM 与新闻检索` +
            `（产生 token 费用），写 data/compare_backtest.json；回测题默认入 :memory: 库，不进公开战绩。`,
          ["scripts/compare_backtest.py", "--sample", String(sample)],
          channelTimeout("compare_backtest"),
          signal,
        );
        return { content: [{ type: "text", text: out }], details: {} };
      },
    }),
  );

  contractTool(
    pi,
    defineTool({
      name: "fb_dry_run",
      label: "ForecastBench Dry Run",
      description:
        "ForecastBench 官方提交演练（写操作，永久 --dry-run，绝不真实上传）：拉题→入本地题池→跑管线→只打印。",
      parameters: Type.Object({
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50, default: 20 })),
      }),
      executionMode: SEQUENTIAL,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const limit = Math.min(Math.max(params.limit ?? 20, 1), 50);
        const out = await runWriteScript(
          pi,
          ctx,
          `fb_dry_run limit=${limit}`,
          `将执行 python scripts/fb_submit.py --dry-run --limit ${limit}。副作用：把官方未解决题复制进` +
            `本地 questions（is_public=False）并跑预测；不会生成 forecast set、不会记账、不会上传。`,
          ["scripts/fb_submit.py", "--dry-run", "--limit", String(limit)],
          channelTimeout("fb_dry_run"),
          signal,
        );
        return { content: [{ type: "text", text: out }], details: {} };
      },
    }),
  );

  // 完备门：契约登记 ⇔ 实际注册双向一致（违反即加载失败）
  assertBridgeContract();
}
