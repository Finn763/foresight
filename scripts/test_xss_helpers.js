"use strict";
// Foresight XSS 防线断言（纯 Node，零依赖；无需 pytest / jsdom）。
// 运行：node scripts/test_xss_helpers.js
// 覆盖三层：
//   1) 行为层 —— 从 app.js 提取 esc/escAttr/safeHref 的真实函数源码，
//      注入最小 DOM/location stub（模拟浏览器 textContent→innerHTML 序列化的 XSS 语义）后断言；
//   2) 调用点层 —— 断言 5 处修复后的模板写法仍在 app.js（防止回退为未转义写法）；
//   3) CSP 层 —— 断言 index.html 含 CSP meta，script-src 严格无 'unsafe-inline'，object-src 'none'。
const fs = require("fs");
const path = require("path");

const APP = path.join(__dirname, "..", "src", "predictor", "web", "static", "app.js");
const HTML = path.join(__dirname, "..", "src", "predictor", "web", "static", "index.html");
const src = fs.readFileSync(APP, "utf8");
const html = fs.readFileSync(HTML, "utf8");

let passed = 0;
function ok(cond, msg) {
  if (!cond) { console.error("FAIL: " + msg); process.exit(1); }
  passed++;
  console.log("ok " + passed + " - " + msg);
}

// ---- 从 app.js 按名提取函数源码（花括号计数；跳过字符串/注释/正则字面量） ----
function skipStr(s, i, quote) {
  i++;
  while (i < s.length) {
    if (s[i] === "\\") { i += 2; continue; }
    if (s[i] === quote) return i + 1;
    i++;
  }
  return i;
}
function skipRegex(s, i) {
  i++;
  while (i < s.length) {
    if (s[i] === "\\") { i += 2; continue; }
    if (s[i] === "/") return i + 1;
    i++;
  }
  return i;
}
function regexStart(s, i) {
  // 上一个非空白字符是操作符/括号等 → 该 / 是正则字面量而非除法
  let j = i - 1;
  while (j >= 0 && /\s/.test(s[j])) j--;
  if (j < 0) return true;
  return "([=,:;{}!&|?+-*%<>^~".includes(s[j]);
}
function extractFn(name) {
  const m = new RegExp("function " + name + "\\(").exec(src);
  if (!m) throw new Error("app.js 中未找到函数 " + name);
  let i = src.indexOf("{", m.index);
  let depth = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === '"' || c === "'" || c === "`") { i = skipStr(src, i, c); continue; }
    if (c === "/" && src[i + 1] === "/") { i = src.indexOf("\n", i); if (i < 0) i = src.length; continue; }
    if (c === "/" && src[i + 1] === "*") { i = src.indexOf("*/", i + 2); i = i < 0 ? src.length : i + 2; continue; }
    if (c === "/" && regexStart(src, i)) { i = skipRegex(src, i); continue; }
    if (c === "{") depth++;
    else if (c === "}") { depth--; if (depth === 0) return src.slice(m.index, i + 1); }
    i++;
  }
  throw new Error(name + " 函数体未闭合，无法提取");
}

// ---- 最小 DOM/location stub ----
// 语义对齐浏览器：textContent 写入后读 innerHTML，文本节点序列化转义 & < >；
// " 在文本节点上下文不转义（浏览器真实行为），属性上下文由 escAttr 补转义。
const document = {
  createElement() {
    let text = "";
    return {
      set textContent(v) { text = String(v); },
      get innerHTML() {
        return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      },
    };
  },
};
const location = { origin: "http://127.0.0.1:8765" };
function loadHelpers() {
  const body = `"use strict";\n${extractFn("esc")}\n${extractFn("escAttr")}\n${extractFn("safeHref")}\nreturn { esc, escAttr, safeHref };`;
  return new Function("document", "location", body)(document, location);
}

const { esc, escAttr, safeHref } = loadHelpers();

