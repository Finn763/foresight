# 人工揭晓调研报告：4 题真实结果核实（2026-08-27）

> 任务性质：**只读调研**。未写 DB、未改 `data/resolutions.csv`、未执行 `resolve.py`。
> 产出文件：本报告 + `data/resolutions.draft.csv`（草稿，供用户拍板后自行并入）。
> 数据判定最终由用户拍板，本报告只收集证据 + 给出建议。

---

## 〇、CSV 格式（依据 scripts/resolve.py 读取逻辑）

`scripts/resolve.py::load_resolutions_csv` 要求 CSV 至少含三列（缺失会直接报错）：

| 列 | 含义 | 取值 |
|---|---|---|
| `id` | 题目 id | int |
| `outcome` | 判定结果 | `1`/`0`/`true`/`false`（其他值整行跳过） |
| `source` | 证据来源文字 | string |

- 已揭晓的题（`q.outcome is not None`）会被 resolve.py 跳过并提示「已揭晓」。
- A 类自动题未到数据窗口也会被拒填（本批 4 题无 A 类在窗口内，不受影响）。
- 草稿额外加了第 4 列 `evidence`（URL/细节），DictReader 会忽略多余列，**不影响兼容性**；并入正式 resolutions.csv 时若想保持三列可删掉 evidence 列。

---

## 一、DB 现状（read_only 读取，2026-08-27 21:00 左右）

| id | 标题（完整） | closes_at | resolution_class | resolution_spec | outcome 现状 |
|---|---|---|---|---|---|
| 9 | 本周 EIA 原油库存会下降吗 | 2026-08-19 00:00 | B | `{"class": "B"}` | **False**（2026-08-20 16:30，llm_websearch）|
| 69 | 明天COMEX黄金收盘会高于今天吗 | 2026-08-13 18:46:31 | C | `{"class":"C","instrument":"gold","source_primary":"sina","compare_symbol":"hf_GC","condition":"gt_prev_close","close_timezone":"America/New_York","grace_days":3,"degrade_to":"C"}` | **True**（2026-08-20 15:31 人工，双源交叉）|
| 93 | 伦敦金现（XAU/USD）下周是否上涨：2026年8月21日（下周五）收盘价高于2026年8月14日（本周五）收盘价即算涨，数据源以公开行情（金十数据/伦敦金银市场）为准 | 2026-08-21 00:00 | **null** | **null**（criteria 即标题文字）| **None（待揭晓）** |
| 97 | 2026年8月19日（美东时间）道琼斯工业平均指数收盘点位会高于2026年8月18日收盘点位吗（判定口径：以官方/权威行情源的每日收盘价为准，19日收盘 > 18日收盘即成立） | 2026-08-20 00:00 | **null** | **null**（criteria 即标题文字）| **None（待揭晓）** |

预测记录：`#9 p=0.320`（8-11 22:55）、`#69 p=0.560`（8-12 19:12）、`#93 p=0.645`（8-15 23:55）、`#97 p=0.475`（8-17 17:12）。

**重要修正（与任务简报的出入）**：任务简报以为 4 题均待揭晓，实际 **#9 与 #69 已揭晓**（分别 8-20 16:30 / 8-20 15:31），真正待揭晓的只有 **#93、#97**。`data/resolutions.template.csv` 里待填的正是 75/93/97（75 不在本任务范围）。

---

## 二、逐题核实

### #9 「本周 EIA 原油库存会下降吗」（已揭晓 False，本次复核）

**Criteria**：resolution_spec = `{"class":"B"}`（无细化规则，判定口径即题面「EIA 原油库存下降」；行业惯例指**商业原油库存（不含战略储备 SPR）**，即 EIA 每周三发布的 WPSR 头条数字）。

**窗口**：建题 8-11 21:34 北京时间，closes_at 8-19 00:00（= 8-18 12:00 ET）。窗口内可判定的 EIA 周报 = **8-12 发布**（覆盖 8-7 当周）。8-19 发布的报告（覆盖 8-14 当周）于 8-19 10:30 ET（北京时间 22:30）发布，**晚于题目关闭**——但两期结果一致，不改变判定。

**官方一手数据（EIA eia.gov）**：
- 序列 `WCESTUS1`（商业库存，不含 SPR，千桶）：7/31 = 406,987 → **8/7 = 424,410（+17,423）** → **8/14 = 428,815（+4,405）** → 8/21 = 428,910（+95，超出窗口）
  - URL: https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=WCESTUS1&f=W（Release Date 8/26/2026，含修订）
- EIA 周报 highlights PDF（8-12 发布版）原文：「U.S. commercial crude oil inventories (excluding those in the SPR) increased by 17.4 million barrels from the previous week. At 424.4 million barrels…」
  - URL: https://www.eia.gov/petroleum/supply/weekly/pdf/highlights.pdf
- 参考：`WCRSTUS1`（含 SPR 总量）8/7 = 723,104、8/14 = 722,241；`WCSSTUS1`（SPR 单独）8/7 = 298,694、8/14 = 293,426。

