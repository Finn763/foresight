# Foresight 今日四批改动审查：新问题/回归隐患（2026-08-27）

审查范围（今日四批）：
- `a297839`（19:08）P1 批2三修：序列轮级缓存+10路并发、LLM 采样并发、工具超时对齐、心跳、pm_resolve 调度
- `20a5a91`（20:28）P2 批次六修：backup_db、告警横幅、日志轮转、证据 UNIQUE 迁移、n_samples=3、题面消毒等（6 子代理并行）
- `a004fc1`（22:15）P2 批次五修：dashboard 守护、扩展桥 22 通道契约化（foresight-tools.ts 1832 行）、pm_fetch 调度
- `06cf0f4`（22:15）docs（STATUS 同步，无代码）

方法：逐函数 diff 新旧版本 + 直接审查现行文件 + 实测验证（tsc --noEmit 干净；`tests/data/test_storage_migration.py`、`tests/test_web_alert_banner.py`、`tests/test_alert_dedup.py`、`tests/test_backup_db.py`、`tests/test_run_silent_rotation.py`、`tests/test_ensure_dashboard.py`、`tests/test_websearch_predictor.py`、`tests/test_foresight_tools_contract.py` 共 76 用例全绿）。**纯审查：未改任何代码、未 commit。**

结论先行：五项目标中**无高危急漏洞**。真问题 3 项（迁移失败全链路阻塞、backup 撕裂副本风险、ack 无 CSRF），其余为边缘/备注级。契约重构本身执行语义零变化。

---

## ① 扩展桥契约重构（.foresight/extensions/foresight-tools.ts，1832 行）

**总体定性：安全（结构重构，零执行语义变化）。** 逐函数 diff `20a5a91`→`a004fc1`：`runReadJson`/`runPython`/`writeGate`/`_out` 信封 try-except 结构、全部 22 个工具体（含内联 Python）、全部超时值、写工具 argv 构造，与旧版逐字一致——仅是搬运进 `BRIDGE_CONTRACT`/`contractTool`/`readBody` 框架。tsc 编译零错误，契约测试绿。

逐项结论：

| 审查点 | 结论 | 证据 |
|---|---|---|
| 白名单过严拒合法调用 | **无**。prefix 匹配 `a === mod \|\| a.startsWith(mod+".")` 语义正确；22 通道参数白名单与 TypeBox schema 全部一致（tsc+契约测试绿）；health 的 `datetime.datetime`/`predictor.ops.probes.get_probes` 等前缀条目均正常通过 | foresight-tools.ts:594-606 |
| 错误语义变化 | **无**。`runReadJson` 的 try/except→`_out={'error':...}`、`runPython` 的 exit≠0 throw、clip 尺寸 20k，与旧版逐字相同 | ft-old.ts:108-131 vs 新版 124-147 |
| _out 信封兼容旧调用 | **兼容**。信封格式（单行 JSON、default=str、异常缺省 error dict）未变；`sys.path.insert(0, cwd/src)` 包装仍在 | 同上 |

边缘/备注（不构成安全边界——内联体是开发期常量，无用户输入）：

1. **readBody 的 `_out` 检查是纯文本包含**（`body.includes("_out")`，:602）：注释/字符串里出现 `_out` 即通过。自纪律机制，误导性而非漏洞。建议改为行级赋值正则 `^\s*_out\s*=`（一行改动）。
2. **import 白名单正则盲区**（:594）：`import a, b` 逗号形式只检查 `a`；`importlib.import_module`/`__import__` 动态导入不查。同类自纪律机制，当前 9 个内联体均未用这些形态。
3. **logs 工具仍读 GBK，与今天的新 utf-8 写入叠加成混合编码**（真·新引入的边缘）：run_silent（20a5a91:78）起新写日志为 `encoding="utf-8"`，旧 daily.log 是 GBK 且未转码；server.py 已在 a004fc1 改 `utf-8+replace` 读，但 TS `logs` 工具内联体仍 `encoding='gbk', errors='replace'`（foresight-tools.ts:734）→ 新追加的 utf-8 中文段经 GBK 解码成乱码。已知道路（fix-report 记"旧 GBK 段转码留人工"），但 TS 侧 reader 没跟上新编码。建议同步改 `utf-8, errors='replace'`。
4. 契约完备门 `assertBridgeContract()`（:609-629）在加载末双向断言，fail-fast 设计合理；`REGISTERED_TOOLS` 为模块级集合，若 pi loader 对同一模块实例二次执行 default export 会重复注册——旧版同构问题，非本次回归。

