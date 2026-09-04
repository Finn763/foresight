# CC §4.2 修复报告：LLM 采样并发 + 工具层超时对齐（2026-08-27）

> 对应 CC 全面评审报告 §4.2（P1，最简版）。范围：采样并发、工具层超时与引擎最坏时长倒挂、AsyncClient 复用、predict 心跳、单测。
> 未 commit；STATUS.md 同步留待主会话（文件边界外）。

## 一、结论摘要

| 项 | 修复前 | 修复后 |
|---|---|---|
| 采样执行 | n_samples=2 **串行**，采样阶段最坏 ≈ 726s | **asyncio.gather 并发**，采样阶段最坏 ≈ 363s（-50%） |
| predict 工具超时 | **180s**（先于引擎内部完成） | **900s（15 分钟）**，覆盖并发最坏 2.3×、串行回退 1.2× |
| 重试连接 | 每次重试重建 AsyncClient | 单次调用复用同一 AsyncClient（连接池） |
| 长预测可观测性 | 无进度输出（挂起时完全失明） | stderr 心跳：序列拉取结束 → 采样 1 → 采样 2 |

## 二、根因

1. **采样串行放大最坏时长**：`websearch_predict` 逐个 `_sample`；单次采样最坏 = responses API
   timeout 120s × 3 次尝试（max_retries=2）+ 指数退避 1s+2s ≈ **363s**；2 采样串行 ≈ 726s，
   加序列拉取 ~20s ≈ **12.5 分钟**。
2. **工具层超时倒挂**：foresight-tools.ts predict 工具超时 180s < 引擎最坏 12.5 分钟 →
   工具层先 kill python → agent 盲目重试 → TUI 挂起（与 STATUS「predict 挂起 17 分钟」吻合）。
3. **连接浪费**：`client.py` `_aresponses`/`_achat` 每次重试重建 AsyncClient（TCP/TLS 握手 ×3）。

## 三、修改清单

### 1. src/predictor/llm/client.py
- `_achat`、`_aresponses`：AsyncClient 移出重试循环（复用连接池，重试不再重建）；
  payload（max_output_tokens 倍增等）仍在重试间就地变更，语义不变。
- `_aresponses` 提为公开 **`aresponses_create`**（async），同步 `responses_create` 薄封装
  `asyncio.run`。并发采样直接 await 异步入口（asyncio.run 不可嵌套）。
- llm_resolver.py 等既有同步调用方不受影响（继续走 `responses_create`）。

### 2. src/predictor/websearch_predictor.py
- `_sample` 拆为 `_build_instructions` + `_parse_sample` + async `_asample`；保留 `_sample`
  同步封装（串行回退/测试基准）。
- `websearch_predict`：n_samples 采样改 **asyncio.gather 并发**（内层 `_run_all` 里调 gather——
  Python 3.13 下循环外 gather 会挂 deprecated 临时 loop 报跨 loop 错，已规避）。返回顺序 =
  任务顺序 = 采样顺序，聚合逻辑（`samples[0].rationale`、probs 均值、引用合并）逐行不变。
- 新增 `_heartbeat`：stderr 三行心跳（序列拉取阶段结束 → 采样 1 → 采样 2），失败静默
  （写 stderr 异常吞掉），stdout 零输出（单行 JSON 契约不受污染）。

### 3. .foresight/extensions/foresight-tools.ts
- predict 工具超时 180_000 → `PREDICT_TOOL_TIMEOUT_MS = 900_000`（15 分钟），常量 + 调用
  一并改；注释内附引擎最坏时长推导（见 §四）。

### 4. 测试（就近补齐）
- `tests/test_websearch_predictor.py`：FakeClient 增 async `aresponses_create`（带可选延迟）；
  +5 测试：并发与串行语义等价（n_samples=3）、墙钟并发证明、同步封装一致、心跳只进
  stderr、失败采样心跳。
- `tests/test_llm_client.py`、`tests/llm/test_client.py`：+2 重试复用回归（计数 transport，
  3 次尝试只 aclose 1 次 = 单 client 全程）。
- `tests/test_evolve.py`：FakeClient 补 async 入口（适配并发化，就近受影响测试）。
- `tests/test_foresight_tools_contract.py`（新）：超时配置值校验 ×3——常量 ≥15 分钟且 ≥ 引擎
  最坏、runPython 调用使用常量、引擎最坏计算依据（120s 缺省 timeout / max_retries=2）护栏。

## 四、引擎最坏时长估算（工具层超时取值依据）

| 阶段 | 计算 | 时长 |
|---|---|---|
| 单次采样最坏 | 120s（responses 缺省 timeout）× 3 尝试 + 退避 1s+2s | ≈ 363s |
| 采样阶段（2 采样，**并发前**） | 2 × 363s 串行 | ≈ 726s（12.1 分钟） |
| 采样阶段（2 采样，**并发后**） | max(363s, 363s) | ≈ 363s（6.1 分钟） |
| 序列拉取 | fetch_series_map（历史基线，实测） | ≈ 20s |
| **引擎最坏（并发后）** | 363 + 20 | **≈ 383s ≈ 6.4 分钟** |
| 引擎最坏（串行回退兜底） | 726 + 20 | ≈ 746s ≈ 12.4 分钟 |

**取值 15 分钟（900s）**：对并发最坏 ≈2.3× 余量；即使采样退回串行（746s）仍完整覆盖，
工具层超时不再先于引擎内部完成。

## 五、挂起问题解决依据

- **直接原因消除**：工具层 900s > 引擎最坏 383s（并发）/746s（串行兜底）——python 不再被
  工具层超时 kill，agent 不会因 180s 截断而盲目重试。
- **可观测性**：心跳三行进 stderr（runPython 经 pi.exec 捕获 stderr，失败时报错、成功进
  exec 日志）——长预测期间 TUI/日志能看到「序列拉取 → 采样 1 → 采样 2」阶段推进，不再
  全程失明。
- **资源**：AsyncClient 复用省去每次重试的握手重建；并发采样使最坏时长减半。

## 六、验证结果

- pytest（相关 9 文件，66 用例）：**66 passed**，含既有回归（test_evolve 适配后全绿）。
- ruff check（7 个改动 py 文件）：**All checks passed**。
- TS：`shell/pi/node_modules/.bin/tsc -p .foresight/extensions/tsconfig.json --noEmit` → **exit 0**。
- 未 commit、未碰 .env/shell/pi/historical.py/.foresight/SYSTEM.md（任务约束遵守）。

## 七、遗留 / 注意

1. **生效方式**：foresight-tools.ts 是 jiti 加载，需 `/reload` 后新超时生效（本次未验证交互）。
2. **STATUS.md 未同步**：文件边界外，由主会话按单页状态纪律更新（可记「§4.2 已修复 + predict
   工具超时 15 分钟」）。
3. **公开仓脱敏提醒**：新注释含内部语境（「CC §4.2」「STATUS 挂起记录」），推
   foresight-public 前按脱敏纪律清洗同类措辞。
4. 全量 pytest 未跑（任务要求只跑相关文件）；测试含 1 个墙钟断言（0.4s×2 采样 <0.75s），
   CI 若抖动可先单跑复验。
