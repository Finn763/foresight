# Foresight Brier 统计口径修复报告（CC §2.3，P2）

- 日期：2026-08-27
- 任务：修复 CC 评审报告 §2.3「生产战绩 Brier 0.2286 ≈ 硬币，且统计口径失真」的三个问题
- 变更边界：`src/predictor/data/storage.py` + `tests/data/test_storage.py`（未碰 server.py / pipeline.py / websearch_predictor.py / config.py / .env / 生产库）
- 验证：相关 pytest 71 用例全绿；ruff 干净；生产库 `data/foresight.db` 只读实测对账通过

---

## 一、三项问题的口径决策

### ① scoreboard 无「按题族」统计 → 新增 `brier_by_family()` 维度

**决策：族 key = `resolution_spec.instrument` → `resolution_spec.category` → `unclassified` 兜底。**

理由（基于生产库只读实测，非猜测）：
- 题族系统实际存在两套谱系：A 类行情网格题族（`selection/families.py` 的六族：标普/上证/道琼斯/黄金/人民币/布伦特，对应 `resolution_spec.instrument` 的 spx/sh/dji/gold/usdcnh/brent）与 autopick 主题族（`resolution_spec.category`，如 central_bank/Finance）。
- 生产库已揭晓题实测：instrument 分布 spx 9/usdcnh 8/sh 5/gold 5/dji 4/brent 1/btc 1；category 仅 2 行非空（autopick 今天刚入库）；旧 B 类题 spec 只有 `{"class":"B"}`。**只用 category 会把 18 道已揭晓公开题几乎全部打成 unknown，维度失去意义**——故 instrument 优先、category 兜底。
- 未采用「event_key 前缀」：event_key 首段是事件 slug（如 `warsh-jackson-hole-speech` 的 `warsh`），不是族标识，语义噪声。
- `unclassified` 兜底保证族桶计数与 `resolved` 总数可对账（实测 3+2+5+3+1+4=18 与 scoreboard resolved 一致）。
- 口径与 `brier_by_horizon_bucket` 完全一致：`brier_score IS NOT NULL + is_public + outcome IS NOT NULL`，n<30 标 `unreliable`。SQL 用 `json_valid()` 防御损坏 spec，`json_extract_string` 提取（JSON 列不可 LIKE，沿项目既有约定）。
- 落点：`scoreboard_summary()` 新增 `families` 字段（纯增量，`/api/scoreboard`、`public_summary()` 自动携带，server.py 无需改动）。
- **未做** CC 报告另半句「与 0.5 常数的 ΔBrier」——本任务范围只列了题族维度，留作后续小改（可加 `brier_mean - 0.25` 字段，一行 SQL）。

### ② `model_stats.brier_ema` 归属失真 → 修正归属口径

**决策：选择「修正」而非「仅注释」。brier_ema 只归属真实产生该预测的模型名：**
- **单模型键（现状）** → 归属该模型名（经 ③ 的配置化转名）；
- **多模型键（未来真多模型集成）** → 归属保留名 `ensemble`（`_ENSEMBLE_OWNER`）：管线最终概率是跨模型集成的产物，不归属任何单一模型，不再把同一 Brier 分摊给 model_runs 内所有模型名——这正是 CC 指出的语义失真；
- **空 dict / SQL NULL** → 不写 stats（保持现状）；
- **损坏 / 非对象（如 JSON null）** → 抛 ValueError，沿用既有降级契约：`resolve_question` 计分段 catch 后记 `resolution_brier_failed`，brier 计分不受影响（原行为是 `json.loads("null")` 返回 None 后迭代抛 TypeError 触发同一路径，契约不变，`tests/test_evolve.py` 的既有用例无需改动继续绿）。

口径注释同步写进代码：brier 是「管线最终概率」（ensemble+extremize+校准后）的 Brier——单模型时归属该模型是当前在线权重（`weights_from_model_stats`）的设计意图，多模型时不再以模型名义记账。

### ③ 模型名硬编码 → storage 侧配置化兼容（最小侵入方案）

**决策：在 storage.py 落点「记录时不信任传入的硬编码名，改从 Settings 读」。**

代码事实（读源码确认，未改）：所有预测路径——`cli.py:68`、`scripts/daily.py:149`、`scripts/backtest.py:23`——都用 `Settings.llm_client_kwargs` 构造 LLMClient，即**两个预测路径（classic 管线与 websearch）实际跑的是同一个模型 `Settings().deepseek_model`**。pipeline.py:137 的 `"deepseek-chat"` 与 websearch_predictor.py 的 `"deepseek-flash-websearch"` 都是与配置脱钩的历史硬编码。

