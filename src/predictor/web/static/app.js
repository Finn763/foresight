"use strict";
// Foresight 展示页前端：内部视图（看板/详情/系统）+ 写窗口 503 自动重试。
const $ = (sel) => document.querySelector(sel);
const mode = new URLSearchParams(location.search).get("mode");

const state = { tab: "board", filters: { class: "", status: "", arm: "", q: "" }, retries: 0 };
state.opsFilter = "";
let opsTimer = null;
let probeTimer = null;
function stopOpsPolling() { if (opsTimer) { clearInterval(opsTimer); opsTimer = null; } }

// ---- fetch 封装：503/网络错误 → 横幅 + 3 秒重试 ----
async function fetchJSON(path, retry = true) {
  try {
    const r = await fetch(path);
    if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
    state.retries = 0;
    hideBanner();
    return await r.json();
  } catch (e) {
    if (e.status === 404) throw e; // 不重试（public 模式探测用）
    showBanner("数据库写窗口进行中或服务不可用，自动重试…（每日 09:05/16:30 前后为写窗口）");
    if (retry) {
      await new Promise((res) => setTimeout(res, 3000));
      return fetchJSON(path);
    }
    throw e;
  }
}
function showBanner(msg) { const b = $("#error-banner"); b.textContent = msg; b.hidden = false; }
function hideBanner() { $("#error-banner").hidden = true; }

// ---- 渲染工具 ----
function fmt(dt) { return dt ? String(dt).replace("T", " ").slice(0, 16) : "—"; }
function pct(x) { return x == null ? "—" : (x * 100).toFixed(0) + "%"; }
function badgeStatus(s) { return `<span class="badge ${s}">${{open:"未揭晓",pending:"待揭晓",resolved:"已揭晓"}[s]}</span>`; }

function probCell(p) {
  if (p == null) return "—";
  return `<span class="prob-bar"><span style="width:${Math.round(p * 100)}%"></span></span><span class="prob">${pct(p)}</span>`;
}

// ---- 看板 ----
async function renderBoard() {
  const f = state.filters;
  const qs = new URLSearchParams();
  if (f.class) qs.set("class", f.class);
  if (f.status) qs.set("status", f.status);
  if (f.arm) qs.set("arm", f.arm);
  if (f.q) qs.set("q", f.q);
  const { items } = await fetchJSON("/api/questions?" + qs.toString());
  $("#app").innerHTML = `
    <div class="card filters">
      <select id="f-class">
        <option value="">全部分类</option><option>${["A", "B", "C"].join("</option><option>")}</option></select>
      <select id="f-status">
        <option value="">全部状态</option>
        <option value="open">未揭晓</option><option value="pending">待揭晓</option>
        <option value="resolved">已揭晓</option></select>
      <select id="f-arm">
        <option value="">全部臂</option>
        <option value="baseline">臂 A</option><option value="experiment">臂 B</option></select>
      <input id="f-q" placeholder="关键词搜索…" value="${f.q}">
      <span style="color:#6b7280;font-size:13px;align-self:center">${items.length} 题</span>
    </div>
    <div class="card"><table>
      <thead><tr><th>ID</th><th>标题</th><th>概率</th><th>揭晓时间</th><th>状态</th><th>Brier</th><th>分类</th></tr></thead>
      <tbody id="q-body">${items.map((q) => `
        <tr class="clickable" data-id="${q.id}">
          <td>#${q.id}</td><td>${esc(q.title)}</td>
          <td>${probCell(q.probability)}</td>
          <td>${fmt(q.closes_at)}</td>
          <td>${badgeStatus(q.status)}</td>
          <td>${q.brier_score == null ? "—" : q.brier_score.toFixed(4)}</td>
          <td><span class="badge cls">${q.resolution_class ?? "—"}</span></td>
        </tr>`).join("") || `<tr><td colspan="7" class="empty">没有匹配的题目</td></tr>`}
      </tbody></table></div>`;
  $("#q-body").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (tr) openDetail(Number(tr.dataset.id));
  });
  $("#f-class").value = f.class; $("#f-status").value = f.status; $("#f-arm").value = f.arm;
  $("#f-class").onchange = (e) => { state.filters.class = e.target.value; renderBoard(); };
  $("#f-status").onchange = (e) => { state.filters.status = e.target.value; renderBoard(); };
  $("#f-arm").onchange = (e) => { state.filters.arm = e.target.value; renderBoard(); };
  $("#f-q").onchange = (e) => { state.filters.q = e.target.value; renderBoard(); };
}

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

