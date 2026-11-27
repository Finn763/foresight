# agent 取证工具：web_search / read_url（2026-11-27）

- 起因：对照 DeepSeek《构建 Chatbot》文档的 9 项能力，agent 侧缺口是「联网搜索 + 阅读链接」
- 结论：新增两个只读工具 `web_search`（Search API）与 `read_url`（Fetch API），其余 7 项属 Chatbot UX / 服务端合规开关 / 对话接口能力，不适用于 CLI 预测 agent（逐项理由见 §5）
- 改动文件：
  - `src/predictor/llm/client.py`：新增 `afetch` / `fetch`
  - `.foresight/extensions/foresight-tools.ts`：2 个通道 + 2 个内联体 + 2 个工具注册
  - `.foresight/SYSTEM.md`、`.foresight/APPEND_SYSTEM.md`：工具清单与边界
  - `tests/test_foresight_tools_contract.py`、`tests/test_llm_client.py`

## 1. 为什么走 REST、不走对话服务端工具

DeepSeek 的 `deepseek_web_search` / `deepseek_read_url` 是**对话场景**的服务端工具；同一篇《联网工具》文档明确写：在 Agent 或自己的程序里搜索和抓取网页，请使用 `/v1/tools/search` 与 `/v1/tools/fetch`。此外 `/v1/responses` 的 `web_search` 自 2026-09 起不再真正执行（见 `src/predictor/llm/evidence.py` 注释），预测链路已改走独立 Search 接口——本次沿用同一条路，agent 与引擎共用 `LLMClient`，不引入第二套直连。

## 2. 工具契约

| 工具 | 参数（白名单） | 后端 | 超时 | 返回 |
|---|---|---|---|---|
| `web_search` | `query`(必填 1-128) / `count`(1-10，默认 5) / `need_content`(默认 false) | `inline_json` → `LLMClient.search` | 60s | `{query, count, results[]}`，每条含 title/url/site_name/date/authority/rerank_score/summary(≤900 字符)，`need_content=true` 时附 content(≤4000 字符) |
| `read_url` | `url`(必填，含 host 的 http(s)) | `inline_json` → `LLMClient.fetch` | 90s | `{url, title, content(≤40000 字符 Markdown)}` |

两者 `kind: "read"`：YOLO/OPS 关闭时也可直接调用（无确认框、无写库）。

## 3. Fetch 客户端语义

`POST {base_url}/v1/tools/fetch`，Bearer 鉴权，请求体 `{"url": ...}`：

- 只重试 `408 / 429 / 5xx`（官方错误表可重试项），其余 4xx 立即抛 `LLMError`，不为同一 URL 重复计费
- 错误信息取 OpenAI 兼容错误体的 `error.message`（`_error_detail`），取不到回退截断原文
- 本地先校验 scheme ∈ {http, https} 且含 host（`file://` 等不发请求）
- 计费：Search / Fetch 各 ¥5 / 千次成功请求，失败与空结果不计费；`count` 不影响单次价格

## 4. 契约与护栏

- `BRIDGE_CONTRACT`：23 → 25 条（13 只读 + 12 写），第三条通道内联体 9 → 11
- pyApi 白名单新增 `predictor.llm.client.LLMClient`；内联体仍经 `readBody` 的 import 白名单 + `_out` 信封门
- `contractTool` 参数白名单/必填标记与契约逐项比对，失配即拒载
- `SYSTEM.md` 新增边界：检索工具只用于取事实与可引用链接，**概率仍只能来自 `predict`**，不得用检索结果自算概率

## 5. 未纳入的 7 项及理由

| 能力 | 不进 agent 的理由 |
|---|---|
| 深度思考搜索 | 是同一工具的思考模式组合，非独立能力；agent 已有思考层级 |
| 文件输入（PDF/Word/PPT 解析） | 对话接口能力；预测链路要的是网页与数据源，不是上传文档 |
| 模型内容安全 `safety_check` | 面向对外服务的合规开关，Foresight 为自用 agent |
| 用户元信息标记 | 已由 SYSTEM.md 与运维轮提示词显式注入日期 |
| 会话标题 | pi fork 会话层已有标题机制，与预测能力无关 |
| 朗读回答 TTS / 语音输入 ASR | CLI agent 无音频场景 |

## 6. 验证

- `pytest tests/test_llm_client.py tests/test_foresight_tools_contract.py` → 28 passed（含新增 fetch 5 例）
- `ruff check` 三个改动文件 → All checks passed
- 扩展 `tsc -p`（去掉预置 `baseUrl` 后）→ 0 错误
- 真实端到端（`foresight -p ... --mode json --no-session`，2026-11-27）：
  - `web_search("DeepSeek V4 发布", count=2)` → 2 条真实结果，`isError:false`
  - `read_url("https://api-docs.deepseek.com/zh-cn/guides/search/")` → `title="网页搜索与抓取"` + Markdown 正文
  - 成本：模型 $0.00136 + 两次 API 调用 ¥0.01

## 7. 已知遗留

`.foresight/extensions/tsconfig.json` 仍带 `"baseUrl"`，仓库当前 TypeScript 6 下 `tsc -p` 会报 TS5102（`baseUrl` 已移除）；删掉该行即可。本次未改（与工具能力无关，避免混入无关变更）。
