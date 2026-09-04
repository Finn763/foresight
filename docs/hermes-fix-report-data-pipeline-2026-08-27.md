# 数据管线两处缺口修复报告（CC §2.7 ①②，P2）

- 日期：2026-08-27
- 范围：仅 `src/predictor/data/storage.py`、`src/predictor/selection/dedup.py` + 对应测试（tests/data/test_storage.py、tests/data/test_storage_migration.py、tests/selection/test_dedup.py）
- 状态：**完成**。相关测试全绿、全量 pytest 438 用例全绿（RC=0）、ruff 干净、未 commit、未碰 .env/shell/pi/.foresight/、生产库零写入（全部只读核实）

---

## 一、结论摘要

| 缺口 | 修复 | 验证 |
|---|---|---|
| ① `source_documents` 无 (question_id,url) 唯一约束：实测 55 组重复 URL（每组 2 行，多余 55 行）、1915/2345 行 content 为空 | create_schema 幂等迁移（清理历史重复 + 唯一索引）+ 写入路径 `INSERT OR IGNORE` 复用既有行 id | 迁移单测（旧库带重复行 → 清理 + curated 引用重定向 + 索引 + 幂等）、重复插入被忽略单测；生产库只读核实 55 组/1915 空与报告一致 |
| ② dedup 标的表不覆盖美联储/FOMC/CPI/EIA → #72/#73/#74 三道近义题并存；否定句式方向抽取误判 | 标的表补宏观题族（fed/CPI 国别消歧/EIA/非农/PMI）+ 题族分方向表 + 否定句式 `_neg` 方向隔离 + 宏观题族月粒度日期 | 生产库只读实测：#72/#73/#74 签名一致；新表述「美联储2026年9月会加息吗」命中已存在题 #73；「美联储9月FOMC会议会降息吗」命中 #47（顺带覆盖 #47/#86 同族近义题）；「2026年10月中国CPI…」命中 #5 |

生产库写清步骤（55 组重复删除）**未直接执行**：迁移代码已并入 create_schema，随生产下次启动自动执行（幂等），用户可择时重启；也可在停轮窗口手动用 duckdb CLI 跑同款 SQL。

---

## 二、缺口①：证据表去重

### 2.1 现状核实（生产库只读，2026-08-27 19:45）

- 重复 (question_id,url) 组：**55 组，每组恰 2 行，多余行 55**（7 天重预测重复入库，与报告 §2.7① 一致）
- 2345 行中 content 为空 **1915 行**；按来源：llm_websearch 1724/1724（设计即空 content，title 存 URL）、gdelt 191/191、crawler 0/430（全有正文）
- `source_documents` 现有索引：**无**
- `curated_documents`：0 行；生产库该表已注册 FK `(document_id) REFERENCES source_documents(id)`
- 55 组重复中 **23 组存在非空 content 行**——「保留 content 非空行」的清理策略有实际价值

### 2.2 修改（src/predictor/data/storage.py）

1. **写入路径 `INSERT OR IGNORE`**（add_document）：同 (question_id,url) 再插入时被唯一索引忽略，**返回既有行 id**（证据引用 evidence_ids 恒指向真实行）。DuckDB 1.5.5 实测：INSERT OR IGNORE 只吞唯一性冲突，NOT NULL/CHECK 违反仍抛异常，不会静默丢证据。
2. **create_schema 幂等迁移** `_migrate_source_documents_unique()`（每次启动执行，索引存在即早退）：
   - 按 (question_id,url) 分组（url IS NULL 不参与），`MIN_BY(id, content 是否非空)` 保留一行：content 非空优先、全空保最早；
   - curated_documents 对被清理行的引用先重定向到保留行（防御性，生产 0 行）；
   - `CREATE UNIQUE INDEX IF NOT EXISTS uq_source_documents_qid_url ON source_documents(question_id, url)`（DuckDB 不支持 ALTER TABLE ADD CONSTRAINT UNIQUE，唯一索引即唯一性落地方式）。

### 2.3 DuckDB 1.5.5 两个引擎 bug（实测定位，代码已规避）

