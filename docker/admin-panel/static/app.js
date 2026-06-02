/* ── PRTG Probe Admin — front-end controller (vanilla, no deps) ───────────── */
"use strict";

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const state = { view: "dashboard", timer: null, logFile: "Probe.log" };

async function api(path, opts) {
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch { /* non-json */ }
  if (!r.ok) throw new Error((body && body.error) || `HTTP ${r.status}`);
  return body;
}

function toast(msg, ok = true) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + (ok ? "ok" : "err");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = "toast"; }, 3800);
}

/* ── navigation ───────────────────────────────────── */
const VIEWS = ["dashboard", "config", "service", "logs", "health", "updates"];
function show(view) {
  if (!VIEWS.includes(view)) view = "dashboard";
  state.view = view;
  if (location.hash.slice(1) !== view) history.replaceState(null, "", "#" + view);
  $$(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.view === view));
  $$(".panel").forEach(p => p.classList.toggle("active", p.dataset.panel === view));
  $("#view-title").textContent = { dashboard: "Dashboard", config: "Configuration",
    service: "Service", logs: "Logs", health: "Health", updates: "Updates" }[view];
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  load(view);
  // auto-refresh for live views
  const every = { dashboard: 5000, service: 5000, health: 8000, logs: 4000, updates: 5000 }[view];
  if (every) state.timer = setInterval(() => load(view, true), every);
}

function load(view, quiet) {
  ({ dashboard: loadDashboard, config: loadConfig, service: loadService,
     logs: loadLogs, health: loadHealth, updates: loadUpdates }[view] || (() => {}))(quiet);
}

/* ── shared: connection pill (kept live on every view) ─ */
async function refreshPill() {
  try { setConnPill(await api("/api/dashboard")); } catch { /* leave as-is */ }
}
function setConnPill(d) {
  const p = $("#conn-pill");
  if (d.connected) { p.className = "pill pill-green"; p.textContent = "● Connected"; }
  else if (d.probe_running) { p.className = "pill pill-orange"; p.textContent = "● Probe up · no core link"; }
  else { p.className = "pill pill-red"; p.textContent = "● Disconnected"; }
  $("#side-meta").innerHTML =
    `<b>${esc(d.probe_name || "—")}</b><br>${esc(d.core_server || "—")}:${esc(d.core_port || "")}` +
    `<br>up ${esc(d.container_uptime || "—")}`;
}

/* ── Dashboard ────────────────────────────────────── */
async function loadDashboard(quiet) {
  let d;
  try { d = await api("/api/dashboard"); } catch (e) { if (!quiet) toast(e.message, false); return; }
  setConnPill(d);
  const m = d.modules || {};
  const cards = [
    { cls: d.connected ? "good" : "bad", label: "Core connection",
      value: d.connected ? "Connected" : "Disconnected",
      sub: d.peer ? "peer " + d.peer : "no ESTABLISHED socket" },
    { cls: "accent", label: "Core server", value: d.core_server || "—",
      sub: "port " + (d.core_port || "—") },
    { cls: "", label: "Probe name", value: d.probe_name || "—",
      sub: (d.gid || "").slice(0, 18) + (d.gid ? "…" : "") },
    { cls: m.failed ? "bad" : "good", label: "Modules loaded",
      value: `${m.loaded ?? "?"}/${m.total ?? "?"}`,
      sub: m.failed ? `${m.failed} failed` : "all loaded" },
    { cls: "accent", label: "Container uptime", value: d.container_uptime || "—",
      sub: "probe " + (d.probe_uptime || "—") },
    { cls: d.probe_running ? "good" : "bad", label: "Probe process",
      value: d.probe_running ? "Running" : "Stopped",
      sub: d.probe_pid ? "pid " + d.probe_pid : "" },
    { cls: "", label: "Last login", value: (d.last_login || "—").split(" ")[1] || "—",
      sub: (d.last_login || "").split(" ")[0] || "no Login OK" },
    { cls: "", label: "Sensor activity", value: String(d.sensor_activity ?? "—"),
      sub: "distinct IDs (recent log)" },
  ];
  $("#dash-cards").innerHTML = "";
  cards.forEach(c => $("#dash-cards").append(
    el("div", "card stat " + c.cls,
      `<div class="label">${esc(c.label)}</div><div class="value">${esc(c.value)}</div>` +
      `<div class="sub">${esc(c.sub)}</div>`)));

  // modules card
  const pct = m.total ? Math.round((m.loaded / m.total) * 100) : 0;
  let mod = `<div class="mono" style="font-size:13px">${m.loaded ?? 0} of ${m.total ?? 0} loaded` +
    `${m.failed ? ` · <span style="color:var(--red)">${m.failed} failed</span>` : ""}</div>` +
    `<div class="mod-bar"><span style="width:${pct}%"></span></div><div class="chips">`;
  const fails = new Set((m.failures || []).map(f => f.module));
  (m.modules || []).forEach(n => mod += `<span class="chip${fails.has(n) ? " bad" : ""}">${esc(n)}</span>`);
  mod += "</div>";
  if (m.failures && m.failures.length)
    mod += `<div class="muted" style="margin-top:10px">Failures: ` +
      m.failures.map(f => `${esc(f.module)} (${esc(f.reason)})`).join("; ") + "</div>";
  $("#dash-modules").innerHTML = mod;

  // connection card
  $("#dash-conn").innerHTML =
    `<dl class="kv">
      <dt>Status</dt><dd>${pill(d.connected ? "green" : "red", d.connected ? "ESTABLISHED" : "down")}</dd>
      <dt>Core</dt><dd class="mono">${esc(d.core_server)}:${esc(d.core_port)}</dd>
      <dt>Peer socket</dt><dd class="mono">${esc(d.peer || "—")}</dd>
      <dt>GID</dt><dd class="mono">${esc(d.gid || "—")}</dd>
      <dt>Last login</dt><dd class="mono">${esc(d.last_login || "—")}</dd>
      <dt>Probe PID</dt><dd class="mono">${esc(d.probe_pid || "—")}</dd>
    </dl>`;
}
function pill(color, txt) { return `<span class="pill pill-${color}">${esc(txt)}</span>`; }

