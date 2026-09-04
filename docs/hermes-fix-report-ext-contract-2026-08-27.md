# 扩展桥「第三条调用通道」契约化修复报告

- 日期：2026-08-27
- 范围：CC 评审报告 §1.2（P1，3-4h）+ §5.3 测试覆盖补强
- 改动文件（仅限边界内）：
  - `.foresight/extensions/foresight-tools.ts`
  - `tests/test_foresight_tools_contract.py`
  - 本报告 `docs/hermes-fix-report-ext-contract-2026-08-27.md`
- 未改动：`.foresight/SYSTEM.md`（人格护栏）、`src/`、`shell/pi/`、其它文件（并行任务占用）

## 1. 结论摘要

| 项 | 结论 |
|---|---|
| 第三条通道定性 | **TS 内联 Python 只读通道**：9 个只读工具（questions/question/leaderboard/scoreboard/system/calibration/health/events/logs）经 `runReadJson` 内联 Python 直调 `predictor.data.storage` / `predictor.ops.*` 内部 API，绕过引擎公开入口（`predictor.cli` / `scripts/*.py` 的单行 JSON 契约）——同桥两制，§1.2 实锤 |
| 处理方式 | **契约化 + 最小收紧（fail-closed）**：全部 22 通道收敛为单一契约常量 `BRIDGE_CONTRACT`；内联体 import 白名单 + `_out` 信封在加载期强制；注册期参数白名单比对；加载末双向完备校验。违反即扩展整体拒载 |
| 验收 | tsc exit 0；契约测试 **15/15 绿**（原 3 例 + 新增 12 例）；运行时正/负例验证 **4/4**（真 TypeBox 加载、越白名单/参数失配均抛「桥契约破坏」） |

## 2. 第三条通道定性

**是什么**：CC 报告 §1.2 所述「TS 内联 Python 代码直接调 `predictor.data.storage` / `predictor.ops.*` 内部 API」（报告时 8 个只读工具，本文件现已扩展为 9 个 inline_json + probe_quotes 走脚本 + backtest_report 纯文件读取，共 11 个只读工具）。修复前这些内联体是 execute 闭包内的字符串，引擎方法改名/签名变化会在**运行时**静默破坏工具，且该通道零测试覆盖（§5.3）。

**为什么叫「绕过工具定义」**：写工具走引擎公开入口契约（`scripts/predict_cli.py` 等，argv[1] JSON 传参防注入、SEQUENTIAL 防 DuckDB 单写者冲突），而读工具的执行路径绕过引擎公开入口定义，直接触达领域内部 API——同一座桥两套制度。

**相邻通道排查结论（一并记录）**：
- 内置工具清单已核（pi fork 源码）：bash/edit/write/find/grep/read。YOLO 关闭时 `tool_call` 钩子硬拦截 bash/edit/write（`block: true`），find/grep/read 仅只读诊断——agent 无可绕过扩展工具的 shell 逃逸路径；YOLO 开启为显式信任模式（放行 bash），属设计行为，不受本契约约束。
- `.foresight/extensions/` 仅此一个扩展文件（loader 自动发现目录），无其它扩展注入通道。
- 全文件 `pi.exec(` 仅 2 处（runPython/runWriteScript），Python 拉起已单漏斗化。

## 3. 契约设计

### 3.1 单一契约常量 `BRIDGE_CONTRACT`（TS 接口 + 22 通道清单）

```ts
interface BridgeChannelContract {
  name: string;            // 工具名（= contractTool 注册名）
  kind: "read" | "write";
  backend: "inline_json" | "script" | "fs_json";  // 第三条通道 = inline_json
  entry?: string;          // script 的脚本路径
  params: ReadonlyArray<{ name: string; required: boolean }>;  // 参数白名单
  timeoutMs?: number;      // 静态超时
  timeoutNote?: string;
  dynamicTimeout?: { perComboMs: number; overheadMs: number }; // crawl_social 动态超时
  pyApi?: readonly string[];  // inline_json 允许引用的 Python API 白名单
}
```

`BridgeContract` 另含：`python`（venv 可执行、`-E -X utf8` 环境旗标、项目根 cwd 规则、argv[1] JSON 传参）、`errorSemantics`（非零退出即 throw、`_out` 单行 JSON 信封、clip 20_000、写闸门、SEQUENTIAL）。

三条名义通道的收敛方式：
- **predict**（写，script）：`timeoutMs` 引用 `PREDICT_TOOL_TIMEOUT_MS = 900_000`（§4.2 单源链：常量→契约→execute 经 `channelTimeout("predict")`）。
- **leaderboard**（读，inline_json）：纳入第三条通道统一治理（pyApi 白名单 = Settings/Storage）。
- **resolve**（写，script）：60_000，params qid/outcome/source 全必填。

### 3.2 三层运行时校验（全部 fail-closed，违反即加载失败）