1. **窗口函数批处理 bug**：迁移若用 `UPDATE...FROM + FIRST() OVER` 窗口形式写在 create_schema 的多语句串里，串第二次执行会抛 `INTERNAL Error: Attempting to dereference an optional pointer`（单独执行两次均正常）。→ 改用 Python 侧 `MIN_BY` 聚合 + 逐组 UPDATE/DELETE。
2. **幻影 FK 注册 bug**：旧库已存在「无 REFERENCES」的 curated_documents 时，批内带 `REFERENCES source_documents(id)` 的 `CREATE TABLE IF NOT EXISTS` 被跳过，随后对 source_documents 的**任何 DELETE**（连 `DELETE WHERE id <> ?`）都抛同款内部错误。四象限实测定位：旧表无 FK + 批内含 REFERENCES → 崩；旧表有 FK（生产库形态）→ 正常；批内去掉 REFERENCES → 正常。→ **批内 curated_documents DDL 移除 REFERENCES 子句**（DuckDB 默认不强制外键、代码未开 foreign_keys pragma，无行为影响；生产库已注册的 FK 保留不动）。

### 2.4 生产库执行方式

- 迁移代码只在 `create_schema`（启动/建库路径）中执行，**本次修复未对生产库写任何数据**（全程 read_only=True 核实）。
- 生产下次启动（用户择时）自动完成：清理 55 组重复 → 建唯一索引 → 之后重复入库被 IGNORE。
- 如需手动提前执行：停轮窗口用 duckdb CLI 连 data/foresight.db 执行报告附注 SQL（与 _migrate_source_documents_unique 等价）。

---

## 三、缺口②：dedup 宏观题族（src/predictor/selection/dedup.py）

### 3.1 根因核实（生产库只读）

#72「2026年9月美联储FOMC会议（9/15-16）会加息吗…」、#73「美联储9月会加息吗」、#74「美联储2026年9月FOMC会议会加息吗…」——旧标的表无 fed 键，且 #72 带日、#73/#74 只有月，旧日粒度日期抽取下三者签名全空/互异 → 三道并存。

### 3.2 设计

1. **标的表补宏观题族**（市场标的在前，宏观在后——"美联储决议后黄金会涨吗"的主语是黄金）：
   - 美联储/联储/FOMC/fed → `fed`；非农 → `nfp`；PMI → `pmi`；EIA → `eia`
   - CPI 走正则**国别消歧**：中国 CPI → `cn_cpi`、美国 CPI → `us_cpi`、裸 CPI → `cpi`（#54 美 CPI 与 #5/#7/#25 中 CPI 是不同事件，不得互判同题；"中国08月CPI"的"中国"与"cpi"隔月份也能正确归属）
2. **方向关键词按题族分表**：市场（涨跌价词）/宏观数据（高低增减词）/fed（加息·降息·维持不变→flat）。跨族不混用——"涨"与"加息"互判同题的错误路径被切断。
3. **否定句式方向抽取**：方向关键词前 ≤3 字符出现否定标记（不/未/没/难/无/停）→ 方向加 `_neg` 后缀。"不会加息"≠"加息"（up_neg ≠ up），且"不加息"≠"降息"（_neg 天然隔离）。排除"否"——"是否降息"仍是降息题；含"停"——"暂停加息"=不加息。
4. **日期粒度**：宏观月度题族（fed/CPI/非农/PMI）取月份集合（FOMC/CPI/PMI 均为月度事件，"9/15-16"与"9月"按月份才能判同题）；EIA 为周频事件保持日粒度（"9月10日当周"≠"9月17日当周"）。沿用"丢弃年份"惯例。

### 3.3 只读实测（生产库，修复后）

- `event_signature`：#72/#73/#74 全部 == `("fed","up",frozenset({"9"}))`
- `find_duplicate_question(st, "美联储2026年9月会加息吗")` → **73**（不存在的字面标题，签名命中）
- `find_duplicate_question(st, "美联储9月FOMC会议会降息吗")` → **47**（顺带识别 #47/#86 同为 9 月降息近义题）
- `find_duplicate_question(st, "2026年10月中国CPI同比会高于9月吗")` → **5**
- 保守取向保持：无绝对日期的模板题（"未来7天突破5150"）、"本周EIA"（无日期）仍不参与签名判重，无误判面