// ---- 详情弹层 ----
async function openDetail(id) {
  const d = await fetchJSON(`/api/questions/${id}`);
  $("#detail-body").innerHTML = `
    <h2>#${d.id} ${esc(d.title)}</h2>
    <dl class="kv">
      <dt>预测概率</dt><dd style="font-size:22px;font-weight:600">${pct(d.probability)}</dd>
      <dt>揭晓窗口</dt><dd>${fmt(d.opens_at)} → ${fmt(d.closes_at)}</dd>
      <dt>状态</dt><dd>${badgeStatus(d.status)} ${d.resolution_class ? `<span class="badge cls">${d.resolution_class}</span>` : ""}</dd>
      <dt>结果</dt><dd>${d.outcome == null ? "待揭晓" : (d.outcome ? "是 ✓" : "否 ✗")}</dd>
      <dt>Brier</dt><dd>${d.brier_score == null ? "—" : d.brier_score.toFixed(4)}</dd>
      <dt>臂</dt><dd>${d.arm ?? "—"}</dd>
      <dt>判定规格</dt><dd>${d.resolution_spec ? JSON.stringify(d.resolution_spec) : "—"}</dd>
    </dl>
    ${modelDivergence(d)}
    <div class="evidence"><h3>证据（${(d.documents || []).length}）</h3>
      <ul>${(d.documents || []).map((doc) => `
        <li><a href="${esc(doc.url || "#")}" target="_blank" rel="noopener">${esc(doc.title || doc.url || "(无标题)")}</a>
            <span class="src">${esc(doc.source)} · ${fmt(doc.published_at)}</span></li>`).join("") ||
        `<li style="color:#6b7280">无证据文档</li>`}
      </ul></div>`;
  $("#detail-overlay").hidden = false;
}
function modelDivergence(d) {
  const runs = d.model_runs;
  if (!runs || !Object.keys(runs).length) return "";
  const all = Object.values(runs).flat().filter((x) => typeof x === "number");
  if (!all.length) return "";
  const mn = Math.min(...all), mx = Math.max(...all);
  const mid = [...all].sort((a, b) => a - b)[Math.floor(all.length / 2)];
  return `<dl class="kv"><dt>模型分歧</dt>
    <dd>${pct(mn)} ~ ${pct(mx)}（中位 ${pct(mid)}，${all.length} 次采样）</dd></dl>`;
}
$("#detail-close").addEventListener("click", () => { $("#detail-overlay").hidden = true; });
$("#detail-overlay").addEventListener("click", (e) => { if (e.target.id === "detail-overlay") $("#detail-overlay").hidden = true; });

// ---- 系统面板 ----
async function renderSystem() {
  const s = await fetchJSON("/api/system");
  const sb = await fetchJSON("/api/scoreboard");
  $("#app").innerHTML = `
    <div class="card"><h3>战绩分桶</h3>${bucketsHTML(sb.buckets)}</div>
    <div class="card"><h3>双臂统计</h3>
      <table><thead><tr><th>臂</th><th>预测数</th><th>已揭晓</th><th>Brier 均值</th></tr></thead>
      <tbody>${s.arm_stats.map((a) => `<tr><td>${a.arm}</td><td>${a.n}</td>
        <td>${a.resolved}</td><td>${a.brier_mean == null ? "—" : a.brier_mean.toFixed(4)}</td></tr>`).join("") ||
        `<tr><td colspan="4" class="empty">无预测数据</td></tr>`}</tbody></table></div>
    <div class="card"><h3>杠杆（levers）</h3>${leversHTML(s.levers)}</div>
    <div class="card"><h3>经验库（lessons）</h3>${lessonsHTML(s.lessons)}</div>
    <div class="card"><h3>进化日志</h3>${logHTML(s.evolution_log)}</div>
    <div class="card"><h3>模型统计</h3>${modelStatsHTML(s.model_stats)}</div>`;
}

