# CC 评审 §2.2 修复报告：统计基线关键词映射系统性错误（2026-08-27）

**范围**：CC 全面改进分析报告 §2.2（P1）四个已核实问题。
**结论**：全部修复完成，相关测试 64 用例全绿，ruff 干净，DB 只读实测新旧注入对比证据见 §4。

---

## 1. 问题与根因（修复前）

`src/predictor/stats/baselines.py` 的关键词分类 + 窗口解析是基线注入的唯一入口。
基线经 `websearch_predictor.py` 注入 instructions（【统计基线】块）且
`inference/forecast.py` 提示「以它为起点，除非当前证据明确偏离」（强锚定）——
错误锚点每天污染 28 次生产预测的 LLM 输入。

| # | 问题 | 根因 | 影响题例（DB 实测） |
|---|---|---|---|
| 1 | 错序列 | `cpi_mom` 模式 `中国.*CPI\|CPI.*同比` 第二支不要求中国 | #54「美国 CPI 同比」注入中国 CPI 序列（CHNCPIALLMINMEI）频率 0.486 |
| 2 | 错窗口 | 窗口解析只认 `(\d+)\s*天`，其余回退默认 7/30 天 | #6「年底前升破 7.0」（126 天）注入 30 天频率；#64（157 天）注入 7 天频率 |
| 3 | 错方向 | `ffr_meeting` 只分「降息/其余」，「加息」题落入「维持不变」分支 | #72/#73/#74「9 月会加息吗」注入「维持」历史频率 0.764（方向性误导锚点） |
| 4 | 错算法 | 「站上/突破 N 点」阈值题命中 `标普\|S&P\|创新高` 走创新高算法 | #64「标普首次站上 8500」走 7 天创新高频率而非 8500 阈值 breakout |

---

## 2. 修复明细

### 2.1 `src/predictor/stats/baselines.py`（核心）

**① cpi_mom 要求「中国」（问题 1）**
```
旧: (re.compile(r"中国.*CPI|CPI.*同比"), "cpi_mom")
新: (re.compile(r"中国.*CPI|CPI.*同比.*中国"), "cpi_mom")   # 两支都要求中国
```

**② 窗口解析重写（问题 2）**
- 新增 `_parse_title_window(title, now)`：按序识别
  绝对日期（`2027年1月31日前`）→ 带年份月底（`2026年10月底前`）→ 带年份年底
  （`2026年底前`）→ 月-日（`9月30日前`，已过取明年）→ 月底（`10月底前`/`月底前`）
  → 数量单位（N 天/周/月/年）→ 裸年底/年内。
- 新增 `_resolve_window(title, now, closes_at)`：
  - 题面可解析且 1~90 天 → 用之（题面优先于 closes_at）；
  - 题面无措辞 → 回退 `closes_at - now`（生产题全有 closes_at，窗口即真实剩余期）；
  - 无法解析或 **>90 天 → None**（CC 建议宁缺毋滥：不注入错误频率）。
- 全部窗口类基线（sp500_high/usdcnh_7/gold/brent/cny/sp500_break）不再有硬编码
  默认窗口；窗口为 None 时整条基线不注入。

**③ ffr_meeting 三向分支 + 收紧语境（问题 3）**
```
旧模式: 美联储|FOMC|维持利率|降息        （"某国央行降息"也会误注入 DFF）
新模式: 美联储|FOMC                     （只认美联储语境）
旧分支: "降息" in title → down, 否则 hold
新分支: 加息|升息|上调 → up；降息|下调 → down；维持|不变|按兵不动 → hold；
        方向不明 → None（宁缺毋滥）
```
`baseline_ffr_meeting` 增加 `up` 计数（diff > +5bp）。

**④ 标普阈值题走 breakout（问题 4）**
```
新增: (标普|S&P|SPX|标准普尔).*(站上|突破|升破|触及)\s*(\d+...) → sp500_break
      （排在 sp500_high 之前；compute_baseline 走 baseline_breakout + 阈值参数）
收紧: sp500_high 改为 (标普|S&P|SPX|标准普尔).*(创新高|新高)
      （裸"创新高"如"淘宝成交额创历史新高"不再映射到标普序列）
```

### 2.2 调用方接线（窗口解析需要 now/closes_at）

| 文件 | 改动 |
|---|---|
| `src/predictor/websearch_predictor.py` | `_load_baseline(title, now, closes_at)`；`predict_with_websearch` 传 `q.closes_at` |
| `src/predictor/pipeline.py` | `compute_baseline(q.title, sm, now=now, closes_at=q.closes_at)` |
| `src/predictor/cli.py` | 同上（`closes` 变量） |
| `scripts/compare_backtest.py` | 同上（`now=pred_date, closes_at=q.closes_at`，窗口=回测假设的 30 天） |
| `scripts/evolve.py` | `_build_base_rates` 新增 `_CANONICAL_TITLES`：bare key「标普」无窗口措辞，改用规范题面「未来7天内标普500会创新高吗」计算族基线（difficulty_tier key 语义不变） |

`compute_baseline` 新增关键字参数 `now`/`closes_at`（均可空，向后兼容旧调用）。

---

## 3. 单元测试