---

## 四、新增单测

- **tests/data/test_storage.py**（+4）：重复 URL 插入被忽略并复用 id；同 URL 不同题可并存；url NULL 不参与去重；NOT NULL 违反仍抛异常
- **tests/data/test_storage_migration.py**（+1）：旧库 3 行重复 (question_id,url) → create_schema 后保留 content 非空行、NULL url 行不动、curated 引用（指向被清行 id=3）重定向到保留行 id=2、唯一索引建立、再插入重复被忽略、二次 create_schema 幂等
- **tests/selection/test_dedup.py**（+8）：#72/#73/#74 判同题 + 签名月粒度；fed 方向（加息/降息/维持）与月份区分；否定句式（不会加息/是否降息/暂停加息）；CPI 国别消歧与近义命中；EIA 周频日粒度；市场题族否定句式回归

## 五、验证记录

- `ruff check`（5 个改动文件）：**All checks passed**
- 相关文件定向 pytest：tests/selection、tests/data、tests/test_storage_readonly、tests/calibration、tests/test_paired_arms、tests/inference/test_retrieve、tests/test_pipeline、tests/test_web_api_public、tests/test_predict_cli、tests/test_daily、tests/test_evolve、tests/test_websearch_predictor —— **全绿**
- 全量 `uv run pytest -q -x`：**438 用例 100% 通过（RC=0）**
- 生产库全程只读（read_only=True），无任何写操作

## 六、已知边界（有意保留，非缺陷）

1. **1915 行空 content 为存量事实，本修复不回填**：llm_websearch 1724 行设计即空 content（URL 级溯源）；gdelt 191 行空 content 属采集源问题，不在本次范围。唯一约束 + IGNORE 从机制上阻断「重复 URL 再入库」，内容质量问题另立议题。
2. 跨年同月宏观题（"2027年9月加息"与"2026年9月加息"同时未揭晓）理论上有漏判边界，沿用"丢弃年份"惯例，实际概率极低。
3. "本周EIA原油库存会下降吗"（#21/#27/#43 字面相同）无绝对日期，仍不参与签名判重——精确标题判重只对**新创建**生效，存量重复题需人工处置。
4. docs/STATUS.md 未同步（本次任务文件边界外，请父会话按惯例更新）。

## 七、改动文件清单

- `src/predictor/data/storage.py`：add_document 改 INSERT OR IGNORE + 复用既有 id；create_schema 末尾挂 `_migrate_source_documents_unique()`；curated_documents DDL 移除 REFERENCES（DuckDB 1.5.5 幻影 FK bug 规避）
- `src/predictor/selection/dedup.py`：宏观题族标的表 + CPI 国别消歧 + 题族分方向表 + 否定句式 `_neg` + 宏观月粒度日期
- `tests/data/test_storage.py`、`tests/data/test_storage_migration.py`、`tests/selection/test_dedup.py`：上述单测

## 附注：生产库手动清理 SQL（与代码等价，可选）

```sql
-- 停轮窗口执行；本 SQL 与 _migrate_source_documents_unique 同口径
UPDATE curated_documents SET document_id = g.keep_id
FROM (
    SELECT id AS old_id, FIRST(id) OVER w AS keep_id
    FROM source_documents WHERE url IS NOT NULL
    WINDOW w AS (PARTITION BY question_id, url
                 ORDER BY CASE WHEN content IS NULL OR content = '' THEN 1 ELSE 0 END, id)
) g
WHERE curated_documents.document_id = g.old_id AND g.old_id <> g.keep_id;
DELETE FROM source_documents WHERE id IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY question_id, url
            ORDER BY CASE WHEN content IS NULL OR content = '' THEN 1 ELSE 0 END, id
        ) rn FROM source_documents WHERE url IS NOT NULL
    ) WHERE rn > 1
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_documents_qid_url
    ON source_documents(question_id, url);
```