---

## ② storage.py 证据表迁移（_migrate_source_documents_unique）

**正确性核验通过**：`MIN_BY(id, CASE WHEN content 空 THEN 1 ELSE 0 END) GROUP BY (question_id,url) WHERE url IS NOT NULL`（storage.py:224-229）——content 非空优先保行、全空任意保行，语义与注释一致；NULL url 不参与（与 DuckDB 唯一索引 NULLS DISTINCT 默认一致）；逐组 UPDATE curated 重定向→DELETE→最后建索引，**幂等**（has_index 早退 :218-223；中断重跑收敛，迁移测试覆盖 88 行绿）。

竞态结论：**并发迁移实际不可能**——所有写入口先过 `evolve.lock`（health_check wait_acquire :254、evolve :301、autopick_ingest 同样排队），且 DuckDB Windows 独占锁保证同库双写连接无法并存（坑 8 实测）。重复启动幂等。

真问题：

1. **【真问题·影响面大·概率低】迁移失败会阻塞全部写入口**。`_migrate_source_documents_unique` 无任何 try/except，`create_schema` 无条件调用（:207），任何异常（磁盘满、杀毒锁文件、DuckDB 内部错误——本函数注释自述曾触发 1.5.5 "optional pointer" 内部错误才改为 Python 侧实现）→ create_schema 抛 → **daily/evolve/predict_cli/pm_fetch/pm_resolve/autopick_ingest/health_check 全部启动即崩**。当前设计是"宁可崩不静默"的 fail-fast 取向，但 blast radius 是全部生产轮次。建议：迁移整体包 try/except，失败落独立告警（复用 alerts 目录）并跳过迁移继续建库（唯一索引缺失不影响既有功能，只是去重约束延后）。
2. **【边缘·设计】监控进程是生产迁移的首个执行者**。health_check 以 read-write 连接 + `create_schema()`（health_check.py:261）入场，08-28 首个计划任务（最早 08:45 autopick_ingest 或 09:35 巡检）就会在生产库执行 DELETE 型迁移——监控工具执行数据删除。历史就 read-write（a297839 已是），但"删除"语义今天才引入。建议监控路径改用 read_only 连接（迁移由轮次进程完成），或显式接受并在 health_check 注释标注。
3. **【边缘】运行时写入与迁移口径不一致**：`add_document` 的 `INSERT OR IGNORE ... RETURNING id`（:440-460）在既有行为空 content、新行有正文时静默丢弃新正文、返回旧 id；迁移却是"content 非空优先"。爬虫同日重抓同一 URL 的更好内容会被丢弃。可接受但值得知晓；如在意可改 upsert 语义（content 非空则 UPDATE）。

备注：`question_id IS NULL + url 非 NULL` 的重复行会被迁移去重，但此后新插入同型行不被唯一索引拦截（NULL 列 NULLS DISTINCT）——语义小错位，实际写入方恒带 qid，无实害。

---

## ③ 采样并发（websearch_predictor asyncio.gather + n_samples=3）

**总体定性：安全（真问题 0）。**