// ---- 系统日志（ops）----
const CHECK_LABEL = { ok: "正常", warn: "警告", error: "异常", info: "提示", pending: "待运行" };
const CHIP_TYPES = {
  round: "round_started,round_completed",
  question_added: "question_added",
  prediction_added: "prediction_added",
  resolution: "resolution_ok,resolution_failed,resolution_timeout,resolution_archived,resolution_brier_failed",
  prediction_skipped: "prediction_skipped",
};
async function renderOps() {
  stopOpsPolling();
  const typeParam = state.opsFilter ? "&types=" + encodeURIComponent(CHIP_TYPES[state.opsFilter]) : "";
  const h = await fetchJSON("/api/ops/health");
  const { items } = await fetchJSON("/api/ops/log?limit=200" + typeParam);
  $("#app").innerHTML = `
    <div class="card health-banner ${h.status}">
      <span class="health-dot"></span>
      <strong>系统状态：${CHECK_LABEL[h.status] ?? h.status}</strong>
      <span style="color:#6b7280;font-size:12px">检测于 ${fmt(h.checked_at)}</span>
      <button id="refresh-probes">立即检测</button>
    </div>
    <div class="card"><h3>健康检查</h3>
      ${h.checks.map((c) => `
        <div class="check-row ${c.status}">
          <span class="check-dot"></span>
          <span class="check-summary">${esc(c.summary)}</span>
          ${c.detail && Object.keys(c.detail).length ?
            `<details><summary>明细</summary><pre>${esc(JSON.stringify(c.detail, null, 2))}</pre></details>` : ""}
        </div>`).join("")}
    </div>
    <div class="card"><h3>事件时间线（最近 200 条）</h3>
      <div class="chips">${chipsHTML()}</div>
      <ul class="timeline">${items.map(evHTML).join("") || "<li class='empty'>暂无事件</li>"}</ul>
    </div>
    <div class="card"><h3>原始日志文件（尾部 100 行）</h3>
      ${["daily", "evolve"].map((n) => `<details><summary>data/${n}.log</summary><pre class="logfile" data-file="${n}">加载中…</pre></details>`).join("")}
    </div>`;
  for (const chip of document.querySelectorAll(".chip")) {
    chip.classList.toggle("on", chip.dataset.type === state.opsFilter);
    chip.onclick = () => {
      state.opsFilter = state.opsFilter === chip.dataset.type ? "" : chip.dataset.type;
      renderOps();
    };
  }
  $("#refresh-probes").onclick = async (e) => {
    e.target.textContent = "检测中…";
    await fetch("/api/ops/health/refresh", { method: "POST" }).catch(() => {});
    e.target.textContent = "立即检测";   // 写窗口 503 自愈：POST 失败也复位文案，不卡「检测中…」
    let tries = 0;
    clearInterval(probeTimer);
    probeTimer = setInterval(async () => {
      const now = await fetchJSON("/api/ops/health");
      if (state.tab !== "ops") { clearInterval(probeTimer); return; }   // 切走即弃，不覆盖其他 tab
      if (!now.checks.some((c) => c.key === "probe_refreshing") || ++tries > 10) {
        clearInterval(probeTimer);
        renderOps();
      }
    }, 3000);
  };
  for (const det of document.querySelectorAll("details:has(.logfile)")) {
    det.addEventListener("toggle", () => {
      if (!det.open) return;
      const pre = det.querySelector(".logfile");
      if (pre.textContent === "加载中…") {
        fetchJSON(`/api/ops/log-files?name=${pre.dataset.file}`)
          .then((r) => { pre.textContent = r.lines.join("\n"); })
          .catch(() => { pre.textContent = "（加载失败）"; });
      }
    });
  }
  opsTimer = setInterval(renderOps, 30000);
}
function chipsHTML() {
  return Object.keys(CHIP_TYPES)
    .map((t) => `<button class="chip" data-type="${t}">${t}</button>`).join("");
}
function evHTML(ev) {
  const bad = ["resolution_failed", "prediction_skipped", "resolution_brier_failed"].includes(ev.event_type);
  const cls = bad ? "bad" : ev.event_type.startsWith("round") ? "round" : "";
  return `<li class="ev ${cls}"><span class="ts">${fmt(ev.ts)}</span>
    <span class="type">${esc(ev.event_type)}</span>
    <span class="detail">${esc(ev.detail ?? "")}</span></li>`;
}
function bucketsHTML(buckets) {
  if (!buckets || !buckets.length) return emptyHTML("战绩积累中", "首题自动揭晓 2026-08-14，之后每日 16:30 更新分桶");
  const max = Math.max(...buckets.map((b) => b.n));
  return `<div class="axis-grid">${buckets.map((b) => `
    <div class="bucket"><div class="bar" style="height:${Math.max(6, Math.round(120 * b.n / max))}px"></div>
      <div class="lbl">${b.bucket}<br>n=${b.n}<br>${b.brier_mean.toFixed(3)}${b.unreliable ? "*" : ""}</div></div>`).join("")}</div>
    <p style="font-size:12px;color:#6b7280">桶内 Brier 均值（* 样本不足）</p>`;
}
function leversHTML(levers) {
  if (!levers.length) return emptyHTML("闭环运行中", "levers 将随每日 predict/resolve 与周报归因积累");
  return `<table><thead><tr><th>杠杆</th><th>类型</th><th>状态</th><th>效应量</th><th>验证样本</th></tr></thead>
    <tbody>${levers.map((l) => `<tr><td>${esc(l.lever_key)}</td><td>${esc(l.lever_type)}</td>
      <td>${esc(l.status)}</td><td>${l.effect_size ?? "—"}</td><td>${l.n_validated}</td></tr>`).join("")}</tbody></table>`;
}
function lessonsHTML(lessons) {
  if (!lessons.length) return emptyHTML("闭环运行中", "lessons 将随揭晓归因积累（首个周报 2026-08-17）");
  return `<table><thead><tr><th>类型</th><th>假设</th><th>结果</th><th>归因</th><th>置信</th><th>状态</th></tr></thead>
    <tbody>${lessons.map((l) => `<tr><td>${esc(l.question_type)}</td><td>${esc(l.testable_criteria)}</td>
      <td>${l.outcome ? "✓" : "✗"}</td><td>${esc(l.attribution ?? "—")}</td>
      <td>${l.confidence ?? "—"}</td><td>${esc(l.status)}</td></tr>`).join("")}</tbody></table>`;
}
function logHTML(logs) {
  if (!logs.length) return emptyHTML("闭环运行中", "evolution_log 将随每日轮次写入");
  return `<table><thead><tr><th>时间</th><th>事件</th><th>详情</th></tr></thead>
    <tbody>${logs.map((l) => `<tr><td>${fmt(l.ts)}</td><td>${esc(l.event_type)}</td>
      <td style="font-size:12px">${esc(l.detail)}</td></tr>`).join("")}</tbody></table>`;
}
function modelStatsHTML(stats) {
  if (!stats.length) return emptyHTML("闭环运行中", "模型统计将随预测轮积累");
  return `<table><thead><tr><th>模型</th><th>预测数</th><th>Brier EMA</th><th>更新时间</th></tr></thead>
    <tbody>${stats.map((m) => `<tr><td>${esc(m.model_name)}</td><td>${m.predictions}</td>
      <td>${m.brier_ema == null ? "—" : m.brier_ema.toFixed(4)}</td><td>${fmt(m.last_updated)}</td></tr>`).join("")}</tbody></table>`;
}
function emptyHTML(title, sub) { return `<div class="empty"><strong>${title}</strong>${sub}</div>`; }

