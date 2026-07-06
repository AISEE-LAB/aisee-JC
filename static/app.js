/* =========================================================
   中转站倍率监测 - 前端交互（多站点版）
   ========================================================= */

// ---------- 全局状态 ----------
let currentSiteId = "all";   // "all" = 全局视角；具体值 = 单站点视角
let sitesCache = [];         // 缓存站点列表

// ---------- 工具 ----------
function $(sel, root = document) { return root.querySelector(sel); }
function $$(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtRel(ts) {
  if (!ts) return "—";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return `${Math.floor(diff)} 秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}
function fmtVal(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!isNaN(n) && /^-?\d+(\.\d+)?$/.test(String(v))) return String(n);
  return String(v);
}

let toastTimer = null;
function toast(msg, type = "") {
  const el = $("#toast");
  if (!el) return;
  el.textContent = msg;
  el.className = "toast show " + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 3000);
}

async function api(url, opts = {}) {
  const r = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

// ---------- Modal ----------
function openModal(title, htmlBody) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = htmlBody;
  $("#modal").classList.remove("hidden");
}
function closeModal() {
  // 关闭所有 Modal（通用 #modal + 新增站点 #add-site-box + 未来可能的其它）
  $$(".modal").forEach(el => el.classList.add("hidden"));
}
// 用 click 冒泡：处理所有 data-close 元素（取消按钮、×、背景遮罩）
document.addEventListener("click", e => {
  if (e.target.closest("[data-close]")) closeModal();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

// ---------- 站点切换栏 ----------
function renderSiteTabs() {
  const box = $("#site-tabs");
  if (!box) return;
  let html = `<button class="site-tab ${currentSiteId === "all" ? "active" : ""}" data-site-id="all">🌐 全部站点</button>`;
  for (const s of sitesCache) {
    const active = currentSiteId === s.site_id ? "active" : "";
    const badge = s.unread ? `<span class="site-tab-badge">${s.unread}</span>` : "";
    html += `<button class="site-tab ${active}" data-site-id="${escapeHtml(s.site_id)}">${escapeHtml(s.name)}${badge}</button>`;
  }
  box.innerHTML = html;
  $$(".site-tab").forEach(btn => {
    btn.addEventListener("click", () => switchSite(btn.dataset.siteId));
  });
}

function switchSite(siteId) {
  currentSiteId = siteId;
  // 同步到 URL（不刷新页面）
  const u = new URL(location.href);
  if (siteId === "all") u.searchParams.delete("site_id");
  else u.searchParams.set("site_id", siteId);
  history.replaceState(null, "", u);
  renderSiteTabs();
  // 重新加载当前页
  loadCurrentPage();
}

async function refreshSitesList() {
  try {
    const d = await api("/api/sites");
    sitesCache = (d.sites || []).map(s => ({ site_id: s.id, name: s.name }));
    renderSiteTabs();
  } catch (e) { /* 静默 */ }
}

function currentSiteParam() {
  return currentSiteId === "all" ? "" : `&site_id=${encodeURIComponent(currentSiteId)}`;
}

// ---------- 全局状态指示 ----------
async function refreshGlobalStatus() {
  try {
    const data = await api("/api/dashboard");
    const pill = $("#global-status");
    if (!pill) return;
    const dot = pill.querySelector(".dot");
    const txt = pill.querySelector(".status-text");
    // 取所有站点的综合健康度
    const sites = data.sites || [];
    const hasFail = sites.some(s => s.latest_fetch && s.latest_fetch.success === false);
    const noData = sites.length === 0 || sites.every(s => !s.latest_fetch || s.latest_fetch.success === null);
    if (noData) { dot.className = "dot dot-gray"; txt.textContent = "未抓取"; }
    else if (hasFail) { dot.className = "dot dot-red"; txt.textContent = "有站点异常"; }
    else { dot.className = "dot dot-green"; txt.textContent = `${sites.length} 站运行中`; }
  } catch (e) { /* 静默 */ }
}

// =========================================================
// 页面调度
// =========================================================
function loadCurrentPage() {
  const page = window.PAGE;
  if (page === "dashboard") loadDashboard();
  else if (page === "models") loadModels();
  else if (page === "changes") loadChanges();
  else if (page === "config") loadConfig();
  else if (page === "logs") loadLogs();
}

// =========================================================
// 仪表盘（双视角）
// =========================================================
let countdownTimer = null, nextRunTs = null;

async function loadDashboard() {
  try {
    const url = currentSiteId === "all"
      ? "/api/dashboard"
      : `/api/dashboard?site_id=${encodeURIComponent(currentSiteId)}`;
    const data = await api(url);

    if (data.mode === "overview") {
      $("#overview-mode").classList.remove("hidden");
      $("#single-mode").classList.add("hidden");
      $("#page-title").textContent = "仪表盘 · 全部站点";
      renderOverview(data);
    } else {
      $("#overview-mode").classList.add("hidden");
      $("#single-mode").classList.remove("hidden");
      $("#page-title").textContent = `仪表盘 · ${data.site?.replace(/^https?:\/\//, "") || currentSiteId}`;
      renderSingle(data);
    }
  } catch (e) {
    toast("加载仪表盘失败：" + e.message, "error");
  }
}

function renderOverview(data) {
  // 全局统计
  const ov = data.overall;
  $("#overview-stats").innerHTML = `
    <div class="card"><div class="card-label">站点数</div><div class="card-value">${data.sites.length}</div></div>
    <div class="card"><div class="card-label">总变化数</div><div class="card-value">${ov.total_changes}</div><div class="card-sub">24h：${ov.changes_last_24h}</div></div>
    <div class="card"><div class="card-label">总抓取次数</div><div class="card-value">${ov.total_snapshots}</div><div class="card-sub">成功 ${ov.success_snapshots} / 失败 ${ov.failed_snapshots}</div></div>
    <div class="card"><div class="card-label">通知发送</div><div class="card-value">${ov.total_notifications}</div></div>
  `;

  // 下次抓取
  nextRunTs = data.next_run_ts;
  startCountdown();
  $("#overview-next-run").textContent = nextRunTs ? `下次抓取倒计时见下` : "";

  // 站点卡片
  const sitesBox = $("#sites-cards");
  if (!data.sites.length) {
    sitesBox.innerHTML = '<div class="empty" style="padding:32px;text-align:center;color:var(--text-light)">还没有站点，去<a href="/config" class="link">配置页</a>添加一个吧</div>';
  } else {
    sitesBox.innerHTML = data.sites.map(s => {
      const snap = s.latest_snapshot;
      const fi = s.latest_fetch;
      let healthHtml;
      if (!fi || fi.success === null) {
        healthHtml = '<span class="health-none">未抓取</span>';
      } else if (fi.success) {
        healthHtml = `<span class="health-ok">● 正常</span> · ${fmtRel(fi.fetched_at)}`;
      } else {
        healthHtml = `<span class="health-fail">● 失败</span> · ${escapeHtml((fi.error_msg || "").slice(0, 50))}`;
      }
      return `
        <div class="site-card" data-site-id="${escapeHtml(s.site_id)}" style="padding:16px 18px;border-bottom:1px solid var(--border);cursor:pointer;display:flex;gap:16px;align-items:center;">
          <div style="flex:1;min-width:0;">
            <div style="font-weight:600;font-size:15px;margin-bottom:4px;">
              ${escapeHtml(s.name)} <span class="badge badge-set" style="font-size:10px">${escapeHtml(s.system || "?")}</span>
            </div>
            <div style="color:var(--text-muted);font-size:12px;">
              分组 ${snap?.group_count ?? 0} · 模型 ${snap?.model_count ?? 0} · 变化 ${s.stats.total_changes}
            </div>
          </div>
          <div style="text-align:right;font-size:12px;color:var(--text-muted);min-width:160px;">
            ${healthHtml}<br>
            <span class="muted">每 ${s.interval_minutes} 分钟</span>
          </div>
        </div>
      `;
    }).join("");
    $$(".site-card").forEach(el => {
      el.addEventListener("click", () => switchSite(el.dataset.siteId));
    });
  }

  // 全局最近变更
  renderRecentChanges(data.recent_changes_global || [], "#overview-recent-changes");
}

function renderSingle(data) {
  const snap = data.latest_snapshot;
  const site = data.site || "—";
  $("#card-site").textContent = site.replace(/^https?:\/\//, "");
  $("#card-group").textContent = "分组：" + (snap?.group_count ?? "—") + " 个 · 当前 " + (snap?.user_group || "—");
  $("#card-models").textContent = snap?.model_count ?? "—";
  $("#card-fetched").textContent = snap ? "抓取于 " + fmtRel(snap.fetched_at) : "—";
  $("#card-changes").textContent = data.stats.total_changes;
  $("#card-changes-24h").textContent = data.stats.changes_last_24h;
  $("#card-channels").textContent = data.enabled_channels.length;
  $("#card-channels-list").textContent = data.enabled_channels.length
    ? data.enabled_channels.join(" / ") : "未启用任何渠道";

  const fi = data.latest_fetch;
  const healthEl = $("#fetch-health");
  if (!fi || fi.success === null) {
    healthEl.innerHTML = '<span class="health-none">尚无抓取记录</span>';
  } else if (fi.success) {
    healthEl.innerHTML =
      `<span class="health-ok">● 正常</span> · 最近抓取 ${fmtTime(fi.fetched_at)} · ` +
      `共 ${data.stats.total_snapshots} 次（成功 ${data.stats.success_snapshots} / 失败 ${data.stats.failed_snapshots}）`;
  } else {
    healthEl.innerHTML =
      `<span class="health-fail">● 失败</span> · ${fmtTime(fi.fetched_at)} · <span class="health-fail">${escapeHtml(fi.error_msg || "未知错误")}</span>`;
  }

  renderRecentChanges(data.changes_recent || [], "#recent-changes");
  renderRecentNotifications(data.notifications_recent || []);
  nextRunTs = data.next_run_ts;
  startCountdown(data.interval_minutes);
}

function renderRecentChanges(changes, selector) {
  const el = $(selector);
  if (!el) return;
  if (!changes.length) {
    el.innerHTML = '<div class="empty" style="padding:24px;text-align:center;color:var(--text-light)">暂无变更 🎉</div>';
    return;
  }
  el.innerHTML = changes.map(c => `
    <div class="timeline-item">
      <div class="timeline-icon ${iconClass(c.kind)}">${iconText(c.kind)}</div>
      <div class="timeline-body">
        <div class="timeline-title">${escapeHtml(c.key_name)} ${c.site_id ? `<span class="muted">· ${escapeHtml(c.site_id)}</span>` : ""}</div>
        <div class="timeline-meta">
          ${fmtTime(c.detected_at)}
          ${(c.before_val !== "" || c.after_val !== "")
      ? ` · <span class="timeline-val">${escapeHtml(c.before_val)}</span><span class="timeline-arrow">→</span><span class="timeline-val">${escapeHtml(c.after_val)}</span>`
      : ""}
        </div>
      </div>
    </div>
  `).join("");
}

function renderRecentNotifications(notifs) {
  const el = $("#recent-notifications");
  if (!el) return;
  if (!notifs.length) {
    el.innerHTML = '<div class="empty" style="padding:24px;text-align:center;color:var(--text-light)">尚未发送通知</div>';
    return;
  }
  el.innerHTML = notifs.map(n => {
    const results = (n.results || []).map(r =>
      `<span class="${r.ok ? "badge badge-set" : "badge badge-changed"}">${r.channel} ${r.ok ? "✓" : "✗"}</span>`
    ).join(" ");
    return `
      <div class="timeline-item">
        <div class="timeline-body">
          <div class="timeline-title">${escapeHtml(n.title || "(无标题)")} · ${n.changes_count} 处变更</div>
          <div class="timeline-meta">${fmtTime(n.sent_at)} · ${results}</div>
        </div>
      </div>`;
  }).join("");
}

function iconClass(kind) {
  return ({ group_ratio: "group", model_ratio: "model", model_added: "added", model_removed: "removed" })[kind] || "group";
}
function iconText(kind) {
  return ({ group_ratio: "G", model_ratio: "M", model_added: "+", model_removed: "−" })[kind] || "?";
}

function startCountdown(intervalMin) {
  clearInterval(countdownTimer);
  const update = () => {
    if (!nextRunTs) { $("#countdown") && ($("#countdown").textContent = "—"); return; }
    const remain = nextRunTs - Date.now() / 1000;
    if (remain <= 0) { $("#countdown") && ($("#countdown").textContent = "正在抓取…"); return; }
    const m = Math.floor(remain / 60), s = Math.floor(remain % 60);
    $("#countdown") && ($("#countdown").textContent = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`);
  };
  update();
  countdownTimer = setInterval(update, 1000);
}

// =========================================================
// 模型表格
// =========================================================
async function loadModels() {
  const q = $("#search-input")?.value || "";
  const sort = $("#sort-select")?.value || "name";
  const url = `/api/models?q=${encodeURIComponent(q)}&sort=${sort}${currentSiteParam()}`;
  try {
    const data = await api(url);
    const tbody = $("#models-tbody");
    $("#model-count").textContent = `共 ${data.count} 个模型${currentSiteId !== "all" ? "" : "（所有站点）"}`;
    if (!data.models.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">无数据</td></tr>';
      return;
    }
    const now = Date.now() / 1000;
    tbody.innerHTML = data.models.map(m => {
      const badge = m.last_changed_at
        ? `<span class="badge ${(now - m.last_changed_at < 86400) ? "badge-changed" : "badge-added"}">${fmtRel(m.last_changed_at)}</span>`
        : '<span class="muted">—</span>';
      return `
        <tr>
          <td>${escapeHtml(m.model_name)}</td>
          <td class="num">${fmtVal(m.ratio)}</td>
          <td class="num">${fmtVal(m.completion_ratio)}</td>
          <td class="num">${fmtVal(m.model_price)}</td>
          <td class="num">${fmtVal(m.group_ratio)}</td>
          <td>${badge}</td>
          <td class="col-action"><button class="btn" style="padding:3px 10px;font-size:12px" data-chart="${encodeURIComponent(m.model_name)}">趋势</button></td>
        </tr>`;
    }).join("");
    $$("button[data-chart]").forEach(b => {
      b.addEventListener("click", () => showModelChart(decodeURIComponent(b.dataset.chart)));
    });
  } catch (e) { toast("加载模型失败：" + e.message, "error"); }
}

async function showModelChart(modelName) {
  openModal(`倍率趋势 · ${modelName}`, `<div class="chart-box"><div class="chart-empty">加载中…</div></div>`);
  try {
    const data = await api(`/api/models/${encodeURIComponent(modelName)}/history?limit=200${currentSiteParam()}`);
    drawChart(modelName, data.history || []);
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="chart-box"><div class="chart-empty">${escapeHtml(e.message)}</div></div>`;
  }
}

function drawChart(modelName, history) {
  const box = $("#modal-body .chart-box");
  if (!history.length) { box.innerHTML = '<div class="chart-empty">暂无历史数据</div>'; return; }
  const pts = history.filter(h => h.ratio !== null && h.ratio !== undefined).map(h => ({ t: h.fetched_at, v: h.ratio }));
  if (pts.length < 1) { box.innerHTML = '<div class="chart-empty">无 ratio 历史数据</div>'; return; }
  const W = 660, H = 260, padL = 50, padR = 16, padT = 16, padB = 36;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const tMin = pts[0].t, tMax = pts[pts.length - 1].t || tMin + 1;
  const vs = pts.map(p => p.v);
  let vMin = Math.min(...vs), vMax = Math.max(...vs);
  if (vMin === vMax) { vMin -= 0.5; vMax += 0.5; }
  const pad = (vMax - vMin) * 0.1; vMin -= pad; vMax += pad;
  const x = t => padL + ((t - tMin) / Math.max(0.001, tMax - tMin)) * innerW;
  const y = v => padT + (1 - (v - vMin) / Math.max(0.0001, vMax - vMin)) * innerH;
  const path = pts.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const areaPath = `${path} L${x(pts[pts.length - 1].t).toFixed(1)},${(padT + innerH).toFixed(1)} L${x(pts[0].t).toFixed(1)},${(padT + innerH).toFixed(1)} Z`;
  const yTicks = [];
  for (let i = 0; i <= 4; i++) {
    const vv = vMin + (vMax - vMin) * i / 4;
    yTicks.push(`<line x1="${padL}" y1="${y(vv).toFixed(1)}" x2="${W - padR}" y2="${y(vv).toFixed(1)}" stroke="#eef2f7" />`);
    yTicks.push(`<text x="${padL - 6}" y="${y(vv).toFixed(1) + 3}" text-anchor="end" font-size="10" fill="#94a3b8">${vv.toFixed(2)}</text>`);
  }
  const xLabels = [pts[0], pts[Math.floor(pts.length / 2)], pts[pts.length - 1]]
    .filter((p, i, arr) => arr.findIndex(q => q.t === p.t) === i)
    .map(p => {
      const d = new Date(p.t * 1000);
      const lbl = `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
      return `<text x="${x(p.t).toFixed(1)}" y="${H - padB + 16}" text-anchor="middle" font-size="10" fill="#94a3b8">${lbl}</text>`;
    }).join("");
  const dots = pts.map(p => {
    const d = new Date(p.t * 1000);
    return `<circle cx="${x(p.t).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="3" fill="#2563eb" stroke="#fff" stroke-width="1"><title>${fmtTime(p.t)} · ${p.v}</title></circle>`;
  }).join("");
  const latest = pts[pts.length - 1].v, first = pts[0].v, delta = latest - first;
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:12px;color:var(--text-muted)">
      <span>共 ${pts.length} 个数据点 · 最新 <b style="color:var(--text)">${latest}</b></span>
      <span class="${delta > 0 ? "log-level-WARNING" : (delta < 0 ? "log-level-INFO" : "")}">${delta > 0 ? "↑" : delta < 0 ? "↓" : "→"} ${Math.abs(delta).toFixed(4)}</span>
    </div>
    <svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      <defs><linearGradient id="grad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#2563eb" stop-opacity="0.25" /><stop offset="100%" stop-color="#2563eb" stop-opacity="0" /></linearGradient></defs>
      ${yTicks.join("")}<path d="${areaPath}" fill="url(#grad)" /><path d="${path}" fill="none" stroke="#2563eb" stroke-width="2" />${dots}${xLabels}
    </svg>`;
}

// =========================================================
// 变更历史
// =========================================================
async function loadChanges() {
  const kind = $("#kind-filter")?.value || "";
  const limit = $("#limit-select")?.value || 100;
  const url = `/api/changes?limit=${limit}&kind=${encodeURIComponent(kind)}${currentSiteParam()}`;
  try {
    const data = await api(url);
    const el = $("#changes-list");
    if (!data.changes.length) {
      el.innerHTML = '<div class="empty" style="padding:32px;text-align:center;color:var(--text-light)">暂无变更记录 🎉</div>';
      return;
    }
    el.innerHTML = data.changes.map(c => `
      <div class="timeline-item">
        <div class="timeline-icon ${iconClass(c.kind)}">${iconText(c.kind)}</div>
        <div class="timeline-body">
          <div class="timeline-title">${escapeHtml(c.key_name)} ${currentSiteId === "all" && c.site_id ? `<span class="muted">· ${escapeHtml(c.site_id)}</span>` : ""}</div>
          <div class="timeline-meta">
            ${fmtTime(c.detected_at)} · ${kindLabel(c.kind)} ${c.site_id && currentSiteId === "all" ? "· " + escapeHtml(c.site_id) : ""}
            ${(c.before_val !== "" || c.after_val !== "") ? ` · <span class="timeline-val">${escapeHtml(c.before_val)}</span><span class="timeline-arrow">→</span><span class="timeline-val">${escapeHtml(c.after_val)}</span>` : ""}
          </div>
        </div>
      </div>`).join("");
  } catch (e) { toast("加载变更失败：" + e.message, "error"); }
}
function kindLabel(kind) {
  return ({ group_ratio: "分组倍率", model_ratio: "模型倍率", model_added: "模型新增", model_removed: "模型下架" })[kind] || kind;
}

// =========================================================
// 日志
// =========================================================
async function loadLogs() {
  const level = $("#log-level-filter")?.value || "";
  const limit = $("#log-limit")?.value || 200;
  try {
    const data = await api(`/api/logs?limit=${limit}&level=${encodeURIComponent(level)}`);
    const tbody = $("#logs-tbody");
    if (!data.logs.length) { tbody.innerHTML = '<tr><td colspan="3" class="empty">无日志</td></tr>'; return; }
    tbody.innerHTML = data.logs.map(l => `
      <tr>
        <td class="col-time">${fmtTime(l.ts)}</td>
        <td class="col-level log-level-${l.level}">${l.level}</td>
        <td class="msg">${escapeHtml(l.message)}</td>
      </tr>`).join("");
  } catch (e) { toast("加载日志失败：" + e.message, "error"); }
}

// =========================================================
// 配置（多站点版）
// =========================================================
let configBackup = null;
let editingSiteIndex = 0;  // 当前编辑的站点下标

async function loadConfig() {
  try {
    const data = await api("/api/config");
    const sites = data.config.sites || [];
    configBackup = JSON.parse(JSON.stringify(sites));
    $("#config-warning").classList.toggle("hidden", data.has_yaml);

    renderConfigSiteList(sites);
    if (sites.length) {
      editingSiteIndex = Math.min(editingSiteIndex, sites.length - 1);
      fillConfigForm(sites[editingSiteIndex]);
    }
  } catch (e) { toast("加载配置失败：" + e.message, "error"); }
}

function renderConfigSiteList(sites) {
  const box = $("#site-list");
  if (!box) return;
  box.innerHTML = sites.map((s, i) => `
    <div class="site-list-item ${i === editingSiteIndex ? "active" : ""}" data-idx="${i}">
      <span class="site-list-name">${escapeHtml(s.name || s.base_url)}</span>
      <span class="badge badge-set" style="font-size:10px">${escapeHtml(s.system || "?")}</span>
      <button class="site-list-del" data-del="${i}" title="删除站点">×</button>
    </div>`).join("");
  $$(".site-list-item").forEach(el => {
    el.addEventListener("click", e => {
      if (e.target.matches("[data-del]")) return;
      editingSiteIndex = parseInt(el.dataset.idx);
      fillConfigForm(configBackup[editingSiteIndex]);
      renderConfigSiteList(configBackup);
    });
  });
  $$("[data-del]").forEach(b => b.addEventListener("click", e => {
    e.stopPropagation();
    const idx = parseInt(b.dataset.del);
    const s = configBackup[idx];
    if (!confirm(`确定删除站点「${s.name || s.base_url}」？该站点的历史数据保留，但不再抓取。`)) return;
    configBackup.splice(idx, 1);
    if (editingSiteIndex >= configBackup.length) editingSiteIndex = Math.max(0, configBackup.length - 1);
    renderConfigSiteList(configBackup);
    if (configBackup[editingSiteIndex]) fillConfigForm(configBackup[editingSiteIndex]);
    else $("#site-form-area").classList.add("hidden");
  }));
}

function fillConfigForm(site) {
  if (!site) return;
  $("#site-form-area").classList.remove("hidden");
  // 顶层字段
  const topFields = ["id", "name", "system", "base_url", "email", "password", "api_key", "access_token", "session_cookie"];
  topFields.forEach(k => {
    const el = $(`[data-field="${k}"]`);
    if (!el) return;
    const v = site[k];
    if (el.tagName === "SELECT") el.value = v || "newapi";
    else if (el.type === "checkbox") el.checked = !!v;
    else el.value = (v === "***" ? "" : (v ?? ""));
  });
  // monitor 字段
  const mon = site.monitor || {};
  ["interval_minutes", "change_threshold_pct", "notify_on_first_run"].forEach(k => {
    const el = $(`[data-field="monitor.${k}"]`);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!mon[k];
    else el.value = mon[k] ?? "";
  });
  // watch_groups / watch_models（列表，逗号分隔）
  ["watch_groups", "watch_models"].forEach(k => {
    const el = $(`[data-field="monitor.${k}"]`);
    if (el) el.value = Array.isArray(mon[k]) ? mon[k].join(",") : (mon[k] || "");
  });
  // notify 字段
  const notify = site.notify || {};
  Object.entries(notify).forEach(([ch, body]) => {
    if (typeof body !== "object") return;
    Object.entries(body).forEach(([k, v]) => {
      const el = $(`[data-field="notify.${ch}.${k}"]`);
      if (!el) return;
      if (el.type === "checkbox") el.checked = !!v;
      else if (el.dataset.bool !== undefined) el.value = v ? "true" : "false";
      else el.value = (v === "***" ? "" : (v ?? ""));
      // 收件人数组
      if (Array.isArray(v) && k === "to_addrs") el.value = v.join(",");
    });
  });
  updateSystemGroups();
}

function collectEditingSite() {
  // 从表单收集当前编辑的站点
  const site = configBackup[editingSiteIndex] || {};
  // 顶层
  ["id", "name", "system", "base_url", "email", "access_token", "session_cookie"].forEach(k => {
    const el = $(`[data-field="${k}"]`);
    if (el) site[k] = el.value.trim();
  });
  // 凭证：留空=不改
  ["password", "api_key"].forEach(k => {
    const el = $(`[data-field="${k}"]`);
    if (el && el.value.trim()) site[k] = el.value.trim();
  });
  // monitor
  const mon = site.monitor = site.monitor || {};
  mon.interval_minutes = parseInt($(`[data-field="monitor.interval_minutes"]`)?.value) || 30;
  mon.change_threshold_pct = parseFloat($(`[data-field="monitor.change_threshold_pct"]`)?.value) || 0;
  mon.notify_on_first_run = $(`[data-field="monitor.notify_on_first_run"]`)?.checked || false;
  mon.watch_groups = ($(`[data-field="monitor.watch_groups"]`)?.value || "").split(",").map(s => s.trim()).filter(Boolean);
  mon.watch_models = ($(`[data-field="monitor.watch_models"]`)?.value || "").split(",").map(s => s.trim()).filter(Boolean);
  // notify
  const notify = site.notify = site.notify || {};
  $$("[data-field]").forEach(el => {
    const path = el.dataset.field;
    if (!path.startsWith("notify.")) return;
    const parts = path.split(".");
    if (parts.length !== 3) return;
    const [, ch, k] = parts;
    if (!notify[ch]) notify[ch] = {};
    if (el.type === "checkbox") notify[ch][k] = el.checked;
    else if (el.dataset.bool !== undefined) notify[ch][k] = el.value === "true";
    else if (k === "to_addrs") notify[ch][k] = el.value.split(",").map(s => s.trim()).filter(Boolean);
    else if (["smtp_port"].includes(k)) notify[ch][k] = parseInt(el.value) || 0;
    else {
      // 凭证：留空=不改（保留 ***）
      const isSecret = ["sendkey", "bot_token", "webhook", "secret", "smtp_pass"].includes(k);
      if (isSecret && !el.value) {
        // 保留旧值（在 configBackup 里是 *** 或实际值，保存时后端会回填）
        notify[ch][k] = site.notify?.[ch]?.[k] || "";
      } else {
        notify[ch][k] = el.value;
      }
    }
  });
  configBackup[editingSiteIndex] = site;
  return site;
}

async function saveConfig() {
  const btn = $("#btn-save-config");
  btn.disabled = true; btn.textContent = "保存中…";
  try {
    collectEditingSite();
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify({ sites: configBackup, log: { level: "INFO", file: "" } }),
    });
    toast("配置已保存（" + configBackup.length + " 个站点）", "success");
    await loadConfig();
    refreshSitesList();
  } catch (e) { toast("保存失败：" + e.message, "error"); }
  finally { btn.disabled = false; btn.textContent = "保存配置"; }
}

function updateSystemGroups() {
  const sel = $('[data-field="system"]');
  if (!sel) return;
  const sys = sel.value;
  $$(".cred-group[data-system]").forEach(el => {
    el.style.display = el.dataset.system === sys ? "" : "none";
  });
}

function addNewSite() {
  // 打开内嵌表单弹窗（不用浏览器 prompt/confirm，更可靠）
  const box = $("#add-site-box");
  if (!box) return;
  $("#new-base-url").value = "";
  $("#new-name").value = "";
  $("#new-system").value = "newapi";
  box.classList.remove("hidden");
  $("#new-base-url").focus();
}

async function confirmAddNewSite() {
  const base_url = ($("#new-base-url").value || "").trim();
  const system = $("#new-system").value;
  const name = ($("#new-name").value || "").trim();
  if (!base_url) { toast("请填写站点地址", "error"); return; }
  if (!/^https?:\/\//.test(base_url)) { toast("地址需以 http:// 或 https:// 开头", "error"); return; }

  const btn = $("#btn-confirm-add-site");
  btn.disabled = true; btn.textContent = "添加中…";
  try {
    const body = { base_url, system };
    if (name) body.name = name;
    const r = await api("/api/sites", {
      method: "POST",
      body: JSON.stringify(body),
    });
    $("#add-site-box").classList.add("hidden");
    toast("已添加站点，请填写凭证后保存", "success");
    await loadConfig();
    // 选中新加的（最后一个）
    editingSiteIndex = configBackup.length - 1;
    if (configBackup[editingSiteIndex]) {
      fillConfigForm(configBackup[editingSiteIndex]);
      renderConfigSiteList(configBackup);
    }
  } catch (e) {
    toast("添加失败：" + e.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "确认添加";
  }
}

// =========================================================
// 初始化
// =========================================================
document.addEventListener("DOMContentLoaded", () => {
  // 从 URL 读 site_id
  const urlSite = new URLSearchParams(location.search).get("site_id");
  if (urlSite) currentSiteId = urlSite;

  const page = window.PAGE;

  // 站点切换栏事件（全局，所有页面都有）
  refreshSitesList();

  // 仪表盘
  if (page === "dashboard") {
    loadDashboard();
    $("#btn-fetch-all")?.addEventListener("click", async e => {
      const btn = e.target; btn.disabled = true; btn.textContent = "抓取中…";
      try {
        const r = await api("/api/fetch_now", { method: "POST", body: JSON.stringify({}) });
        const ok = (r.results || []).filter(x => x.ok).length;
        toast(`抓取完成：${ok}/${r.results.length} 个站点成功`, ok === r.results.length ? "success" : "error");
        await loadDashboard();
        await refreshSitesList();
        refreshGlobalStatus();
      } catch (err) { toast("抓取失败：" + err.message, "error"); }
      finally { btn.disabled = false; btn.textContent = "立即抓取全部"; }
    });
    $("#btn-fetch")?.addEventListener("click", async e => {
      const btn = e.target; btn.disabled = true; btn.textContent = "抓取中…";
      try {
        const r = await api("/api/fetch_now", { method: "POST", body: JSON.stringify({ site_id: currentSiteId }) });
        const res = r.results?.[0] || {};
        toast(res.ok ? `抓取完成：模型 ${res.model_count}，变化 ${res.diffs_count}` : "抓取失败：" + (res.error || ""), res.ok ? "success" : "error");
        await loadDashboard();
      } catch (err) { toast("抓取失败：" + err.message, "error"); }
      finally { btn.disabled = false; btn.textContent = "立即抓取该站点"; }
    });
    $("#btn-test")?.addEventListener("click", async e => {
      const btn = e.target; btn.disabled = true; btn.textContent = "发送中…";
      try {
        const r = await api("/api/test_notify", { method: "POST", body: JSON.stringify({ site_id: currentSiteId }) });
        const results = r.notification_results || [];
        const ok = results.filter(x => x.ok).length;
        toast(`已发通知：${ok}/${results.length} 渠道成功`, ok === results.length ? "success" : "error");
        await loadDashboard();
      } catch (err) { toast("测试失败：" + err.message, "error"); }
      finally { btn.disabled = false; btn.textContent = "测试通知"; }
    });
    setInterval(loadDashboard, 30000);
  }

  if (page === "models") {
    let t = null;
    $("#search-input").addEventListener("input", () => { clearTimeout(t); t = setTimeout(loadModels, 250); });
    $("#sort-select").addEventListener("change", loadModels);
    $("#btn-refresh-models").addEventListener("click", loadModels);
    loadModels();
  }

  if (page === "changes") {
    $("#kind-filter").addEventListener("change", loadChanges);
    $("#limit-select").addEventListener("change", loadChanges);
    $("#btn-refresh-changes").addEventListener("click", loadChanges);
    $("#btn-reset").addEventListener("click", async () => {
      const scope = currentSiteId === "all" ? "所有站点" : `站点 ${currentSiteId}`;
      if (!confirm(`确定重置${scope}的基准？将清空变更记录，下次抓取会重新评估。`)) return;
      try {
        const r = await api("/api/reset", { method: "POST", body: JSON.stringify({ site_id: currentSiteId === "all" ? null : currentSiteId }) });
        toast(`已清空 ${r.deleted} 条变更记录`, "success");
        await loadChanges();
      } catch (e) { toast("重置失败：" + e.message, "error"); }
    });
    loadChanges();
  }

  if (page === "config") {
    $("#config-form").addEventListener("submit", e => { e.preventDefault(); saveConfig(); });
    $("#btn-reload-config").addEventListener("click", () => { if (confirm("放弃修改重新加载？")) loadConfig(); });
    $("#btn-add-site").addEventListener("click", addNewSite);
    $("#btn-confirm-add-site")?.addEventListener("click", confirmAddNewSite);
    // 回车提交
    $("#new-base-url")?.addEventListener("keydown", e => { if (e.key === "Enter") confirmAddNewSite(); });
    const sysSel = $('[data-field="system"]');
    if (sysSel) sysSel.addEventListener("change", updateSystemGroups);
    loadConfig();
  }

  if (page === "logs") {
    $("#log-level-filter").addEventListener("change", loadLogs);
    $("#log-limit").addEventListener("change", loadLogs);
    $("#btn-refresh-logs").addEventListener("click", loadLogs);
    loadLogs();
  }

  refreshGlobalStatus();
  setInterval(refreshGlobalStatus, 30000);
});