/* ── Configuration ────────────────────────────────── */
async function loadConfig() {
  let c;
  try { c = await api("/api/config"); } catch (e) { toast(e.message, false); return; }
  const f = $("#cfg-form");
  f.core_server.value = c.core_server || "";
  f.core_port.value   = c.core_port || "";
  f.probe_name.value  = c.probe_name || "";
  f.gid.value         = c.gid || "";
  f.data_path.value   = c.data_path || "";
  $("#key-form").key.value = "";
  $("#key-form").key.placeholder = c.access_key || "8 hex chars";
  $$("#loglevel-seg .seg-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.level === (c.debug_log ? "debug" : "normal")));

  const tb = $("#wmi-tbl tbody"); tb.innerHTML = "";
  const tg = c.wmi_targets || {};
  if (!Object.keys(tg).length) tb.append(el("tr", "", `<td colspan="4" class="muted">no targets configured</td>`));
  for (const [host, cr] of Object.entries(tg))
    tb.append(el("tr", "", `<td class="mono">${esc(host)}</td><td>${esc(cr.username)}</td>` +
      `<td>${esc(cr.domain || ".")}</td><td class="mono">${esc(cr.password || "—")}</td>`));

  if (c.read_only) $$(".panel[data-panel=config] button").forEach(b => b.disabled = true);
}

/* ── Service ──────────────────────────────────────── */
async function loadService(quiet) {
  let s;
  try { s = await api("/api/service"); } catch (e) { if (!quiet) toast(e.message, false); return; }
  const cards = [
    { cls: s.probe_running ? "good" : "bad", label: "Probe service",
      value: s.probe_running ? "Running" : "Stopped", sub: s.probe_pid ? "pid " + s.probe_pid : "" },
    { cls: s.wineserver ? "good" : "bad", label: "Wine server",
      value: s.wineserver ? "Up" : "Down", sub: "wineserver process" },
    { cls: s.service_registered ? "good" : "", label: "SCM registration",
      value: s.service_registered ? "Registered" : "Unknown", sub: "PRTGProbeService" },
  ];
  $("#svc-cards").innerHTML = "";
  cards.forEach(c => $("#svc-cards").append(
    el("div", "card stat " + c.cls,
      `<div class="label">${esc(c.label)}</div><div class="value">${esc(c.value)}</div>` +
      `<div class="sub">${esc(c.sub)}</div>`)));
}

async function serviceAction(act) {
  const labels = { "start": "Start probe", "stop": "Stop probe", "restart": "Restart probe",
    "restart-container": "Restart container" };
  if (act === "restart-container" && !confirm("Restart the entire probe container? The probe will be offline for ~30–60s.")) return;
  const out = $("#svc-output"); out.hidden = false; out.textContent = `▸ ${labels[act]}…`;
  $$(".btn-row .btn").forEach(b => b.disabled = true);
  try {
    const r = await api("/api/service/" + act, { method: "POST" });
    out.textContent = r.output || "(no output)";
    toast(`${labels[act]}: ${r.ok ? "ok" : "check output"}`, r.ok);
  } catch (e) { out.textContent = e.message; toast(e.message, false); }
  finally { $$(".btn-row .btn").forEach(b => b.disabled = false); setTimeout(() => loadService(true), 1500); }
}

/* ── Logs ─────────────────────────────────────────── */
async function loadLogs(quiet) {
  const level = $("#log-level").value, lines = $("#log-lines").value;
  const v = $("#log-view");
  const atBottom = v.scrollHeight - v.scrollTop - v.clientHeight < 40;
  try {
    const r = await api(`/api/logs/${encodeURIComponent(state.logFile)}?lines=${lines}` +
      (level ? `&level=${level}` : ""));
    const rows = (r.lines || []).map(line => {
      const m = line.match(/\s(DEBG|INFO|NOTI|WARN|ALRT|ERRO)\s/);
      const cls = m ? "l-" + m[1] : "";
      return `<span class="${cls}">${esc(line)}</span>`;
    });
    v.innerHTML = rows.join("\n") || "(empty)";
    if ($("#log-follow").checked && (atBottom || !quiet)) v.scrollTop = v.scrollHeight;
  } catch (e) { if (!quiet) v.textContent = "error: " + e.message; }
}

/* ── Health ───────────────────────────────────────── */
async function loadHealth(quiet) {
  let h;
  try { h = await api("/api/health"); } catch (e) { if (!quiet) toast(e.message, false); return; }
  const g = $("#health-grid"); g.innerHTML = "";
  const order = ["engine_a", "engine_b", "engine_c", "wmi_facade", "psrp", "capture", "wine_prefix"];
  order.forEach(k => {
    const x = h[k]; if (!x) return;
    const color = x.ok ? "green" : "red";
    g.append(el("div", "hcard " + (x.ok ? "ok" : "bad"),
      `<span class="dot ${color} hdot"></span><div><div class="hname">${esc(x.name)}</div>` +
      `<div class="hdetail">${esc(x.detail)}</div></div>`));
  });
}

/* ── Updates ──────────────────────────────────────── */
const UPD_STATUS = { idle: "grey", building: "orange", built: "green", applying: "orange",
  applied: "green", error: "red", unavailable: "grey" };
async function loadUpdates(quiet) {
  let u;
  try { u = await api("/api/updates"); } catch (e) { if (!quiet) toast(e.message, false); return; }
  const p = u.pending || null;
  const cards = [
    { cls: u.updater_available ? "good" : "bad", label: "Updater",
      value: u.updater_available ? "Online" : "Offline", sub: "prtg-updater sidecar" },
    { cls: "accent", label: "Current version", value: u.current_version || "—",
      sub: u.current_image || "" },
    { cls: p ? "good" : "", label: "Pending update", value: p ? p.version : "none",
      sub: p ? p.image : (u.status === "building" ? "building…" : "up to date") },
    { cls: UPD_STATUS[u.status] === "red" ? "bad" : (p ? "good" : ""), label: "Status",
      value: (u.status || "—"), sub: u.auto_apply ? "auto-apply ON" : "manual apply" },
  ];
  $("#upd-cards").innerHTML = "";
  cards.forEach(c => $("#upd-cards").append(
    el("div", "card stat " + c.cls,
      `<div class="label">${esc(c.label)}</div><div class="value">${esc(c.value)}</div>` +
      `<div class="sub">${esc(c.sub)}</div>`)));

  $("#upd-pending").innerHTML = p
    ? `<dl class="kv"><dt>New image</dt><dd class="mono">${esc(p.image)}</dd>` +
      `<dt>Build</dt><dd class="mono">${esc(p.version)}</dd>` +
      `<dt>Installer</dt><dd class="mono">${esc(p.installer)}</dd>` +
      `<dt>Built</dt><dd class="mono">${esc(p.built_at)}</dd></dl>`
    : `<p class="muted">No update built and waiting. The watchdog rebuilds automatically when the core pushes an installer; “Scan” forces a check (e.g. after dropping one in the dropzone).</p>`;

  $("#upd-apply").disabled = !(u.updater_available && p && u.status === "built");
  $("#upd-rollback").disabled = !(u.updater_available && u.previous_image);
  $("#upd-msg").textContent = u.message || "";

  // history
  $("#upd-history").innerHTML = (u.history || []).slice(0, 12).map(h =>
    `<div class="kv-row"><span class="mono muted">${esc((h.ts || "").replace("T", " ").replace("Z", ""))}</span> ` +
    `<b>${esc(h.event)}</b> <span class="muted">${esc(h.detail || "")}</span></div>`).join("") ||
    `<p class="muted">no activity yet</p>`;

  // build log (only while building/built/error, to avoid noise)
  if (["building", "built", "error", "applying"].includes(u.status)) {
    try {
      const lg = await api("/api/updates/log?n=300");
      const v = $("#upd-log");
      v.textContent = (lg.lines || []).join("\n") || "—";
      v.scrollTop = v.scrollHeight;
    } catch { /* ignore */ }
  } else {
    $("#upd-log").textContent = "(no build running)";
  }
}

async function updatesAction(act) {
  if (act === "apply" && !confirm("Recreate the probe container on the new image? Brief monitoring gap (~30–60s); identity & data persist.")) return;
  if (act === "rollback" && !confirm("Roll the probe back to the previous image?")) return;
  $$("#upd-actions .btn").forEach(b => b.disabled = true);
  try {
    const r = await api("/api/updates/" + act, { method: "POST" });
    toast(r.message || (r.ok ? `${act} ok` : (r.error || "failed")), r.ok !== false);
  } catch (e) { toast(e.message, false); }
  finally { setTimeout(() => loadUpdates(true), 1200); }
}

/* ── wiring ───────────────────────────────────────── */
function init() {
  $$(".nav-item").forEach(n => n.addEventListener("click", e => { e.preventDefault(); show(n.dataset.view); }));
  $("#refresh-btn").addEventListener("click", () => {
    const b = $("#refresh-btn"); b.firstChild && (b.innerHTML = '<span class="spin">⟳</span>');
    load(state.view); setTimeout(() => (b.innerHTML = "⟳"), 500);
  });

  // config save
  $("#cfg-form").addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = { core_server: fd.get("core_server"), core_port: fd.get("core_port"),
      probe_name: fd.get("probe_name") };
    try {
      const r = await api("/api/config", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const bad = Object.entries(r).filter(([, v]) => v && v.ok === false);
      toast(bad.length ? "Some fields failed: " + bad.map(([k]) => k).join(", ") : "Config saved (restart to apply)", !bad.length);
    } catch (err) { toast(err.message, false); }
  });

  // key rotation
  $("#key-form").addEventListener("submit", async e => {
    e.preventDefault();
    const key = e.target.key.value.trim();
    if (!/^[0-9A-Fa-f]{8}$/.test(key)) return toast("Key must be 8 hex characters", false);
    if (!confirm("Rotate the probe access key? It must also match the core's key.")) return;
    try { await api("/api/config/key", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }) });
      toast("Access key rotated (restart to apply)"); e.target.key.value = "";
    } catch (err) { toast(err.message, false); }
  });

  // log level
  $("#loglevel-seg").addEventListener("click", async e => {
    const b = e.target.closest(".seg-btn"); if (!b) return;
    try { await api("/api/config/loglevel", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ level: b.dataset.level }) });
      $$("#loglevel-seg .seg-btn").forEach(x => x.classList.toggle("active", x === b));
      toast("Logging level → " + b.dataset.level);
    } catch (err) { toast(err.message, false); }
  });

  // service buttons
  $$(".btn-row .btn[data-act]").forEach(b => b.addEventListener("click", () => serviceAction(b.dataset.act)));

  // update buttons
  $$("#upd-actions .btn[data-uact]").forEach(b => b.addEventListener("click", () => updatesAction(b.dataset.uact)));

  // log toolbar
  $("#log-file-seg").addEventListener("click", e => {
    const b = e.target.closest(".seg-btn"); if (!b) return;
    $$("#log-file-seg .seg-btn").forEach(x => x.classList.toggle("active", x === b));
    state.logFile = b.dataset.log; loadLogs();
  });
  $("#log-level").addEventListener("change", () => loadLogs());
  $("#log-lines").addEventListener("change", () => loadLogs());

  window.addEventListener("hashchange", () => show(location.hash.slice(1)));
  show(location.hash.slice(1) || "dashboard");
  refreshPill();
  setInterval(refreshPill, 10000);   // keep sidebar status live on any tab
}
document.addEventListener("DOMContentLoaded", init);