`tests/stats/test_baselines.py` 新增 6 个回归测试（+2 个解析直测）：
- `test_cpi_requires_china` — 中国 CPI 照常 18/35；「美国 CPI 同比」两变体 → None
- `test_parse_title_window_units` / `test_parse_title_window_calendar` — 天/周/月/年、
  年底 126 天、绝对日期 157 天、带年份月底 65 天、月-日跨年 308 天、裸月底、无措辞 → None
- `test_window_over_90_days_returns_none` — #6/#64 题面原样 → None；closes_at 兜底 30 天
  生效且同样受 90 天上限约束；题面 30 天优先于 closes_at 60 天
- `test_ffr_three_way_direction` — 加息 6/36、降息 10/36、维持 20/36（合成月频序列
  精确断言）；方向不明 → None；「中国人民银行会降息吗」→ None（不再注入 DFF）
- `test_sp500_threshold_uses_breakout` — 「站上 6500」走 breakout（245/479 精确断言，
  threshold/window 校验）；「创新高」仍走 sp500_high；裸「创历史新高」→ None

`tests/test_evolve.py` 新增 `test_build_base_rates_canonical_titles` —
monkeypatch 序列后验证「标普」族基线仍产出（规范题面接线生效）。

**运行结果**（相关文件定向，`-p no:cacheprovider` 避免并行任务互踩）：
- `tests/stats/test_baselines.py + tests/test_evolve.py`：**31 passed**
- `tests/test_websearch_predictor.py + tests/test_pipeline.py + tests/inference/test_forecast.py
  + tests/test_predict_cli.py + tests/test_daily.py + tests/test_cli_interactive.py`：**33 passed**
- `ruff check`（8 个改动文件）：**All checks passed!**

---

## 4. DB 只读抽查证据（真实行情序列，now=2026-08-27 09:00）

用 `duckdb.connect(read_only=True)`（锁重试）取题面；序列真实拉取
（sp500=2677 / usdcnh=2771 / cpi_cn=112 / ffr=3890 根）；「旧」= 修复前逻辑内联复刻。

| 题 | 旧注入（修复前） | 新注入（修复后） |
|---|---|---|
| #54 美国 CPI 同比 | cpi_mom **0.486**（中国 CPI，n=111） | **None（不注入）** ✓ |
| #6 年底前升破 7.0 | cny_below **0.700** / window=30 | **None**（年底口径 126 天 >90） ✓ |
| #64 标普首次站上 8500 | sp500_high **0.324** / window=7 | **None**（绝对日期 157 天 >90） ✓ |
| #72 FOMC 会加息吗 | ffr 维持 **0.764** | ffr **上调 0.150**（19/127 月） ✓ |
| #73 美联储9月会加息吗 | ffr 维持 **0.764** | ffr **上调 0.150** ✓ |
| #74 FOMC 会加息吗（上调区间） | ffr 维持 **0.764** | ffr **上调 0.150** ✓ |

**回归抽查（正确路径不得被过度收紧）**：

| 题例 | 结果 |
|---|---|
| #16 型 未来7天标普创新高 | sp500_high 0.324 / window=7 ✓ |
| #12 型 未来30天升破 7.0 | cny_below 0.700 / window=30 ✓ |
| #3 型 FOMC 维持不变 | ffr 维持 0.764 ✓ |
| #47 型 FOMC 降息 | ffr 下调 0.087 ✓ |
| #56 型 10月底前布伦特突破 95 | brent_break 0.162 / **window=65**（原 30）✓ |
| #7 型 中国 CPI 同比 | cpi_mom 0.486 ✓ |

---

## 5. 残余风险（不在本任务范围，建议后续）

1. **>90 天题无基线**：#6/#64/#62/#60/#66 类长窗口题按宁缺毋滥不再注入基线——
   这是报告建议的预期行为，但 LLM 失去外部锚点，后续可考虑「窗口归一化频率」
   （历史同长度窗口的频率，样本会更少）。
2. **#107 类**「Kevin Warsh 是否呼吁美联储降息」含「美联储+降息」→ 注入 Fed 下调
   频率——题面问的是个人表态而非利率本身，需要「呼吁/表示/认为」类负向模式排除。
3. **#53 类**「国际金价（COMEX）会突破 5000」——`黄金.*突破` 模式不匹配「金价」，
   未命中基线（非本次四个问题，属覆盖率缺口）。
4. **否定句式**「美联储9月不会加息吗」→ 仍判「加息」方向（up 频率），
   未做「不/否」负向处理。
5. `docs/STATUS.md` 未同步（避免与并行修复会话文件冲突，留给主会话收口）。

---

## 6. 改动文件清单

| 文件 | 类型 |
|---|---|
| `src/predictor/stats/baselines.py` | 修（4 处核心 + 窗口解析新函数） |
| `src/predictor/websearch_predictor.py` | 修（_load_baseline 接线） |
| `src/predictor/pipeline.py` | 修（接线） |
| `src/predictor/cli.py` | 修（接线） |
| `scripts/compare_backtest.py` | 修（接线；与并行会话的 §2.1 重构合并后验证无冲突） |
| `scripts/evolve.py` | 修（规范题面） |
| `tests/stats/test_baselines.py` | 增（8 个测试） |
| `tests/test_evolve.py` | 增（1 个测试） |

约束遵守：未 commit；未碰 `.env`、`shell/pi/`、`.foresight/SYSTEM.md`；DB 全程只读
（read_only=True，未写入）。