1. **注册门 `contractTool(pi, defineTool({...}))`**：全部 22 个工具改经此注册（`pi.registerTool(` 全文件仅剩门内 1 处）。校验：①工具名已登记契约；②TypeBox schema 属性名与契约 params 逐项一致；③TypeBox `required` 数组与契约必填标记一致（v1.3.7 运行时形态 `{type, required, properties}` 已实测）。
2. **内联体门 `readBody(channel, lines)`**：9 个内联体从 execute 闭包提升为模块级 `READ_BODY_*`，加载期逐行扫描 `from X import` / `import Y`，模块必须落在该通道 pyApi 白名单前缀内（如 health 白名单含 `predictor.ops.facts.build_facts`、`datetime.datetime`）；体内必须产 `_out`（readEnvelope）。**新增内部 API 调用必须先登记契约，否则扩展拒载。**
3. **完备门 `assertBridgeContract()`**：default export 末尾双向校验——契约登记未注册 / 已注册未登记 / script 缺 entry / inline_json 缺白名单 / 无任何超时定义，均抛错。

超时全部从契约取值（`channelTimeout()` / `channelDynamicTimeout()`），execute 内不再有魔法数，改超时只动契约一处。

### 3.3 结构性收尾（未做，另开任务）

CC §1.2 建议的根治方案——给 `predictor.cli` 补 read-only 子命令、9 个只读工具改薄调用——需改动 `src/`（本次文件边界禁止）。本契约已把它变成安全的过渡态：内部 API 的使用点被白名单登记在册，未来迁移时逐条替换即可，且迁移完成前任何引擎 API 变更会同时被 TS 加载校验 + Python 契约测试双网拦截。

## 4. 测试补强（tests/test_foresight_tools_contract.py）

原 3 例保留（§4.2 predict 超时），`test_predict_tool_run_python_uses_contract_timeout` 更新为断言经 `channelTimeout("predict")` 取契约超时 + 契约引用常量。新增 12 例：

| 测试 | 断言内容 |
|---|---|
| 契约常量存在 + version=1 | BRIDGE_CONTRACT 声明与版本 |
| 通道清单双向一致 | 契约 22 条 ⇔ contractTool 注册 22 条（**通道清单**） |
| 全部注册走门 | `pi.registerTool(` 全文件恰 1 处 |
| 参数白名单 | 每通道 kind/backend/entry/pyApi/参数名与必填标记（**参数白名单**） |
| 超时值 | 静态超时逐通道比对；predict 引用常量；crawl_social 动态公式参数（**超时值**） |
| 内联体经 readBody 门 | 9 个 READ_BODY_* 提升式构建 |
| 第三条通道 import 白名单 | 每个内联体的全部 import ⊆ 该通道 pyApi 前缀 |
| readEnvelope 语义 | readBody 强制 `_out`；runReadJson 缺省 error 信封 |
| Python 拉起单漏斗 | `pi.exec(` 恰 2 处、双 spawn 点带 `-E -X utf8`、argv[1] JSON 传参 |
| 非零退出抛错 | 两个 spawn 封装 `code!==0||killed` → throw |
| YOLO-off 拦截护栏 | tool_call 钩子拦 bash/edit/write 且 `block:true` |
| clip/串行不变量 | OUTPUT_CLIP=20_000 与契约一致；写工具 executionMode=SEQUENTIAL |

## 5. 验证记录

- `tsc -p .foresight/extensions/tsconfig.json` → **exit 0**
- `env -u PYTHONPATH .venv/Scripts/python.exe -E -X utf8 -m pytest tests/test_foresight_tools_contract.py` → **15 passed**
- 运行时验证（Node v26 原生类型剥离 + 真 typebox v1.3.7 + stub `defineTool`，临时目录副本，不落仓库）：
  - 正例：扩展加载 + 注册全量无异常，注册 22 工具 ✓
  - 负例 1：内联体注入 `import predictor.ops.sneaky` → 加载抛「桥契约破坏 …predictor.ops.sneaky」✓
  - 负例 2：question 工具参数改名 `qid_x` → 注册抛「桥契约破坏 …question」✓

## 6. 残余风险与后续建议

1. **内联体仍是字符串 Python**：pyApi 白名单管住了 import 面，但内联体里已白名单 API 的**方法调用**（如 `s.list_questions_all`）无法在 TS 侧静态校验——引擎改名仍会在工具调用时抛错。根治 = §3.3 的 predictor.cli read-only 子命令（另开任务，P1）。
2. **§5.3 的 vitest 冒烟（mock pi.exec）未覆盖**：本次用「静态契约测试 + 加载期运行时校验」替代了行为冒烟；工具执行行为（runReadJson 解析、writeGate 拒绝路径）仍无单元测试，建议后续按 §5.3 补（4-6h，另开任务）。
3. **YOLO 开启后 bash 通道**为显式信任模式，agent 可直跑任意 `scripts/*.py`——契约不覆盖该通道，属设计决策；如未来收紧，应在 tool_call 钩子对 YOLO 态的白名单脚本清单做限定（需先与 SYSTEM.md 人格护栏对齐）。