// ---- 行为断言：esc（HTML 文本上下文） ----
ok(esc("<img src=x onerror=alert(1)>") === "&lt;img src=x onerror=alert(1)&gt;", "esc 转义 < > 为实体");
ok(esc("a & b") === "a &amp; b", "esc 转义 & 为实体");
ok(esc("</dd><img onerror=alert(1)>").indexOf("<") === -1, "esc 输出无裸 '<'（</dd> 标签破坏被中和）");
ok(esc(null) === "" && esc(undefined) === "", "esc(null/undefined) 返回空串");
ok(esc(JSON.stringify({ spec: "</dd><img src=x onerror=alert(1)>" })).indexOf("<") === -1,
  "L115 场景：resolution_spec JSON 化后经 esc 无标签注入");

// ---- 行为断言：escAttr（HTML 属性上下文） ----
ok(escAttr('a"b') === "a&quot;b", "escAttr 双引号转 &quot;");
ok(escAttr('" onfocus="alert(1)').indexOf('"') === -1, "escAttr 中和属性引号破坏");
ok(escAttr('"><img src=x onerror=alert(1)>').indexOf('"') === -1 &&
   escAttr('"><img src=x onerror=alert(1)>').indexOf("<") === -1,
  "L74 场景：搜索词回填 payload 经 escAttr 无属性/标签注入");

// ---- 行为断言：safeHref（href 协议白名单 + 属性转义） ----
const BLOCKED = ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>alert(1)</script>",
  "vbscript:msgbox(1)", "file:///etc/passwd", "  javascript:alert(1)", "\u0000javascript:alert(1)"];
for (const u of BLOCKED) ok(safeHref(u) === "#", "safeHref 拦截协议 " + JSON.stringify(u));
const ALLOWED = ["https://example.com/a?b=1", "http://example.com/x", "/evidence/1",
  "docs/x.html", "./x", "../x", "//example.com/path"];
for (const u of ALLOWED) ok(safeHref(u) === u, "safeHref 放行 " + JSON.stringify(u));
ok(safeHref("") === "#" && safeHref(null) === "#" && safeHref(undefined) === "#", "safeHref 空值回落 #");
ok(safeHref('https://x.com/a"onclick="alert(1)').indexOf('"') === -1,
  "safeHref 对 http(s) URL 内嵌引号仍做属性转义");

// ---- 调用点断言（防回退）：5 处修复写法仍在 ----
ok(src.includes('esc(JSON.stringify(d.resolution_spec))'), "调用点1 L115 resolution_spec 走 esc()");
ok(src.includes('href="${safeHref(doc.url)}"'), "调用点2 L120 href 走 safeHref()");
ok(src.includes('value="${escAttr(f.q)}"'), "调用点3 L74 搜索词回填走 escAttr()");
ok(src.includes('${q.resolution_class ? esc(q.resolution_class) : "—"}'), "调用点4 L86 resolution_class 走 esc()");
ok(src.includes('${esc(d.resolution_class)}</span>` : ""}'), "调用点5 L111 resolution_class 走 esc()");
ok(!src.includes('value="${f.q}"'), "负向：旧写法 value=${f.q} 已清除");
ok(!src.includes('href="${esc(doc.url'), "负向：旧写法 href=${esc(doc.url 已清除");
ok(!src.includes('? JSON.stringify(d.resolution_spec)'), "负向：旧写法 JSON.stringify 直插已清除");

// ---- CSP 断言 ----
const csp = /<meta http-equiv="Content-Security-Policy" content="([^"]+)"/.exec(html);
ok(!!csp, "index.html 含 CSP meta");
if (csp) {
  const v = csp[1];
  const scriptSrc = /script-src\s+([^;]+)/.exec(v);
  ok(!!scriptSrc && scriptSrc[1].includes("'self'") && !scriptSrc[1].includes("'unsafe-inline'"),
    "script-src 严格 'self'（无 'unsafe-inline'）");
  ok(v.includes("object-src 'none'"), "object-src 'none' 兜底");
  ok(v.includes("style-src 'self' 'unsafe-inline'"), "style-src 保留 unsafe-inline（内联 style 属性所需）");
  ok(v.includes("img-src 'self' data:"), "img-src 放行 data: favicon");
  ok(v.includes("base-uri 'none'"), "base-uri 'none'（防 <base> 注入）");
}

console.log("\n全部 " + passed + " 项断言通过 ✓");
