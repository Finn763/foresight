# Foresight 前端 XSS 修复报告（2026-08-27）

对应 CC 评审报告 §5.2（P1，「前端 XSS 修复（3 处已核实）+ vitest 骨架」中 2h 核心部分）。
修复范围：`src/predictor/web/static/app.js` + `index.html`。**纯前端改动，未碰 Python 引擎 / shell/pi / .env，未 commit。**

## 结论

- 5 处 XSS 薄弱点全部修复（评审核实 3 处 + resolution_class 2 处 + CSP 兜底），`node --check` 通过，38 项纯 JS 断言全绿，web_server 回归抽查（127.0.0.1:8765）全部 200 无回归。
- 视觉与功能不变：所有改动均是对正常数据输出完全一致的「值插值处包一层转义」；唯一可见语义差异是恶意协议 URL 从可点击变为 `#`（这正是修复目标）。

---

## 1. 修复明细（5 处 + CSP）

### 1.1 app.js:115 存储型 XSS（评审核实第 1 处）

**前**：`<dd class="spec">${d.resolution_spec ? JSON.stringify(d.resolution_spec) : "—"}</dd>`
**后**：`<dd class="spec">${d.resolution_spec ? esc(JSON.stringify(d.resolution_spec)) : "—"}</dd>`

`JSON.stringify` 不转义 `<`，LLM 生成的 resolution_spec 入库后回显可夹带 `</dd><img onerror=...>` 破坏 DOM。现走文件内既有 `esc()`（textContent→innerHTML 实体化）。正常 JSON 不含 HTML 元字符时输出逐字节不变。

### 1.2 app.js:120 href 协议白名单（评审核实第 2 处）

**前**：`href="${esc(doc.url || "#")}"` —— `esc` 只转义实体不过滤协议，`javascript:` 原样通过（doc.url 来自 LLM 引用提取）。
**后**：新增 `safeHref(url)` 助手并替换调用：

```js
function safeHref(url) {
  if (!url) return "#";
  let p;
  try { p = new URL(url, location.origin); } catch { return "#"; }
  if (p.protocol !== "http:" && p.protocol !== "https:") return "#";
  return escAttr(url);
}
```

- 仅放行 http/https 与相对路径（相对路径经 `location.origin` 解析后协议必为 http/https）；`javascript:`（含大小写混淆 `JaVaScRiPt:`、前导空白、NUL 前缀）、`data:`、`vbscript:`、`file:` 一律回落 `#`。
- 返回值继续走 `escAttr`（比原 `esc` 多补 `"` 转义），防 URL 内嵌引号做属性逃逸。
- 取舍：`mailto:` 等非 http(s) 协议同样回落 `#`（当前数据无此类 URL，安全优先）。

### 1.3 app.js:74 反射型回填（评审核实第 3 处）

**前**：`value="${f.q}"`（引号可破坏属性）
**后**：`value="${escAttr(f.q)}"`。正常关键词输出不变。

### 1.4 app.js:86 看板 resolution_class（评审第 4 处）

**前**：`${q.resolution_class ?? "—"}`
**后**：`${q.resolution_class ? esc(q.resolution_class) : "—"}`

语义微调：空串 `""` 原先渲染空徽章，现显示 `—`（更合理，正常数据无差异）。

### 1.5 app.js:111 详情 resolution_class（评审第 4 处）

**前**：`<span class="badge cls">${d.resolution_class}</span>`
**后**：`<span class="badge cls">${esc(d.resolution_class)}</span>`

### 1.6 index.html 新增 CSP meta

```html
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'">
```

**取舍说明**（比任务要求的兜底版 `script-src 'self' 'unsafe-inline'` 更严格）：

- `script-src 'self'` **不加 'unsafe-inline'**：index.html 无内联 `<script>`；app.js 全部事件绑定走 `addEventListener`/属性赋值（已 grep 核实无 `eval`/`new Function`/内联事件属性/`javascript:` 用法）→ 严格版不破坏任何功能。
- `style-src 'self' 'unsafe-inline'` **必须保留 unsafe-inline**：app.js 渲染内联 `style="width:…%"`（概率条）与 `style="height:…px"`（分桶柱），改为类名方案有视觉回归风险，接受此权衡。
- `img-src 'self' data:`：favicon 是 data: URI。
- `object-src 'none'`：任务要求的兜底，禁插件/嵌入。
- `base-uri 'none'`：防 `<base>` 注入劫持相对路径。
- meta 形式 CSP 的限制：`frame-ancestors`/`report-uri` 等指令在 meta 中无效，只能由服务端响应头提供（web_server.py 改造超出本次范围）；meta CSP 存在时浏览器会与响应头 CSP 取交集，两者兼容。