// ---- 对外视图（战绩榜）：仅从 /api/public/* 取数 ----
async function renderPublic() {
  const s = await fetchJSON("/api/public/summary");
  const { items } = await fetchJSON("/api/public/resolved");
  document.querySelectorAll(".tab").forEach((t) => (t.style.display = "none"));
  $("#mode-switch").innerHTML = `<a href="?"><button>← 内部视图</button></a>`;
  $("#app").innerHTML = `
    <div class="card" style="display:flex;gap:32px;flex-wrap:wrap">
      <div><div style="font-size:12px;color:#6b7280">已揭晓</div><div style="font-size:28px;font-weight:700">${s.resolved}</div></div>
      <div><div style="font-size:12px;color:#6b7280">整体 Brier</div><div style="font-size:28px;font-weight:700">${s.brier_mean == null ? "—" : s.brier_mean.toFixed(4)}</div></div>
      <div><div style="font-size:12px;color:#6b7280">战绩窗口</div><div style="font-size:14px;margin-top:6px">${s.first_resolved_at ? fmt(s.first_resolved_at) + " → " + fmt(s.last_resolved_at) : "—"}</div></div>
    </div>
    <div class="card"><h3>分桶 Brier</h3>${bucketsHTML(s.buckets)}</div>
    <div class="card"><h3>已揭晓预测</h3>
      <table><thead><tr><th>标题</th><th>揭晓时间</th><th>预测概率</th><th>结果</th><th>Brier</th></tr></thead>
      <tbody>${items.map((q) => `
        <tr><td>${esc(q.title)}</td><td>${fmt(q.closes_at)}</td>
          <td>${probCell(q.probability)}</td>
          <td>${q.outcome ? "是 ✓" : "否 ✗"}</td>
          <td>${q.brier_score == null ? "—" : q.brier_score.toFixed(4)}</td></tr>`).join("") ||
        `<tr><td colspan="5" class="empty"><strong>战绩积累中</strong>首题自动揭晓 2026-08-14，之后每日 16:30 更新</td></tr>`}
      </tbody></table></div>`;
}