- **异常路径无泄漏**：`_one` 捕获全部 Exception→返回 None→作废该采样（websearch_predictor.py:224-232）；`gather` 默认 return_exceptions=False 但 `_one` 永不抛 → gather 永不抛；3.13 下 CancelledError 属 BaseException 不被吞，但唯一取消源是外部杀进程，无泄漏面。
- **stdout 契约不污染**：`_heartbeat` 只写 `sys.stderr`（:163-170），TS `runPython` 分离捕获 stdout/stderr 且成功时只取 stdout 返回——predict 单行 JSON 契约（cli.py:223-228 `print(json.dumps(...))`）不受心跳影响。心跳写异常（pythonw 假句柄/管道关闭）被吞（:168）。
- **AsyncClient 无跨 loop 串扰**：`aresponses_create` 每次调用自建 `async with httpx.AsyncClient`（client.py:175-177），3 路并发各持独立连接池；同步封装 `responses_create`/`chat` 用 asyncio.run 不经嵌套。最坏时长 ≈ 120s×3 次 + 退避 ≈363s < 900s 工具超时，余量 2.3×。
- **asyncio.run 调用点安全（现状）**：`_sample`（:156-160）仅测试使用；`websearch_predict` 的 `asyncio.run(_run_all())` 调用方为 cli.py:116（同步 CLI）、daily.py:128（同步脚本）、tests——全部在同步上下文，无嵌套循环。

边缘：

1. **【边缘·潜伏】`_sample`/`websearch_predict` 禁止在既有事件循环内调用**：任何未来 async 调用点（如 dashboard 后台任务/新增 FastAPI 端点内联预测）会 RuntimeError（asyncio.run 不可嵌套），且该错误在 cli.py:128 会被吞成 `{"ok": False, "管线失败"}` 的伪失败。建议在函数入口加 `asyncio.get_running_loop()` 探测断言（`RuntimeError: 禁止在事件循环内调用`）或在 docstring 声明。
2. **【备注】心跳在工具路径不可见**：TS `predict` 工具成功时丢弃 stderr，15 分钟长调用中用户/agent 全程零进度反馈（心跳的 TUI 可见性初衷只在 `foresight -p` 直跑路径成立）。
3. **【备注】run_silent 合并 stdout+stderr 进同一日志**（run_silent.py:79-80）：心跳行与 JSON 输出在 daily.log 混排，人工 tail 阅读体验下降；不影响任何机器契约。

---

## ④ 告警横幅 + ack（server.py）

- **【真问题·安全·中低危】ack 无 CSRF 防护**。`POST /api/ops/alerts/ack`（server.py:244-255）无 token、无 Origin/Referer 校验；服务绑 127.0.0.1（web_server.py:22），但跨源表单提交（application/x-www-form-urlencoded，CORS 简单请求、无 preflight；Firefox/Safari 及 PNA 未落地环境不受 Private Network Access 限制）可从任意网页触达 loopback：恶意页面可静默 ack 告警（**掩盖健康告警**，且 ack 后同日同签名复发会新建文件、横幅重现——health_check.py:74-76 正确跳过 .ack，此点无碍），并可刷 `POST /api/ops/health/refresh`（:223-226，无限制触发后台探测任务，资源消耗）。建议：校验 `Origin`/`Referer` ∈ {127.0.0.1:8765 白名单} 或加 env 随机 token（单用户 dashboard，成本一行）。同批的 `ops_health_refresh` 同受此问题。
- **路径穿越：无**。ack 的文件名来自 `alerts_dir.glob("alert-*.md")` 结果的 basename（:36、:247-252），无用户可控拼接；`ops_log_files` name 白名单 ("daily","evolve")（:230）。**安全**。
- **注入 escape 完整**：横幅三个动态字段 title/detected/items 全部 `html.escape`（默认 quote=True，:79-91）后嵌入文本节点，无属性上下文注入面；测试 test_web_alert_banner 136 行绿。**安全**。
- 【备注】ack TOCTOU 无害（replace 失败吞 OSError）；同秒落盘的 error 文件与普通文件按名排序倒置（`-health-error` 的 `-`<`.`），横幅可能显示稍旧一条——纯外观。

---

## ⑤ ensure_dashboard / backup_db / 序列缓存