实现（全部在 storage.py，pipeline.py / websearch_predictor.py / config.py 零改动）：
- `get_model_name()`：单一事实源，返回 `Settings().deepseek_model`，供两文件**后续**切换（不在本任务边界，暂不代改）；
- `canonical_model_name(name)`：resolve 写 model_stats 前调用——两个历史硬编码名统一映射到配置模型名；非硬编码名原样保留（未来真多模型直接写入配置名即可）；Settings 不可用/配置为空回退原名（不阻塞揭晓）；
- `_load_settings()` 独立工厂函数，供测试 monkeypatch 钉死配置，保证测试与本机 `.env` 解耦。

行为变化说明：`.env` 未设置 `DEEPSEEK_MODEL`（默认 deepseek-chat）时映射是恒等变换，生产行为零变化；`.env` 换模型后，新揭晓记录的 stats 标签自动跟随配置模型名（两个臂合并到同一真实模型名行——因为本就是同一客户端同一模型；臂维度对比仍由 `arm_stats` 承担）。**历史 model_stats 行不做迁移**（生产库只读、且历史行本就按当时口径入账；现有 `deepseek-chat`×8 / `deepseek-flash-websearch`×11 两行冻结，新记录写入配置名）。

---

## 二、变更明细

| 文件 | 位置 | 变更 |
|---|---|---|
| `src/predictor/data/storage.py` | 模块顶部 | 新增 `_LEGACY_MODEL_HARDCODES`、`_ENSEMBLE_OWNER`、`_load_settings()`、`get_model_name()`、`canonical_model_name()` |
| 同上 | `resolve_question` 计分段 | 逐名 upsert 改为 `self._brier_owner_names(mr_json)`；注释写明管线最终概率口径 |
| 同上 | `resolve_question` 之后 | 新增 `_brier_owner_names()`（②归属口径：单键→配置名；多键→ensemble；空→[]；损坏→raise） |
| 同上 | `brier_by_horizon_bucket` 之后 | 新增 `brier_by_family()`（①题族分桶，instrument→category→unclassified） |
| 同上 | `scoreboard_summary()` | 新增 `d["families"] = self.brier_by_family()` |
| `tests/data/test_storage.py` | 顶部 | 新增 autouse fixture `_pin_model_settings`（钉死 `_load_settings`，测试与 .env 解耦） |
| 同上 | 文件尾部 | 新增 6 个用例（见下） |

## 三、新增测试（tests/data/test_storage.py，全绿）

1. `test_brier_by_family_buckets_instrument_category_and_unknown` — instrument/category/unclassified 三路分桶、brier_mean 精确值、非公开题不进桶、n 与 resolved 对账、unreliable 标注、scoreboard_summary 携带 families。
2. `test_model_stats_brier_ema_single_owner` — 单模型键归属该模型名。
3. `test_model_stats_brier_ema_multi_model_goes_to_ensemble` — 多模型键不再分摊，归属 `ensemble`。
4. `test_model_stats_canonicalizes_legacy_hardcoded_names` — 两个硬编码名映射到配置名并合并计数、EMA α=0.1 递推精确值、非硬编码名原样保留。
5. `test_model_stats_empty_model_runs_not_recorded` — 空 runs 不写 stats（现状基线）。
6. `test_model_stats_non_dict_model_runs_logged_not_raised` — 非对象 runs 降级：brier 照常落库、记 `resolution_brier_failed`、不向上抛。

## 四、验证结果

- **pytest**：相关 7 文件 71 用例全绿（test_storage 19 + storage_migration + storage_readonly + calibration + web_api_public/internal + evolve）。
- **ruff**：`ruff check src/predictor/data/storage.py tests/data/test_storage.py` → All checks passed。
- **生产库只读实测**（read_only=True，无写入）：`brier_by_family` 输出 dji 3 / gold 2 / sh 5 / spx 3 / usdcnh 1 / unclassified 4，合计 18 = scoreboard `resolved` 18，完全对账；`scoreboard_summary` 新增 families 字段正常；model_stats 历史两行未被触碰。

## 五、边界遵守

- 只改了 storage.py + 对应测试；未碰 server.py（并行任务占用）、pipeline.py、websearch_predictor.py、config.py、.env、shell/pi/、.foresight/。
- 生产库只读访问，零写入；无 commit。

## 六、遗留与后续建议

1. **pipeline.py / websearch_predictor.py 的 model_runs 键**：建议后续任务把硬编码改为 `from predictor.data.storage import get_model_name`，届时 `canonical_model_name` 的映射自动退化为恒等（兜底可保留）。
2. **ΔBrier（vs 0.5 常数）**：CC §2.3 建议的另一半，未在本次范围，一行 SQL 可补。
3. **历史 model_stats 两行**：如需与配置名对齐，可在下次有写权限的维护窗口做一次性 `UPDATE model_stats SET model_name = <配置名> WHERE model_name IN ('deepseek-chat','deepseek-flash-websearch')`（若两行已映射到同一名需先按 EMA 加权合并）。