// ---- 顶栏 & 启动 ----
async function init() {
  // public 模式探测：内部端点 404（路由未注册）→ 锁定对外视图（仅 --mode public 时发生）。
  // 注意判断必须用 status === 404 而非 !ok——503（DB 写窗口/不可用）不算 public 信号，
  // 保持内部视图并走既有 fetchJSON 横幅重试路径，避免误锁对外视图（final review I-4）。
  let view = "internal";
  if (mode === "public") { view = "public"; }
  else {
    try {
      const probe = await fetch("/api/questions", { method: "HEAD" });
      if (probe.status === 404) view = "public";
    } catch { /* 网络错误由重试横幅兜底 */ }
  }
  if (view === "public") renderPublic();
  else {
    $("#mode-switch").innerHTML = `<a href="?mode=public"><button>对外视图 →</button></a>`;
    document.querySelectorAll(".tab").forEach((t) =>
      t.addEventListener("click", async () => {
        document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
        t.classList.add("active");
        state.tab = t.dataset.tab;
        stopOpsPolling();   // 切走必须停轮询：否则 30s 后 renderOps 会覆盖其他 tab 的 DOM
        if (state.tab === "board") await renderBoard();
        else if (state.tab === "system") await renderSystem();
        else await renderOps();
      }));
    await renderBoard();
  }
}
init();