### ensure_dashboard.py（新文件，a004fc1）
- **命令注入：无**。`subprocess.Popen([str(PY), "-E", "-X", "utf8", str(SRV_SCRIPT), ...])` 列表形式无 shell（:92-101）；PY/SRV_SCRIPT 由 `__file__` 解析，argv 仅测试注入。
- 【边缘】并发双拉起竞态：两个 ensure 实例同时探测 down→各自拉起，第二个 bind 失败退出，`_wait_up` 因第一个存活而双双报成功——良性，30 分钟间隔下概率极低。
- 【备注】`main(venv=...)` 只校验不传导（launch 用模块级 `PY`），纯测试缝。
- 拉起后 30s 端口+HTTP 双验证、occupied 判失败 exit 1（:114-135），与 signsrv 同构，设计正确。

### backup_db.py（新文件，20a5a91）
- **【真问题·数据安全·边缘】copy2 直拷活库的撕裂副本风险**。`shutil.copy2(db_path, dest)` 不连接 DB、无一致性保证（:36）。02:30 名义上避开全部轮次，但轮次超时/手工 YOLO 运维/autopick 延迟入库时，若 DuckDB 在 Windows 的共享语义允许读拷贝（坑 8 的 IOException 是 DuckDB 锁管理器报错、发生在 OS 打开之后，说明 OS 层句柄可共享），拷贝可能成功但截获半写页/未 checkpoint 的 WAL——**备份文件静默损坏**（比 PermissionError 显式失败更糟）；且 WAL（foresight.db.wal）从不拷贝，遗留 WAL 的提交不进备份。注释"文件拷贝是唯一不受持锁轮次影响的方式"只对"不受拒绝"成立，对"内容一致"不成立。建议：备份前探测 `data/evolve.lock` 空闲（wait_acquire 短超时或直接跳过当日备份），或拷贝时连同 .wal 一起拷并做备份后 open 验证。
- 【备注】同名 `-HHMM` 两次同分钟运行互相覆盖（间隔 30min，概率极低）；prune 文件名切片 `f.name[10:18]` 与格式匹配（:48）。

### 序列轮级缓存（historical.py，a297839）
- **【边缘】缓存返回同一可变对象**：`fetch_series_map` 命中时返回缓存 dict 本体（:245-246），无拷贝。**当前零实害**——实测全部调用方纯读：`compute_baseline` 全族（baselines.py:152-368 切片/遍历，无 sort/append/pop）、`build_series_context`（:272-312 只读）。但任何未来调用方对返回值 append/sort/改行会污染同日内所有后续题。建议返回深拷贝（代价：每轮 10 序列 ×~2700 行 dict，深拷贝毫秒级）或 docstring 加"返回值只读"契约。
- 【备注】缓存 key 日粒度：同日第二次调用不刷新当日盘中最新价（设计选择，注释已声明）；ThreadPoolExecutor 并发拉取中单序列失败降级 `[]` 且不写缓存（:262-264），语义保留。

---

## 附带观察（不在五项目标内，仅一句话）

- dedup.py 宏观题族扩展（20a5a91）方向保守（宁可漏判不误判），纯函数无副作用；isotonic/auto_resolve/update-rule 改动有配套测试且全量绿，本次未深审。
- `test_storage_readonly` 等历史用例与迁移叠加后仍绿，未见回归。

## 建议优先级

1. **backup_db**：加 evolve.lock 空闲探测（或 skip 当日备份）+ 备份后 `duckdb.connect(read_only=True)` 验证可读——防静默撕裂副本。
2. **ack CSRF**：Origin/Referer 白名单或 token。
3. **迁移兜底**：`_migrate_source_documents_unique` 包 try/except + 失败落告警不阻塞 create_schema。
4. 顺手项：TS `logs` 工具编码改 utf-8+replace；readBody `_out` 改行级正则；`websearch_predict`/`_sample` 加事件循环探测断言；`fetch_series_map` 返回深拷贝。