---

## 2. 验证证据

### 2.1 语法

```
$ node --check src/predictor/web/static/app.js   → SYNTAX_OK
$ node --check scripts/test_xss_helpers.js        → 通过
```

### 2.2 纯 JS 断言脚本（新增 `scripts/test_xss_helpers.js`，零依赖，`node scripts/test_xss_helpers.js`）

**38/38 通过**，三层覆盖（不使用 pytest：前端纯 JS，Node 直跑更贴近真实运行环境；脚本已入 `scripts/` 可随时重跑）：

1. **行为层（24 项）**：从 app.js **提取 esc/escAttr/safeHref 的真实函数源码**（花括号计数 + 字符串/注释/正则字面量跳过），注入最小 DOM/location stub（模拟浏览器 textContent→innerHTML 序列化语义：文本节点转义 `& < >`）后断言：
   - esc 转义实体、中和 `</dd><img onerror=…>` 标签破坏、L115 场景（JSON 化 payload）无 `<` 注入；
   - escAttr 转 `&quot;`、中和 `" onfocus="…` 属性逃逸、L74 场景 payload 无属性/标签注入；
   - safeHref 拦截 7 种恶意协议（含大小写混淆/前导空白/NUL 前缀）、放行 7 种合法 URL（http/https/绝对路径/相对路径/协议相对）、空值回落 `#`、https URL 内嵌引号仍做属性转义。
2. **调用点层（8 项，防回退）**：断言 5 处修复后的模板写法仍在 app.js，且旧写法（`value="${f.q}"`、`href="${esc(doc.url`、`JSON.stringify` 直插）已清除。
3. **CSP 层（6 项）**：index.html 含 CSP meta；script-src 严格无 'unsafe-inline'；object-src 'none'；style-src 保留 unsafe-inline；img-src 放行 data:；base-uri 'none'。

### 2.3 web_server 回归抽查（127.0.0.1:8765，2026-08-27 10:24 实测）

8765 已有本项目 uvicorn 实例在跑（server 头确认为 uvicorn，API 形状与项目一致），**直接复用以避免端口冲突**；静态文件为磁盘实时读取，修改即生效：

| 抽查项 | 结果 |
|---|---|
| `GET /` | 200，返回 HTML 已含新 CSP meta ✓ |
| `GET /static/app.js` | 含全部 5 处修复关键词（safeHref/escAttr(f.q)/esc(JSON.stringify…)/esc(q.resolution_class)/esc(d.resolution_class)）✓ |
| `GET /api/questions` | 200，22,912 字节正常列表 ✓ |
| `GET /api/questions/67`（详情） | 200，resolution_spec 为对象、documents/evidence 字段正常 ✓ |
| `GET /api/questions?q=%22%3E%3Cimg&status=resolved`（恶意搜索词） | 200，items:0 空集，无异常 ✓ |
| `GET /api/system`、`/api/scoreboard`、`/api/public/summary`、`/api/public/resolved` | 全部 200 ✓ |

本次抽查未撞 DuckDB 写窗口锁，故未触发 60s 重试路径（该路径为 web_server 既有行为，与本改动无关）。

---

## 3. 本轮范围外观察（记录备查，未改）

- app.js 内 `h.status`（:171 health-banner class）、`c.status`（:181 check-row class）、`ev.event_type`（:236 时间线 class）仍以未转义形式进 class 属性。数据源为内部引擎/系统自身写入（非用户/LLM 直供），且枚举受控，风险低；若未来数据源开放，建议按同样方式收敛（P2）。
- 服务端响应头 CSP（frame-ancestors 等 meta 无法表达的指令）需在 web_server.py 加中间件，属后端改动，另行排期。
- 评审 §5.2 的「vitest 骨架」部分（前端测试基建）不在本次 2h 核心范围；本次以轻量纯 JS 断言脚本先行兜住 5 处修复的防回退，vitest 骨架可作为后续升级载体。

## 4. 文件清单

| 文件 | 状态 |
|---|---|
| `src/predictor/web/static/app.js` | 修改（+14/-5）：新增 safeHref、5 处插值转义 |
| `src/predictor/web/static/index.html` | 修改（+1）：CSP meta |
| `scripts/test_xss_helpers.js` | 新增：38 项断言脚本（node 直跑） |
| `docs/hermes-fix-report-xss-2026-08-27.md` | 本报告 |

未 commit、未改 style.css、未碰 .env/shell/pi/Python 代码。