**二手佐证**：Anadolu Agency（8-20，转述 EIA）「+4.4M 至 428.8M」；Rigzone（8-20）；investing.com 经济日历（8-12 发布 actual +17.423M vs 预期 -1.7M；8-19 发布 +4.405M）。

**判定**：窗口内两期报告均为**增库** → 「库存下降」不成立 → **False**。与 DB 现有 outcome=False 一致 ✅。
**置信度**：高（官方一手 + 多源一致）。Brier（p=0.320, False）= 0.102。

---

### #69 「明天COMEX黄金收盘会高于今天吗」（已揭晓 True，本次发现疑似窗口错位 ⚠️）

**Criteria（resolution_spec 全文）**：
```json
{"class":"C","instrument":"gold","source_primary":"sina","compare_symbol":"hf_GC",
 "condition":"gt_prev_close","close_timezone":"America/New_York","grace_days":3,"degrade_to":"C"}
```
题面「明天收盘高于今天」：建题 8-12 18:47 北京时间（= 8-12 06:47 ET，8-12 美盘未开），closes_at 8-13 18:46（= 8-13 06:46 ET）。**题意窗口 = 8-13 收盘 vs 8-12 收盘**（condition: gt_prev_close）。

**窗口惯例验证（同模板题 #67）**：「明天标普 500 收盘会高于今天吗」（建题同为 8-12，closes 8-13 09:00）DB outcome=True。S&P 500 收盘：8-12 = 7,748.50 → 8-13 = 7,798.99（涨）→ True 恰好对应 **8-13 vs 8-12** 窗口。若该题用的是 8-14 vs 8-13（7,785.76 < 7,798.99，跌）会是 False。**结论：模板惯例 = 建题日 D 比较 D+1 收盘 vs D 收盘。** #69 应比 8-13 vs 8-12。

**官方口径数据（COMEX 近月 8 月合约结算价）**：
| 日期 | 结算价 | 来源 |
|---|---|---|
| 8-12 | **4,408.90**（+0.59%）| Morningstar 转 Dow Jones Market Data/FactSet（Data Talk 202608129053）；Yahoo GC=F 日线一致 |
| 8-13 | **4,363.60**（**-1.03%**）| WSJ 标题「Comex Gold Settles 1.03% Lower at $4363.60」；Yahoo GC=F 日线一致；与 8-17 Data Talk「两日累计 +54.20」反推（4417.80-54.20=4363.60）吻合 |
| 8-14 | 4,380.40（+0.39%）| Yahoo GC=F；8-17 Data Talk「连续两日上涨」吻合 |

→ **8-13（4,363.60）< 8-12（4,408.90）→ 按 criteria 应为 False。**

**现有揭晓为何是 True？** DB 的 resolution_source：「双源交叉：新浪K线 GC 8-13收4406.7/8-14收4431.9；每经(新浪财经)8-14收4432.00；金投网昨收4407.1」——该证据链比较的是 **8-14 vs 8-13**（涨），比题意窗口**错位一天**。且新浪 hf_GC / 金投网 / 每经的数值（8-13≈4406.7-4407.1、8-14≈4431.9-4432.0）与官方近月合约结算（4363.60 / 4380.40）不符，疑为中国门户展示的**12 月合约（GCZ26）**：Yahoo 8-13 早报「Gold (GC=F) December futures opened at $4,468.80，与周三收盘持平」→ 12 月合约 8-12 收 ≈4,468.8。**即便用新浪的 12 月合约口径，8-13（4,406.7）仍低于 8-12（≈4,468.8）→ 同样应为 False。**（两种合约口径下 8-13 都跌。）

**建议**：**改判 False**。依据：官方结算 + 模板惯例双确认；现有 True 的判据窗口错位一天。但改判属纠错性质，且该题 8-20 已人工揭晓（数据已进 model_stats/校准器），**是否回改由用户拍板**。若用户维持现有解释（8-14 vs 8-13），则维持 True——请用户明示口径。
**置信度**：数值与窗口解释均高；唯一不确定性在「用户是否认可纠错」。
Brier：若 True 维持 = 0.194（p=0.56）；若改 False = 0.314。

---

### #93 「伦敦金现（XAU/USD）下周是否上涨」（待揭晓 ✅ 可判）

**Criteria（resolution_spec=null，判定口径即标题）**：2026-08-21（周五）收盘价 高于 2026-08-14（周五）收盘价 即 True；数据源以公开行情（金十数据 / **伦敦金银市场**）为准。

**官方一手数据（LBMA 伦敦金银市场协会官方定价）**：
- 8-14：AM 4,349.90 / **PM 4,390.70**
- 8-21：AM 4,581.95 / **PM 4,582.10**
- URL：https://prices.lbma.org.uk/json/gold_pm.json 与 gold_am.json（LBMA 官网数据接口，覆盖 1968 至今全史）；官网页 https://www.lbma.org.uk/prices-and-data/precious-metal-prices
- 口径说明：LBMA 每日两次定价（伦敦 10:30 AM / 15:00 PM），无单一「收盘」概念。以 PM 计：**8-21 比 8-14 高 +191.40（+4.36%）**；以 AM 计：+232.05（+5.33%）。**两口径均为大涨，无歧义。**

