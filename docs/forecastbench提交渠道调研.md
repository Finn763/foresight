# ForecastBench 提交渠道调研（2026-08-11 实测查证）

## 结论速览

- **官方提交 = 邮件注册制 + GCP Cloud Storage bucket 上传**，不是公开 API，没有 Metaculus bot 通道。
- **逐题答案（ground truth）不公开**：官方只在排行榜公布分数；官方数据集的题 URL 是截断的（部分题已删除），无法用 manifold/metaculus 公开 API 反查答案。
- 因此官方榜单分数应理解为**提交后由官方计分公布**，本地只能提交 + 等分数。

## 查证过程（2026-08-11）

1. `forecastingresearch/forecastbench-datasets` 仓库存在（github.com 网页 200）。
2. 仓库结构：`datasets/question_sets/YYYY-MM-DD-llm.json`（每两周一期，2025-03-02 起，另有 latest-llm.json）；`datasets/forecast_sets/`（Git LFS 指针文件，需 git-lfs 拉取，含人类预测集）。
3. question_sets 真实结构：
   ```json
   {"forecast_due_date": "2025-06-08", "question_set": "...", "questions": [{"id", "source", "question", "resolution_criteria", "background", "market_info_open_datetime", "market_info_close_datetime", "url", "freeze_datetime", ...}]}
   ```
   - **题内无 resolution 字段**（未解决题）
   - 2025-10-26 前的旧题集含 combination questions（id 为数组），需丢弃
   - `url` 字段是**截断的**（slug 不完整），manifold `by-id`/`slug` API 均 404；部分题 url 含 `_deleted_`
   - `freeze_datetime` = 预测冻结时间（回测检索截止应以此为界）
4. 官方提交机制（wiki: How-to-submit-to-ForecastBench）：
   - 发邮件至 forecastbench@forecastingresearch.org：Gmail/Google Workspace 邮箱列表、组织名（可匿名）、网站、logo
   - 官方回复：GCP bucket 文件夹 + 下一个 forecast due date（每两周）
   - 流程：due date 0:00 UTC 下载 Question Set → 生成 Forecast Set → 23:59:59 UTC 前上传
   - 上榜要求：至少对规定的题目子集提交预测
5. 网络实测：github.com / forecastbench.org / api.manifold.markets 通；raw.githubusercontent.com / api.github.com / huggingface.co / metaculus.com 部分网络不可达。本仓库已将 36 期 question_sets 落盘 `data/fb_seed/` 作本地降级源（fetch 失败自动读本地）。

## 对回测与提交实现的影响

- 回测模块（benchmarks.py）按真实结构实现（dict 顶层 + questions[] + freeze_datetime + combination 过滤 + 本地 seed 降级）。回测计分需"已揭晓题答案"——公开渠道拿不到逐题答案，调试基线改用**自建可判定题 + 人工揭晓 CSV**（resolve.py 流程）积累 Brier；官方分数由提交后获取。
- 官方提交模块（forecastbench_official.py）：
  - `fetch_open_questions(limit=20)` 读 `data/fb_seed/latest-llm.json`（无本地数据时降级 GitHub raw）；过滤 combination id（数组）/已揭晓/缺文本题；closes_at 取 `freeze_datetime`（占位 2525 年跳过）→ set 级 `forecast_due_date` 兜底
  - `submit_predictions(predictions, *, api_token)` 生成 `data/forecast_sets/<due>_forecast_set.json`；配置 `FORECASTBENCH_GCS_BUCKET` 且有 gcloud 时自动上传（best-effort）
  - 本地记账 = `data/forecastbench_ledger.json`（append-only，题号+概率去重）
- `scripts/fb_submit.py`：`--limit`/`--dry-run`/`--db`/`--ledger`；dry-run 只跑管线不落盘不记账。
- 测试：`tests/data/test_forecastbench_official.py` 11 项离线测试（本地 set 解析/过滤/limit、HTTP 降级 MockTransport、forecast set 生成、ledger 记账）。
- 遗留 TODO：① 邮件注册拿到 GCP bucket 后配 `FORECASTBENCH_GCS_BUCKET`；② 网络恢复后复核 raw URL 与 forecast set 官方 schema；③ forecast set 文件名规则（官方 wiki 未逐字复核）。
