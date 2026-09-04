# 修复报告：每道题重复拉全量历史序列（CC §4.1，P1）

- 日期：2026-08-27
- 范围：`src/predictor/stats/historical.py`（内部实现）+ `tests/stats/test_historical_cache.py`（新增）
- 调用方（websearch_predictor.py 等）**零改动**，`fetch_series_map(now)` 签名不变

## 1. 问题

`websearch_predictor.py:270 → _load_baseline:255 → fetch_series_map(now)` 在 daily/evolve 主循环里**每题调用一次**，每次串行拉取 10 个序列（Yahoo 6 + FRED 3 + EIA 1）；同轮内 `now` 仅秒级差异、结果完全相同。8-27 全天 29 次调用 ≈ 9.9 分钟纯冗余网络 IO，且放大 Yahoo/FRED 限流风险。

## 2. 方案

### 2.1 轮级缓存——key 用日期粒度

- 缓存 key = `now.date().isoformat()`（如 `"2026-08-27"`），**不是精确 now**。
- 理由：同轮内每题 `now` 只差秒级，防泄漏截断（Yahoo `period2`/FRED/EIA `end`）在同一天内的差异不产生实质影响；用日期粒度能让同轮 29 次调用全部命中同一条目。跨日自动失效，无需 TTL 清理。
- 命中时返回**同一个 dict 对象**（已核实全部调用方只读：`compute_baseline`/`build_series_context` 仅 `.get()`；`series_json_cache` 自行瘦身拷贝）。
- 线程安全：读/写用 `threading.Lock`；`cache_clear()` 供测试隔离与长时间驻留进程维护。
- **失败不缓存**：①任一序列抛异常（内部 HTTP 错误已被各 fetch 函数降级为 `[]`，此处指意外异常）→ 不写缓存；②全部序列为空（如全网断）→ 不写缓存。下次调用自动重试。
- 权衡（有意为之）：单个源返回空但整体非空（如未配 FRED key 恒空、单源暂时限流）仍缓存——否则某源整日故障会使缓存永不生效、性能退化回修前。异常序列本身仍按原语义降级为空列表、不拖垮整体。

### 2.2 10 序列并发拉取

- httpx 同步代码路径 → `ThreadPoolExecutor(max_workers=10)`，每序列一个 worker，`as_completed` 收结果。
- 单序列超时与降级语义完整保留：Yahoo 15s / FRED 15s / EIA 20s 各自 timeout 不变；某序列失败（异常→空列表）不阻塞其他序列。
- 每个 fetch 函数仍自建 `httpx.Client`，线程安全无共享可变状态；生产 transport=None（httpx 默认 transport 线程安全）。

## 3. 实测时耗（2026-08-27 晚，真实网络 + 项目 .env key，本机）

| 场景 | 时耗 | 说明 |
|---|---|---|
| 修前串行（等价实现复测①） | **27.96s** | 18:59 窗口，网络偏劣（CC 报告原测 20.4s） |
| 修前串行（复测②） | 16.28s | 19:02 窗口，单序列分布见下 |
| 修前串行（复测③，配对窗口） | **14.14s** | 与修后同窗口对比 |
| 修后并发冷启动（配对窗口） | **4.67s** | 与串行 14.14s 同窗口，≈3× 提速 |
| 修后并发冷启动（另一窗口） | **6.79s** | 恰等于最慢单序列 FRED ffr（6.79s）|
| 修后并发冷启动（劣网窗口） | 18.35s | 同窗口串行为 27.96s，瓶颈=最慢单序列 |
| 修后同日第二次调用（缓存命中） | **0.0000s** | 返回同一对象，零 HTTP |

单序列分布（19:02 窗口）：Yahoo 6 个 0.77–1.09s；FRED cpi_cn 1.25s、**ffr 6.79s（瓶颈）**、wti_price 1.52s；EIA 1.08s。

结论：并发后总时耗 = max(单序列)，本机窗口 4.7–6.8s，**≤8s 目标达成**；劣网窗口随最慢单序列放大（串行 27.96s→并发 18.35s，仍 ~35% 提速）。轮内影响：29 次调用从 ~9.9 分钟 → 1 次冷拉（~5s）+ 28 次 0ms，节省 **~99%**。

## 4. 测试

新增 `tests/stats/test_historical_cache.py`（6 用例，全离线：httpx.MockTransport + monkeypatch 假 key）：

1. `test_cache_hit_same_day_no_extra_http` — 同日二次调用请求数保持 10（**断言请求次数=1 次轮拉取**）、返回同一对象
2. `test_cache_invalidates_across_dates` — 跨日期失效（10→20），次日当天内仍命中
3. `test_network_error_not_cached_retry` — 网络失败（全空）不缓存，恢复后同日重拉
4. `test_fetcher_exception_not_cached` — 拉取器抛异常不缓存，恢复后正常重试
5. `test_cache_clear_forces_refetch` — cache_clear() 后强制重拉
6. `test_concurrent_fetch_parallel_and_data_integrity` — 最大同时在途请求 ≥5（串行实现只会是 1）+ 10 序列数据归属逐一校验 + 成功轮写缓存

验证：`pytest tests/stats/ tests/test_evolve.py` = **37 passed**（stats 23 含原有回归 + evolve 14）；`ruff check src/predictor/stats/historical.py tests/stats/test_historical_cache.py` = **All checks passed**。

## 5. 边界与注意

- 缓存为**进程内存级**：daily/evolve 每轮是新进程，每轮首题仍真实拉取一次（轮级缓存设计目标即此）；跨进程/跨天不共享。可选后续：落盘持久化（未做，避免动 data/ 目录与并行任务冲突）。
- 日粒度 key 的防泄漏近似：命中返回当日**首次拉取时点**截断的数据；生产调用（daily/evolve/cli）同轮 now 单调，不扩大泄漏窗口。`scripts/compare_backtest.py` 传历史日期，仅同日多题时共享缓存，影响可忽略。
- 未改 `websearch_predictor.py`（并行任务占用）、未 commit、未碰 .env/shell/pi/.foresight/、未写 DuckDB。STATUS.md 同步留给主 agent 汇总（docs/ 有并行任务在写）。