**二手佐证（现货 XAU/USD 收盘口径）**：8-14 spot ≈ 4,375.50（Trading Economics CFD）；8-21 ≈ 4,600-4,624（PMI 4,620.54；Yahoo GC=F 8-21 收 4,624.10；WSJ 周报「Comex Gold Ends the Week 5.56% Higher at $4624.10」）。现货口径同样大涨。

**判定**：8-21 收盘显著高于 8-14 收盘 → **True**。
**置信度**：高（LBMA 官方一手；AM/PM、现货、期货多口径同向，价差 +4% 以上远超数据源误差）。
Brier（p=0.645, True）= 0.126。

---

### #97 「2026-08-19（美东）道指收盘 高于 08-18 收盘」（待揭晓 ✅ 可判）

**Criteria（resolution_spec=null，判定口径即标题）**：官方/权威行情源每日收盘价；19 日收盘 > 18 日收盘即 True。

**数据（多源一致，权威二手）**：
| 日期 | DJIA 收盘 | 来源 |
|---|---|---|
| 8-18 | **53,343.40** | Yahoo Finance ^DJI 历史；WSJ DJIA historical prices 表（53343.40）|
| 8-19 | **53,463.05**（+119.65，+0.22%）| Yahoo ^DJI；WSJ 表；AP/ABC 收盘电讯「The Dow Jones Industrial Average rose 119.65 points, or 0.2%, to 53,463.05」；CNBC 8-19 报道 |

**判定**：53,463.05 > 53,343.40 → **True**。
**置信度**：高（4 家独立源数值一致）。S&P Dow Jones Indices 官方页需订阅未直连，属唯一小缺口，但多源一致可覆盖。
Brier（p=0.475, True）= 0.276。

---

## 三、判定汇总

| id | 建议 outcome | 置信度 | 状态 | 关键数值 |
|---|---|---|---|---|
| 9 | **False**（与现有揭晓一致）| 高 | 已揭晓，复核通过 | 8/7 当周 +17,423 千桶；8/14 当周 +4,405 千桶（均增库）|
| 69 | **False（建议改判）** | 高（需用户拍板）| 已揭晓 True，窗口疑似错位 | 8-12 收 4,408.90 → 8-13 收 4,363.60（-1.03%）|
| 93 | **True** | 高 | **待揭晓（可判）** | LBMA PM 4,390.70 → 4,582.10（+4.36%）|
| 97 | **True** | 高 | **待揭晓（可判）** | 道指 53,343.40 → 53,463.05（+0.22%）|

---

## 四、诚实声明：数据源分级

**官方一手数据**：
- #9：eia.gov LeafHandler（WCESTUS1/WCRSTUS1/WCSSTUS1）+ EIA WPSR highlights PDF ✅
- #93：LBMA 官方定价 JSON（prices.lbma.org.uk）✅
- #69：官方口径结算价由 Dow Jones Market Data/FactSet（Morningstar Data Talk 转载）与 WSJ 结算标题给出，Yahoo GC=F 日线一致——**交易所（CME）原始结算单未直连**，属「权威转述 + 多源交叉」

**二手转述（佐证用）**：
- #9：Anadolu AA、Rigzone、investing.com 经济日历（均转述 EIA）
- #93：GoldSilver 伦敦定价表、Trading Economics、PMI、Fortune、Yahoo/WSJ 期货行情
- #97：Yahoo、WSJ、AP/ABC、CNBC（官方指数商 S&P DJI 页需订阅未直连，以四源一致为准）

**无法确定/需用户拍板**：
- #69：数据与窗口解释均高置信（官方口径 8-13 下跌无争议），但**是否把已揭晓的 True 改判为 False 属决策**，取决于用户是否认可「模板惯例 = 建题日 D 比较 D+1 vs D」与「新浪 12 月合约口径同样支持 False」这两点。
- #93 的「收盘价」时点：题面未指定。LBMA 无单一收盘价，报告以 PM 定价为主判定（题面点名伦敦金银市场）；若用户偏好金十 XAU/USD 美东收盘口径，结论不变（同样大涨）。

## 五、草稿使用说明

- 文件：`data/resolutions.draft.csv`（4 行，列 id/outcome/source/evidence）。
- **#93、#97 两行可直接并入 resolutions.csv 执行**（execute resolve.py 由用户自行操作，本任务未执行）。
- **#9、#69 两行仅供复核/纠错参考**：DB 已揭晓，即使并入也会被 resolve.py 跳过。若用户拍板改判 #69 → False，需要另外的改判手段（如后续人工修正脚本，不在本任务范围）。
- 并入时注意：不要覆盖 `data/resolutions.csv` 里现有的 #69 行（那是已消费的历史记录）。
