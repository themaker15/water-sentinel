"use strict";
/* Water Sentinel web UI. Plain JS + Chart.js (vendored, works offline). Talks to the FastAPI backend. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const pct = (v, d = 1) => (v == null ? "–" : (v * 100).toFixed(d) + "%");
const fmt = (v, d = 2) => (v == null || !isFinite(v) ? "–" : Number(v).toFixed(d));

const svg = (d) => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const SENSORS = {
  temp_c: { name: "Temperature", unit: "°C", dp: 2, icon: svg('<path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"/>') },
  tds_ppm: { name: "TDS", unit: "ppm", dp: 0, icon: svg('<path d="M12 2.7l5.7 5.7a8 8 0 1 1-11.4 0z"/><circle cx="10" cy="14" r="1"/><circle cx="14" cy="12" r="1"/><circle cx="13" cy="16.5" r="1"/>') },
  turb_v: { name: "Turbidity", unit: "V", dp: 3, icon: svg('<path d="M4 8h12M4 12h16M4 16h10M8 20h10"/>') },
  ph: { name: "pH", unit: "", dp: 2, icon: svg('<path d="M9 3h6M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3"/><path d="M7 15h10"/>') },
};
const LABELS = ["normal", "salt_lvl1", "salt_lvl2", "baking_soda_lvl1", "starch_lvl1", "hot_water", "vinegar_lvl1"];
/* All colours come from tokens.css, read at draw time so light/dark themes stay in sync.
   C.ok -> --ok, MODEL_COLORS.mspc -> --model-mspc, SENSOR_COLORS.ph -> --sensor-ph, seriesColor(2) -> --series-3 */
const tokenMap = (prefix) => new Proxy({}, { get: (_, k) => (typeof k === "string" ? css(prefix + k) : undefined) });
const C = tokenMap("--");
const MODEL_COLORS = tokenMap("--model-");
const SENSOR_COLORS = tokenMap("--sensor-");
const SERIES_N = 10;
const seriesColor = (i) => css("--series-" + ((i % SERIES_N) + 1));

const S = { st: null, lastT: 0, hist: [], charts: {}, report: null, cfg: null, lastEvent: null, audio: null,
            recWas: false, runDirty: false, tileKeys: [], scZoom: true, emb: { m: "pca", c: "label", data: null },
            ex: EXSTATE(), tr: TRSTATE(), role: null, shareUrl: null, share: null, turb: null, cmp: null, runs: [], archived: [] };

const LABEL_COLORS = {};
function registerLabels(labels) {   // distinct colour per label, stable order (alphabetical)
  [...new Set(labels)].filter((l) => l !== "normal").sort().forEach((l) => {
    if (!(l in LABEL_COLORS)) LABEL_COLORS[l] = Object.keys(LABEL_COLORS).length;   // series index, resolved at draw time
  });
}
function labelColor(label) {
  if (label === "normal") return C.ok;
  if (!(label in LABEL_COLORS)) registerLabels([label]);
  return seriesColor(LABEL_COLORS[label]);
}
function alpha(hex, a) {
  if (!/^#[0-9a-f]{6}$/i.test(hex || "")) return `color-mix(in srgb, ${hex} ${a * 100}%, transparent)`;
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-json */ }
  if (!r.ok || data.ok === false) throw new Error(data.error || data.detail || `HTTP ${r.status}`);
  return data;
}
function toast(msg, ms = 2600) {
  const t = $("toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("on"), ms);
}

/* ------------------------------------------------------------------ theme + charts */
function applyTheme() {
  let t = null;
  try { t = localStorage.getItem("ws-theme"); } catch (e) { /* storage blocked */ }
  if (t) document.documentElement.dataset.theme = t;
}
function chartDefaults() {
  Chart.defaults.font.family = css("--font-display");   // condensed numerals read cleanly on axes
  Chart.defaults.font.size = 13;
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--line");
  Chart.defaults.animation = false;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.plugins.legend.labels.boxWidth = 12;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
}
function mkChart(id, config) {
  if (S.charts[id]) S.charts[id].destroy();
  S.charts[id] = new Chart($(id), config);
  return S.charts[id];
}

/* ------------------------------------------------------------------ tabs */
function showTab(v) {
  if (isViewer() && ["record", "settings"].includes(v)) v = "live";
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("on", s.id === "v-" + v));
  if (v === "record") loadRuns();
  if (v === "model" || v === "viz") loadReport();
  if (v === "model" && typeof loadSnaps === "function") loadSnaps();
  if (v === "model" && !isViewer()) initTrainPicker(true);
  if (v === "settings") { loadConfig(); loadTurb(); loadShare(); }
  try { history.replaceState(null, "", "#" + v); } catch (e) { /* ignore */ }
}

/* ------------------------------------------------------------------ live */
function buildTiles(usePh) {
  const keys = ["temp_c", "tds_ppm", "turb_v"].concat(usePh ? ["ph"] : []);
  if (keys.join() === S.tileKeys.join()) return;
  S.tileKeys = keys;
  $("tiles").innerHTML = keys.map((k) => `
    <div class="tile"><div class="k"><span>${SENSORS[k].icon} ${SENSORS[k].name}</span><span class="z" id="z-${k}">z –</span></div>
    <div class="v"><span id="v-${k}">–</span><span class="u" id="u-${k}">${SENSORS[k].unit}</span></div>
    <div class="trend" id="tr-${k}"></div>
    <div class="tsub" id="sub-${k}"></div>
    <div class="spwrap"><canvas id="sp-${k}"></canvas></div><div class="spcap" id="cap-${k}"></div></div>`).join("");
  keys.forEach((k) => mkChart("sp-" + k, {
    type: "line",
    data: { labels: [], datasets: [{ data: [], borderColor: css("--accent"), borderWidth: 1.6, pointRadius: 0, tension: .3,
      fill: true, backgroundColor: alpha(C.accent, 0.12) }] },
    options: { plugins: { legend: { display: false }, tooltip: { enabled: false } },
      maintainAspectRatio: false, layout: { padding: { top: 6, right: 4 } },
      scales: {
        x: { display: false },   // the caption under the sparkline already says "Last 2 min"
        y: { display: false } } },   // min / max are printed in the caption below
  }));
}

function initScoreChart() {
  mkChart("cScore", {
    type: "line",
    data: { labels: [], datasets: [
      { label: "Score", data: [], borderColor: css("--accent"), borderWidth: 2, pointRadius: 0, tension: .25, fill: true,
        backgroundColor: alpha(C.accent, 0.12),
        segment: { borderColor: (c) => (c.p1.parsed.y > 1 ? css("--bad") : undefined) } },
      { label: "Alarm line", data: [], borderColor: css("--bad"), borderDash: [6, 5], borderWidth: 1.5, pointRadius: 0, fill: false },
    ] },
    options: { interaction: { mode: "index", intersect: false }, spanGaps: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: {
        label: (c) => (c.datasetIndex ? "Alarm line 1.00" : `Score ${fmt(c.chart.$raw ? c.chart.$raw[c.dataIndex] : c.parsed.y)}`) } } },
      scales: { x: { ticks: { maxTicksLimit: window.innerWidth < 600 ? 3 : 6, maxRotation: 0 }, grid: { display: false } },
        y: { type: "logarithmic", min: 0.01, suggestedMax: 3,
          ticks: { callback: (v) => ([0.01, 0.1, 1, 10, 100, 1000].includes(v) ? v : "") },
          title: { display: true, text: "Score / alarm line (log)" } } } },
  });
}

const HERO = {
  normal: ["", "Normal water"], anomaly: ["", "Abnormal water detected"], fault: ["", "Sensor problem"],
  drifting: ["", "Values drifting slowly"],
  starting: ["⏳", "Starting measurement…"], off: ["", "Monitoring is off"], no_model: ["", "No model yet"],
  no_data: ["", "No data from the sensor"],
};

function renderLive(st) {
  const d = st.detector || { status: "off" };
  const stale = st.link.age == null || st.link.age > 6;
  let s = stale ? "no_data" : d.status;
  if (!st.monitor_on && s !== "no_data") s = st.model.exists ? "off" : "no_model";
  const [icon, title] = HERO[s] || HERO.off;
  let sub = "";
  if (s === "normal") sub = `Score ${fmt(d.ratio)} (alarms above 1.00)`;
  else if (s === "anomaly") sub = (d.cause || []).map((c) => c.text).join(" · ") || "Several sensors changed at once";
  else if (s === "drifting") sub = ((d.drift && d.drift.trend) || []).slice(0, 2).map(trendText).join(" · ") + " · below the alarm line";
  else if (s === "fault") sub = d.fault_text;
  else if (s === "starting") sub = d.left_s != null ? `Waiting about ${d.left_s} s more to filter out noise` : "Preparing the noise filter";
  else if (s === "off") sub = "Turn on the switch on the right to start real-time anomaly detection";
  else if (s === "no_model") sub = "Record at least 5 normal-water runs, then press Train on the \"Train & compare\" tab";
  else if (s === "no_data") sub = st.link.msg;
  $("hero").className = "hero span-12 s-" + s;
  $("heroIcon").textContent = icon; $("heroTitle").textContent = title; $("heroSub").textContent = sub;
  const tg = $("monitorToggle");
  if (document.activeElement !== tg) tg.checked = !!st.monitor_on;
  tg.disabled = !st.model.exists;
  $("heroModel").textContent = st.model.exists ? `Model: ${st.model.active_name}` : "";
  const ap = $("liveAlarmPill");
  ap.hidden = !(s === "anomaly" && !$("exploreBox").hidden);
  ap.textContent = "Abnormal water alarm — tap to go back to live view";
  document.title = s === "anomaly" ? "Abnormal · Water Sentinel" : "Water Sentinel";

  buildTiles(st.use_ph);
  const L = st.latest || {};
  const staleNote = $("staleNote");
  if (stale && L.time) {
    const mins = Math.round((Date.now() / 1000 - L.time) / 60);
    staleNote.hidden = false;
    staleNote.textContent = `Values below are the last ones received at ${new Date(L.time * 1000).toLocaleString("en-GB")} (${mins < 1 ? "just now" : mins + " min ago"}), not current values`;
  } else staleNote.hidden = true;
  document.querySelectorAll("#tiles .tile").forEach((t) => t.classList.toggle("stale", stale));
  for (const k of S.tileKeys) {
    if (k === "turb_v") {
      const td = turbDisplay(L);
      $("v-" + k).textContent = L[k] == null ? "–" : td.val; $("u-" + k).textContent = td.unit; $("sub-turb_v").textContent = L[k] == null ? "" : td.sub;
      $("v-" + k).parentElement.title = td.title;
    } else $("v-" + k).textContent = fmt(L[k], SENSORS[k].dp);
    const z = d.z ? d.z[k] : null;
    const zEl = $("z-" + k);
    zEl.textContent = z == null ? "z –" : `z ${z > 0 ? "+" : ""}${fmt(z, 1)}`;
    zEl.classList.toggle("hot", z != null && Math.abs(z) >= 3);
    const tr = ((d.drift && d.drift.trend) || []).find((x) => x.sensor === k);
    $("tr-" + k).textContent = tr && !stale ? (tr.rate_per_min > 0 ? "↗ " : "↘ ") + trendText(tr).replace(tr.text + " ", "") : "";
    const ch = S.charts["sp-" + k];
    const tail = S.hist.slice(-120);
    ch.data.labels = tail.map((h) => new Date(h.t * 1000).toLocaleTimeString("en-GB", { hour12: false }));
    ch.data.datasets[0].data = tail.map((h) => h[k]);
    // never stretch tiny steps (e.g. 0.0625 °C) to full height: show at least one "floor" of range
    const vals = tail.map((h) => h[k]).filter((v) => v != null && isFinite(v));
    if (vals.length) {
      const lo = Math.min(...vals), hi = Math.max(...vals), mid = (lo + hi) / 2;
      const span = Math.max(hi - lo, (S.cfg && S.cfg.floors[k]) || 0) * 1.1 || 1;
      ch.options.scales.y.min = mid - span / 2; ch.options.scales.y.max = mid + span / 2;
      const dp = SENSORS[k].dp, u = k === "turb_v" ? "V" : SENSORS[k].unit;
      $("cap-" + k).innerHTML = `<span>Min <b>${fmt(lo, dp)}</b> · Max <b>${fmt(hi, dp)}</b> ${u}</span><span>Last 2 min</span>`;
    }
    ch.update("none");
  }

  const r = stale ? null : d.ratio;
  // graduated cylinder: 0 at the bottom, alarm line (1.0) at two thirds, full at 1.5 and above
  $("hero").style.setProperty("--level", r == null ? 0 : Math.max(0, Math.min(1, r / 1.5)));
  $("cylTube").classList.toggle("over", r != null && r > 1.5);
  $("heroScore").textContent = r == null ? "–" : fmt(r);
  $("ratioText").textContent = r == null ? (stale ? "No live data" : "–") : `Now ${fmt(r)}`;
  const sc = S.charts.cScore;
  const tail = S.hist.slice(-600);
  const step = (d && d.interval_s) || (st.model && st.model.interval_s) || 1;
  const labels = [], vals = [], raw = [];
  tail.forEach((h, i) => {
    if (i && h.t - tail[i - 1].t > Math.max(3 * step, 8)) { labels.push(""); vals.push(null); raw.push(null); } // gap = break the line
    labels.push(new Date(h.t * 1000).toLocaleTimeString("en-GB", { hour12: false }));
    vals.push(h.ratio == null ? null : Math.max(h.ratio, 0.01));
    raw.push(h.ratio);
  });
  sc.data.labels = labels;
  sc.data.datasets[0].data = vals;
  sc.data.datasets[1].data = labels.map(() => 1);
  sc.$raw = raw;
  sc.update("none");

  renderEvents(st.events);
  renderLikely(st);
  $("simCard").hidden = !st.sim;
  if (st.sim) document.querySelectorAll("#simChips .chip").forEach((c) => c.classList.toggle("on", c.dataset.s === st.sim_scenario));
}

function trendText(t) {
  const dp = SENSORS[t.sensor] ? Math.max(SENSORS[t.sensor].dp, 1) : 2;
  const r = `${t.rate_per_min > 0 ? "+" : ""}${fmt(t.rate_per_min, dp)} ${t.unit}/min`;
  return `${t.text} ${r}` + (t.eta_min != null ? ` (reaches the alarm line in ~${fmt(t.eta_min, 1)} min)` : "");
}

function renderEvents(evs) {
  const top = evs.length ? evs[0].id : 0;
  if (S.lastEvent === null) S.lastEvent = top;          // first load: don't replay old alarms
  else if (top > S.lastEvent) {
    evs.filter((e) => e.id > S.lastEvent).reverse().forEach(onNewEvent);
    S.lastEvent = top;
  }
  const kindIcon = { anomaly: "", recovered: "", fault: "", info: "", link_lost: "", link_restored: "", drift: "" };
  // collapse consecutive identical events (e.g. a probe lifted many times) into one row with a count
  const rows = [];
  for (const e of evs) {
    const last = rows[rows.length - 1];
    if (last && last.kind === e.kind && last.msg === e.msg && last.date === e.date) last.count++;
    else rows.push({ ...e, count: 1 });
  }
  let day = null;
  const today = new Date().toISOString().slice(0, 10);
  $("events").innerHTML = rows.length ? rows.map((e) => {
    const sep = e.date !== day ? `<li class="day">${e.date === today ? "Today" : esc(e.date)}</li>` : "";
    day = e.date;
    return `${sep}<li><time>${esc(e.time)}</time><div>
      <div class="k-${e.kind}">${kindIcon[e.kind] || ""} ${esc(e.msg)}${e.count > 1 ? ` <span class="tag" style="background:var(--idle-bg)">×${e.count}</span>` : ""}</div>
      ${e.sent && e.sent.length ? `<div class="sent">${e.sent.map((x) => `${x.channel} ${x.ok ? "✓" : "✗ " + esc(x.error || "")}`).join(" · ")}</div>` : ""}
    </div></li>`;
  }).join("") : `<li class="hint">No events yet</li>`;
}

/* ------------------------------------------------------------------ alerts in the browser */
function unlockAudio() {
  try {
    if (!S.audio) S.audio = new (window.AudioContext || window.webkitAudioContext)();
    if (S.audio.state === "suspended") S.audio.resume();
  } catch (e) { /* no audio */ }
}
function beep(freqs, dur = 0.18, gap = 0.08, vol = 0.25) {
  if (!S.audio || !(S.cfg ? S.cfg.alerts.sound : true)) return;
  let t = S.audio.currentTime + 0.02;
  for (const f of freqs) {
    const o = S.audio.createOscillator(), g = S.audio.createGain();
    o.type = "sine"; o.frequency.value = f; o.connect(g); g.connect(S.audio.destination);
    g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(vol, t + 0.02); g.gain.linearRampToValueAtTime(0, t + dur);
    o.start(t); o.stop(t + dur + 0.02); t += dur + gap;
  }
}
function onNewEvent(e) {
  if (e.kind === "anomaly") {
    beep([880, 660, 880, 660, 880]);
    notify("Abnormal water", e.msg);
  } else if (e.kind === "recovered") {
    beep([523, 784], 0.15);
    notify("Back to normal", e.msg);
  } else if (e.kind === "fault") {
    beep([300], 0.25, 0, 0.15);
  } else if (e.kind === "drift") {
    beep([523, 587], 0.12, 0.05, 0.15);
    notify("Values drifting slowly", e.msg);
  } else if (e.kind === "link_lost") {
    beep([440, 330], 0.3);
    notify("Sensor link lost", e.msg);
  }
}
function notify(title, body) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (S.cfg && !S.cfg.alerts.browser) return;
  try { new Notification(title, { body, tag: "water-sentinel", renotify: true }); } catch (e) { /* ignore */ }
}
async function askNotify() {
  unlockAudio();
  if ("Notification" in window && Notification.permission === "default") await Notification.requestPermission();
  beep([660, 880], 0.12);
  notifState();
  toast("Alarm sound on");
}
function notifState() {
  const el = $("notifState");
  if (!("Notification" in window)) { el.textContent = "This browser does not support notifications"; return; }
  el.textContent = { granted: "Allowed ✓", denied: "Blocked: allow it in the browser settings", default: "Not allowed yet" }[Notification.permission];
}

/* ------------------------------------------------------------------ polling */
async function poll() {
  try {
    const st = await api(`/api/state?since=${S.lastT}`);
    S.st = st;
    applyRole(st);
    for (const h of st.history) S.hist.push(h);
    if (S.hist.length > 900) S.hist = S.hist.slice(-900);
    if (st.history.length) S.lastT = st.history[st.history.length - 1].t;
    renderHeader(st); renderLive(st); renderRec(st);
    if ($("v-settings").classList.contains("on")) updateTurbLive();
  } catch (e) {
    $("linkText").textContent = "Cannot reach the app (is it closed?)"; $("linkDot").className = "dot bad";
  }
  setTimeout(poll, isViewer() ? 2000 : 1000);
}
function renderHeader(st) {
  const stale = st.link.age == null || st.link.age > 6;
  $("linkDot").className = "dot " + (st.link.ok && !stale ? "ok" : "bad");
  $("linkText").textContent = st.link.msg;
  const m = st.model;
  let mp = !m.exists ? "No model" : `Model: ${m.features.length} sensors · ${m.created}`;
  if (m.exists && m.mismatch) mp = "⚠ The model does not match the selected sensors: retrain";
  else if (m.exists && m.new_runs) mp = `⚠ ${m.new_runs} new runs not trained on yet`;
  $("modelPill").textContent = mp;
  $("modelPill").classList.toggle("warn", !!(m.exists && (m.mismatch || m.new_runs)));
  const key = m.exists ? m.created + "|" + (m.features || []).join(",") : "";
  if (key !== S.modelSelKey && typeof refreshModelSel === "function") { S.modelSelKey = key; refreshModelSel(); }
  const fw = $("fwPill");
  fw.hidden = st.firmware !== "legacy";
  fw.textContent = "Old firmware (samples every 2.3 s): upload water_sentinel.ino";
}

/* ------------------------------------------------------------------ record */
async function loadRuns() {
  try {
    const r = await api("/api/runs");
    if (!S.runDirty && !(S.st && S.st.rec)) $("recRun").value = r.next_run;
    const runs = r.runs;
    S.runs = runs; S.archived = r.archived || [];
    const src = { record: "Save", auto: "Automatic (at alarm)", snapshot: "Saved from history" };
    $("runsBody").innerHTML = runs.length ? runs.slice().reverse().map((x) => `<tr class="${x.excluded ? "muted" : ""}"><td>${x.run ?? "-"}</td>
      <td><span class="tag" style="background:${alpha(labelColor(x.label), .18)};color:${labelColor(x.label)}">${esc(x.label)}</span>
        ${x.label === "unlabeled" ? '<span class="tag" style="background:var(--warn-bg);color:var(--warn)">Needs a label</span>' : ""}</td>
      <td>${esc(x.time)}</td><td>${esc(x.session)}</td><td class="num">${x.rows}</td><td class="num">${fmt(x.interval_s, 1)} s</td>
      <td>${esc(src[x.source] || x.source)}</td><td style="white-space:normal;min-width:140px">${esc(x.note)}</td>
      <td><button class="btn ghost sm" data-edit="${esc(x.file)}">Edit</button>
        <button class="btn ghost sm" data-excl="${esc(x.file)}">${x.excluded ? "Apply" : "Not used"}</button>
        <button class="btn ghost sm" data-arch="${esc(x.file)}" title="Move to data/_excluded (not deleted)">Archive</button></td></tr>`).join("")
      : `<tr><td colspan="9" class="hint">No data yet</td></tr>`;
    const usable = runs.filter((x) => x.rows > 0 && !x.excluded);
    const nN = usable.filter((x) => x.label === "normal").length;
    const nA = usable.filter((x) => !["normal", "unlabeled"].includes(x.label)).length;
    const nU = usable.filter((x) => x.label === "unlabeled").length;
    const nS = new Set(usable.filter((x) => x.label === "normal").map((x) => x.session)).size;
    $("runsSummary").textContent = `normal ${nN} runs (${nS} water sessions) · anomaly ${nA} runs${nU ? ` · ${nU} waiting for a label` : ""}`;
    $("recPlan").innerHTML = `<div class="notice ${nS >= 3 ? "" : "warn"}">${nS >= 3 ? "✓" : "○"} Normal water ${nS}/3 sessions (different cup or day) · ${nN} runs</div>
      <div class="notice ${nA >= 2 ? "" : "warn"}">${nA >= 2 ? "✓" : "○"} anomaly ${nA} runs (at least 2 kinds recommended)</div>
      ${nU ? `<div class="notice warn">${nU} runs were saved automatically during alarms. Press "Edit" to give each a condition (e.g. salt_lvl1) so it can be used for training</div>` : ""}`;
    $("runsBody").querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => editRun(b.dataset.edit)));
    $("runsBody").querySelectorAll("[data-excl]").forEach((b) => (b.onclick = () => toggleExcluded(b.dataset.excl)));
    $("runsBody").querySelectorAll("[data-arch]").forEach((b) => (b.onclick = () => archiveRun(b.dataset.arch)));
    exAfterChange(runs);
    if (!isViewer() && $("trPicker") && S.tr.inited && $("v-model").classList.contains("on")) initTrainPicker(true);
  } catch (e) { toast(e.message); }
}
async function editRun(file) {
  const x = S.runs.find((y) => y.file === file);
  const label = prompt(`Condition (label) for run ${x.run}\ne.g. normal, salt_lvl1, starch_lvl1`, x.label);
  if (label === null) return;
  const note = prompt("Note (which cup, what was added, how much)", x.note || "");
  if (note === null) return;
  try { await api("/api/run/meta", { file, label: label.trim(), note: note.trim() }); toast("Saved"); loadRuns(); }
  catch (e) { toast(e.message, 4000); }
}

function renderRec(st) {
  const r = st.rec;
  $("btnRec").disabled = !!r; $("btnRecStop").disabled = !r;
  $("markBox").hidden = !r;
  if (r) {
    $("markList").innerHTML = (r.markers || []).slice().reverse().map((m) =>
      `<li><time>${fmt(m.t_s, 0)} s</time><div>${esc(m.text)}${m.dose != null ? ` · ${m.dose}` : ""}</div></li>`).join("");
  }
  if (r) {
    $("recBar").style.width = Math.min(100, (r.elapsed / (r.minutes * 60)) * 100) + "%";
    $("recRows").textContent = r.rows;
    $("recTime").innerHTML = `${Math.round(r.elapsed)}<span class="u">/ ${Math.round(r.minutes * 60)} s</span>`;
    $("recMsg").textContent = `Recording run ${r.run} (${r.label})`;
  } else if (S.recWas) {
    $("recBar").style.width = "0%";
    $("recMsg").textContent = st.rec_last ? `Done: run ${st.rec_last.run}, ${st.rec_last.rows} rows` : "";
    S.runDirty = false;
    loadRuns();
    toast("This run is finished");
  }
  S.recWas = !!r;
}

/* ------------------------------------------------------------------ model report */
async function loadReport() {
  try {
    S.report = await api("/api/report");
    registerLabels(S.report.runs.map((r) => r.label));
  } catch (e) {
    S.report = null;
  }
  renderReport(); renderViz(); setupTuning();
  if ($("trBody")) renderTrBody();
}

function renderReport() {
  const box = $("modelResults");
  const rep = S.report;
  if (!rep) { box.innerHTML = `<div class="card"><p class="hint">No training results yet</p></div>`; return; }
  const meta = rep.meta, models = Object.values(rep.models);
  const active = S.st && S.st.model.active;
  const hasAnom = meta.anomaly_runs.length > 0;
  const hasTest = (meta.test_runs || []).length > 0 && models.some((m) => m.test_false_alarm != null);
  const best = rep.models[meta.best];
  const cls = (v, goodHigh, okv) => (v == null ? "" : (goodHigh ? (v >= okv ? "good" : "poor") : (v <= okv ? "good" : "poor")));
  box.innerHTML = `
  <div class="grid">
    <div class="card span-12">
      <div class="row"><h2>Recommended: ${esc(best.name)}</h2><span class="spacer"></span><span class="hint" style="margin:0">Trained ${esc(meta.created)}</span></div>
      <p class="hint">${hasAnom ? "Highest balanced accuracy with false alarms at or below 2%" : "No anomaly runs yet, so only false alarms can be compared (record anomaly runs for a full comparison)"}</p>
      <div class="kpis">
        <div class="tile"><div class="k">False alarms on normal water</div><div class="v ${cls(best.false_alarm, false, .02)}">${pct(best.false_alarm)}</div></div>
        ${hasTest ? `<div class="tile"><div class="k">False alarms on held-out test runs</div><div class="v ${cls(best.test_false_alarm, false, .02)}">${pct(best.test_false_alarm)}</div></div>` : ""}
        <div class="tile"><div class="k">Abnormal water detected</div><div class="v ${cls(best.detection, true, .8)}">${pct(best.detection)}</div></div>
        <div class="tile"><div class="k">AUC</div><div class="v">${fmt(best.auc, 3)}</div></div>
        <div class="tile"><div class="k">Time to alarm (median)</div><div class="v">${best.delay_s == null ? "–" : Math.round(best.delay_s)}<span class="u">s</span></div></div>
      </div>
      <h3>Early warning while values drift</h3>
      <div class="kpis">
        <div class="tile"><div class="k">False early warnings on normal water</div><div class="v">${rep.models.mspc.drift_false_per_hour == null ? "–" : fmt(rep.models.mspc.drift_false_per_hour, 1)}<span class="u">per hour</span></div></div>
        <div class="tile"><div class="k">Early-warning lead time (median)</div><div class="v">${rep.models.mspc.drift_lead_s == null ? "–" : Math.round(rep.models.mspc.drift_lead_s)}<span class="u">s</span></div></div>
        <div class="tile"><div class="k">CUSUM sensitivity (h per sensor)</div><div class="v" style="font-size:18px">${(meta.drift ? meta.drift.h : []).map((h, i) => `${SENSORS[meta.features[i]].name} ${fmt(h, 0)}`).join(" · ") || "–"}</div></div>
      </div>
      <p class="hint" style="margin-top:12px">Data: normal ${meta.normal_runs.length} runs${(meta.test_runs || []).length ? ` (+ test-only ${meta.test_runs.length})` : ""}, anomaly ${meta.anomaly_runs.length} runs, ${meta.n_train_rows} training rows${meta.dataset ? ` · dataset "${esc(meta.dataset)}"` : ""} ·
        Sensors: ${meta.features.map((f) => SENSORS[f].name).join(", ")} · evaluation: ${esc(meta.protocol)}</p>
      ${!hasAnom ? `<div class="notice warn">No abnormal-water runs yet, so detection cannot be measured (only false alarms can be compared for now)
        <button class="btn ghost sm" onclick="showTab('record')">Record anomaly runs</button></div>` : ""}
      ${(meta.n_sessions ?? 0) < 3 ? `<div class="notice warn">Normal water comes from ${meta.n_sessions ?? "?"} sessions (${meta.normal_runs.length} runs): runs in the same water are not independent, so false-alarm results look better than they are. Record at least 3 sessions of normal water</div>` : ""}
      ${rep.skipped.length ? `<details><summary>Unused files (${rep.skipped.length})</summary><ul class="hint">${rep.skipped.map((s) => `<li>${esc(s.file)}: ${esc(s.reason)}</li>`).join("")}</ul></details>` : ""}
    </div>
    <div class="card span-12">
      <h2>Model comparison</h2>
      <p class="hint">Leave-one-run-out evaluation: every number comes from runs the model never saw in training · Press "Use" to choose the model used for monitoring</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Model</th><th class="num">False alarms ↓</th>${hasTest ? '<th class="num">False alarms on test runs ↓</th>' : ""}<th class="num">Detected ↑</th><th class="num">Balanced acc ↑</th><th class="num">AUC ↑</th>
          <th class="num">Time to alarm</th><th class="num">Anomaly runs caught</th><th class="num">Speed/sample</th><th></th></tr></thead>
        <tbody>${models.map((m) => `<tr class="${m.key === meta.best ? "best" : ""}">
          <td><span class="tag" style="background:${alpha(MODEL_COLORS[m.key], .18)};color:${MODEL_COLORS[m.key]}">●</span> ${esc(m.name)}${m.key === meta.best ? " " : ""}</td>
          <td class="num ${cls(m.false_alarm, false, .02)}">${pct(m.false_alarm)}</td>
          ${hasTest ? `<td class="num ${cls(m.test_false_alarm, false, .02)}">${pct(m.test_false_alarm)}</td>` : ""}
          <td class="num ${cls(m.detection, true, .8)}">${pct(m.detection)}</td>
          <td class="num">${pct(m.balanced_acc)}</td><td class="num">${fmt(m.auc, 3)}</td>
          <td class="num">${m.delay_s == null ? "–" : Math.round(m.delay_s) + " s"}</td>
          <td class="num">${hasAnom ? `${m.runs_caught}/${meta.anomaly_runs.length}` : "–"}</td>
          <td class="num">${m.score_us < 1000 ? Math.round(m.score_us) + " µs" : (m.score_us / 1000).toFixed(1) + " ms"}</td>
          <td>${m.key === active ? `<span class="tag" style="background:${alpha(C.ok, .2)};color:var(--ok)">In use</span>`
            : `<button class="btn ghost sm" data-use="${m.key}">Use</button>`}</td></tr>`).join("")}</tbody></table></div>
      <div class="chart-box" style="margin-top:14px"><canvas id="cCompare"></canvas></div>
      <details><summary>What the metrics mean</summary><ul class="hint" style="line-height:1.8">
        <li><b>False alarm</b>: share of time the model alarms on normal water, measured on normal runs held out from training</li>
        <li><b>Detection</b>: share of time the model alarms during anomaly runs (includes the time before the additive is mixed in, so it never reaches 100%)</li>
        <li><b>Balanced accuracy</b> = (1 − false alarm + detection) / 2, not biased by how much data each side has</li>
        <li><b>AUC</b>: how well normal and abnormal are separated, independent of the alarm line. High AUC with many false alarms means the model separates well but the line is too tight</li>
        <li><b>Time to alarm</b>: seconds from the event "Additive" (or from the start of the anomaly run) to the first alarm</li>
        <li><b>Runs with additive events</b>: time before the first addition counts as normal water (for false alarms), time after counts as abnormal</li>
        <li><b>Drift (CUSUM)</b>: accumulates small differences that keep going the same way, warning before the main alarm line, without moving the baseline (so it does not get "used to" slow contamination)</li></ul></details>
    </div>
    <div class="card span-12">
      <h2>Per-run results (final model trained on every normal run)</h2>
      <p class="hint">Normal runs marked train in this table were used for training, so their numbers look better than reality (test runs never were). Use the comparison table above as the main result</p>
      <div class="table-wrap"><table><thead><tr><th>Run</th><th>Condition</th><th class="num">Rows</th><th class="num">Sensor errors</th>
        ${models.map((m) => `<th class="num">${esc(m.key.toUpperCase())}</th>`).join("")}<th class="num">Alarm after addition</th><th class="num">Early warning</th><th class="num">Minimum dose</th><th>Biggest change</th></tr></thead>
        <tbody>${rep.runs.map((r) => `<tr><td>${r.run_id}</td>
          <td><span class="tag" style="background:${alpha(labelColor(r.label), .18)};color:${labelColor(r.label)}">${esc(r.label)}</span>${r.role === "test" ? ' <span class="tag" style="background:var(--info-bg);color:var(--info)">test</span>' : ""}</td>
          <td class="num">${r.rows}</td><td class="num">${r.faults || ""}</td>
          ${models.map((m) => { const v = r.models[m.key].alarm_rate; const good = r.role !== "anomaly" ? v <= .02 : v >= .5;
            return `<td class="num ${good ? "good" : "poor"}">${pct(v, 0)}</td>`; }).join("")}
          <td class="num">${r.role === "anomaly" && r.models.mspc.first_alarm_s != null ? Math.round(r.models.mspc.first_alarm_s) + " s" : "–"}</td>
          <td class="num">${r.lead_s != null ? Math.round(r.lead_s) + " s" : "–"}</td>
          <td class="num">${r.min_dose != null ? fmt(r.min_dose, 1) : "–"}</td>
          <td>${(r.cause || []).map((c) => esc(c.text)).join(", ")}</td></tr>`).join("")}</tbody></table></div>
      <p class="hint" style="margin-top:8px">"Alarm after addition" counted from the event "Additive" if any (otherwise from the start of the run) · "Early warning" = how many seconds drift warns before the main alarm · "Minimum dose" = cumulative dose when the alarm starts</p>
    </div>
    ${causeModelHtml(meta)}
  </div>`;
  box.querySelectorAll("[data-use]").forEach((b) => (b.onclick = async () => {
    try { await api("/api/model", { key: b.dataset.use }); toast("Model switched"); setTimeout(renderReport, 1200); } catch (e) { toast(e.message); }
  }));
  mkChart("cCompare", {
    type: "bar",
    data: { labels: models.map((m) => m.name.split(" (")[0]), datasets: [
      { label: "False alarms (%)", data: models.map((m) => (m.false_alarm ?? 0) * 100), backgroundColor: alpha(C.bad, .75), borderRadius: 6 },
      { label: "Detected (%)", data: models.map((m) => (m.detection ?? 0) * 100), backgroundColor: alpha(C.ok, .75), borderRadius: 6 },
      { label: "AUC × 100", data: models.map((m) => (m.auc ?? 0) * 100), backgroundColor: alpha(C.accent, .75), borderRadius: 6 },
    ] },
    options: { scales: { y: { min: 0, max: 100 }, x: { grid: { display: false } } }, plugins: { legend: { position: "bottom" } } },
  });
}

/* ------------------------------------------------------------------ visualization */
function renderViz() {
  const rep = S.report;
  $("vizEmpty").hidden = !!rep;
  document.querySelectorAll(".viz-on").forEach((el) => (el.hidden = !rep));
  if (!rep) return;
  const sel = $("tlModel");
  const keys = Object.keys(rep.timelines);
  if (sel.options.length !== keys.length) {
    sel.innerHTML = keys.map((k) => `<option value="${k}">${esc(rep.model_names[k])}</option>`).join("");
    sel.value = (S.st && S.st.model.active) || rep.meta.best;
  }
  drawTimeline(); drawRoc(); setupScatter(); setupRunDetail(); drawDose(); drawEmbedding();
}

function drawTimeline() {
  const rep = S.report, key = $("tlModel").value;
  let off = 0;
  const ds = [];
  const ticks = [];
  for (const run of rep.timelines[key]) {
    const pts = run.t.map((t, i) => ({ x: off + t - run.t[0], y: Math.max(run.ratio[i], 0.01), run: run.run_id, label: run.label }));
    ticks.push({ x: off, text: `#${run.run_id}` });
    off += (run.t[run.t.length - 1] - run.t[0]) + 25;
    ds.push({ label: `Run ${run.run_id} · ${run.label}`, data: pts, borderColor: labelColor(run.label), backgroundColor: labelColor(run.label),
      borderWidth: 1.6, pointRadius: 0, tension: .2 });
  }
  ds.push({ label: "Alarm line", data: [{ x: 0, y: 1 }, { x: off, y: 1 }], borderColor: css("--text"), borderDash: [6, 5], borderWidth: 1.2, pointRadius: 0 });
  mkChart("cTimeline", {
    type: "line", data: { datasets: ds },
    options: { parsing: true, interaction: { mode: "nearest", intersect: false },
      plugins: { legend: { position: "bottom", labels: { filter: (it, d) => {
        // one legend entry per label, not per run
        const lab = d.datasets[it.datasetIndex].label.split(" · ")[1];
        return !lab || d.datasets.findIndex((x) => x.label.split(" · ")[1] === lab) === it.datasetIndex;
      }, generateLabels: (ch) => Chart.defaults.plugins.legend.labels.generateLabels(ch).map((l) => ({ ...l, text: l.text.includes(" · ") ? l.text.split(" · ")[1] : l.text })) } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmt(c.parsed.y)}` } } },
      scales: { x: { type: "linear", title: { display: true, text: "Time (s) · runs placed end to end" }, grid: { display: false } },
        y: { type: "logarithmic", min: 0.01, title: { display: true, text: "Score / alarm line (log)" } } } },
  });
}

// vertical lines (markers, change points) without an extra Chart.js plugin download
const vlinesPlugin = {
  id: "vlines",
  beforeDatasetsDraw(chart, _args, opts) {
    const xs = chart.scales.x; const ys = chart.scales.y;
    if (!opts || !opts.bands || !xs) return;
    const g = chart.ctx; g.save();
    for (const b of opts.bands) {
      const x0 = Math.max(xs.getPixelForValue(b.x0), xs.left), x1 = Math.min(xs.getPixelForValue(b.x1), xs.right);
      if (x1 > x0) { g.fillStyle = b.color; g.fillRect(x0, ys.top, x1 - x0, ys.bottom - ys.top); }
    }
    g.restore();
  },
  afterDatasetsDraw(chart, _args, opts) {
    const xs = chart.scales.x; const ys = chart.scales.y;
    if (!opts || !opts.lines || !xs) return;
    const g = chart.ctx; g.save();
    for (const l of opts.lines) {
      const px = xs.getPixelForValue(l.x);
      if (px < xs.left || px > xs.right) continue;
      g.strokeStyle = l.color; g.lineWidth = 1.5; g.setLineDash(l.dash || []);
      g.beginPath(); g.moveTo(px, ys.top); g.lineTo(px, ys.bottom); g.stroke();
      if (l.text) { g.fillStyle = l.color; g.font = "11px " + css("--font"); g.fillText(l.text, px + 3, ys.top + 11); }
    }
    g.restore();
  },
};

function setupRunDetail() {
  const rep = S.report, sel = $("rdRun");
  const ids = rep.runs.map((r) => String(r.run_id)).filter((id) => rep.series && rep.series[id]);
  if (sel.options.length !== ids.length) {
    sel.innerHTML = rep.runs.filter((r) => ids.includes(String(r.run_id)))
      .map((r) => `<option value="${r.run_id}">Run ${r.run_id} · ${esc(r.label)}</option>`).join("");
    const firstAnom = rep.runs.find((r) => r.role === "anomaly" && ids.includes(String(r.run_id)));
    if (firstAnom) sel.value = String(firstAnom.run_id);
    sel.onchange = drawRunDetail;
  }
  drawRunDetail();
}

function drawRunDetail() {
  const rep = S.report, id = $("rdRun").value;
  const ser = rep.series && rep.series[id];
  const row = rep.runs.find((r) => String(r.run_id) === id);
  if (!ser || !row) return;
  const f = rep.meta.features;
  // shares only mean something once the water is at least one "meaningful change" away from normal
  const share = ser.z.map((zr) => { const q = zr.map((v) => v * v); const tot = q.reduce((a, b) => a + b, 0);
    return tot < 1 ? q.map(() => null) : q.map((v) => v / tot); });
  const sensorColors = SENSOR_COLORS;
  const lines = (row.markers || []).map((m) => ({ x: m.t_s, color: C.warn, text: m.text, dash: [] }))
    .concat((row.change_points_s || []).map((x) => ({ x, color: css("--muted"), dash: [4, 4] })));
  const bands = [];
  ser.drift.forEach((on, i) => {
    if (!on) return;
    const last = bands[bands.length - 1];
    const x1 = ser.t[Math.min(i + 1, ser.t.length - 1)];
    if (last && last.open) last.x1 = x1; else bands.push({ x0: ser.t[i], x1, open: true, color: alpha(C.warn, 0.22) });
    if (!ser.drift[i + 1]) bands[bands.length - 1].open = false;
  });
  mkChart("cContrib", {
    type: "line", plugins: [vlinesPlugin],
    data: { datasets: f.map((k, j) => ({
      label: SENSORS[k].name, data: ser.t.map((t, i) => ({ x: t, y: share[i][j] })), borderColor: sensorColors[k],
      backgroundColor: alpha(sensorColors[k], 0.6), fill: j === 0 ? "origin" : "-1", pointRadius: 0, borderWidth: 1, tension: .2, spanGaps: false })) },
    options: { parsing: true, plugins: { legend: { position: "bottom" }, vlines: { lines, bands } },
      scales: { x: { type: "linear", title: { display: true, text: "Time (s)" } }, y: { stacked: true, min: 0, max: 1, ticks: { callback: (v) => Math.round(v * 100) + "%" } } } },
  });
  const n = ser.pc.length;
  const thr = rep.meta.thresholds.mspc;
  const ell = rep.meta.pca2 ? ellipse([0, 0], rep.meta.pca2.normal_cov, thr) : [];
  mkChart("cTraj", {
    type: "scatter",
    data: { datasets: [
      { label: "Normal-water boundary", data: ell, showLine: true, borderColor: css("--text"), borderDash: [5, 4], borderWidth: 1.2, pointRadius: 0 },
      { label: `Run ${id}`, data: ser.pc.map((p) => ({ x: p[0], y: p[1] })), showLine: true, borderWidth: 1,
        borderColor: alpha(labelColor(row.label), 0.4),
        pointRadius: 3, pointBackgroundColor: ser.pc.map((_, i) => alpha(labelColor(row.label), 0.15 + 0.85 * i / Math.max(1, n - 1))), pointBorderWidth: 0 },
    ] },
    options: { plugins: { legend: { position: "bottom" } }, scales: { x: { title: { display: true, text: "PC1" } }, y: { title: { display: true, text: "PC2" } } } },
  });
  const m = row.models.mspc;
  $("rdSummary").innerHTML = [
    `Condition <b>${esc(row.label)}</b>, ${row.rows} rows, every ${row.dt} s`,
    row.t_dose != null ? `Additive at ${fmt(row.t_dose, 0)} s` : "",
    m.first_alarm_s != null ? `MSPC alarms after ${Math.round(m.first_alarm_s)} s` : (row.role === "anomaly" ? "MSPC does not alarm" : ""),
    row.drift_first_s != null ? `Drift warns after ${Math.round(row.drift_first_s)} s` : "",
    row.min_dose != null ? `Smallest dose detected ${fmt(row.min_dose, 1)}` : "",
    (row.change_points_s || []).length ? `Change points at ${row.change_points_s.map((x) => Math.round(x) + " s").join(", ")}` : "",
  ].filter(Boolean).join(" · ");
}

function drawDose() {
  const rep = S.report;
  const runs = rep.runs.filter((r) => r.t_dose != null && (r.markers || []).some((m) => typeof m.dose === "number"));
  if (!runs.length) {
    if (S.charts.cDose) { S.charts.cDose.destroy(); delete S.charts.cDose; }
    const c = $("cDose"), g = c.getContext("2d"); g.clearRect(0, 0, c.width, c.height);
    g.fillStyle = css("--muted"); g.font = "14px " + css("--font");
    g.fillText("No runs with recorded \"additive\" events and doses yet: record a gradual-addition run and press Mark event each time you add", 16, 36);
    return;
  }
  const doseAt = (markers, t) => markers.filter((m) => typeof m.dose === "number" && m.t_s <= t).reduce((a, m) => a + m.dose, 0);
  const ds = runs.map((r) => {
    const tl = rep.timelines.mspc.find((x) => x.run_id === r.run_id);
    return { label: `Run ${r.run_id} ${r.label}` + (r.min_dose != null ? ` · detected at ${fmt(r.min_dose, 1)}` : " · no alarm"),
      data: tl.t.map((t, i) => ({ x: doseAt(r.markers, t), y: Math.max(tl.ratio[i], 0.01) })),
      borderColor: labelColor(r.label), backgroundColor: labelColor(r.label), showLine: true, pointRadius: 2, borderWidth: 1.5 };
  });
  const maxX = Math.max(...ds.flatMap((d) => d.data.map((p) => p.x)), 1);
  ds.push({ label: "Alarm line", data: [{ x: 0, y: 1 }, { x: maxX, y: 1 }], borderColor: css("--text"), borderDash: [6, 5], showLine: true, pointRadius: 0, borderWidth: 1.2 });
  mkChart("cDose", { type: "scatter", data: { datasets: ds },
    options: { plugins: { legend: { position: "bottom" } },
      scales: { x: { title: { display: true, text: "Cumulative dose added (in the unit you recorded, e.g. mL)" } },
        y: { type: "logarithmic", min: 0.01, title: { display: true, text: "Score / alarm line (log)" } } } } });
}

function drawRoc() {
  const rep = S.report;
  const ds = Object.values(rep.models).filter((m) => m.roc).map((m) => ({
    label: `${m.name.split(" (")[0]} · AUC ${fmt(m.auc, 3)}`, data: m.roc.fpr.map((x, i) => ({ x, y: m.roc.tpr[i] })),
    borderColor: MODEL_COLORS[m.key], backgroundColor: MODEL_COLORS[m.key], showLine: true, pointRadius: 0, borderWidth: 2,
  }));
  if (!ds.length) {
    const c = $("cRoc"); if (S.charts.cRoc) { S.charts.cRoc.destroy(); delete S.charts.cRoc; }
    const g = c.getContext("2d"); g.clearRect(0, 0, c.width, c.height); g.fillStyle = css("--muted");
    g.font = "14px " + css("--font"); g.fillText("Record an anomaly run first to draw the ROC curve", 20, 40);
    return;
  }
  ds.push({ label: "Random", data: [{ x: 0, y: 0 }, { x: 1, y: 1 }], borderColor: css("--muted"), borderDash: [4, 4], showLine: true, pointRadius: 0, borderWidth: 1 });
  mkChart("cRoc", { type: "scatter", data: { datasets: ds },
    options: { plugins: { legend: { position: "bottom" } },
      scales: { x: { min: 0, max: 1, title: { display: true, text: "False positive rate" } }, y: { min: 0, max: 1, title: { display: true, text: "True positive rate" } } } } });
}

function setupScatter() {
  const f = S.report.meta.features;
  const opts = f.map((k) => `<option value="${k}">${SENSORS[k].name}</option>`).join("");
  if ($("scX").options.length !== f.length) {
    $("scX").innerHTML = opts; $("scY").innerHTML = opts;
    $("scX").value = "tds_ppm"; $("scY").value = f.includes("turb_v") ? "turb_v" : f[0];
    const zb = document.createElement("button");
    zb.className = "btn ghost sm"; zb.id = "scZoom"; zb.textContent = "Show all";
    zb.onclick = () => { S.scZoom = !S.scZoom; zb.textContent = S.scZoom ? "Show all" : "Zoom to normal water"; drawScatter(); };
    $("scY").after(zb);
  }
  drawScatter();
}

function ellipse(mu, cov, level, n = 90) {
  const [a, b, c] = [cov[0][0], cov[0][1], cov[1][1]];
  const l11 = Math.sqrt(a), l21 = b / l11, l22 = Math.sqrt(Math.max(c - l21 * l21, 1e-12)), k = Math.sqrt(level);
  const out = [];
  for (let i = 0; i <= n; i++) {
    const t = (i / n) * 2 * Math.PI, u = Math.cos(t), v = Math.sin(t);
    out.push({ x: mu[0] + k * l11 * u, y: mu[1] + k * (l21 * u + l22 * v) });
  }
  return out;
}

function drawScatter() {
  const rep = S.report, f = rep.meta.features;
  const ix = f.indexOf($("scX").value), iy = f.indexOf($("scY").value);
  const groups = {};
  for (const p of rep.points) (groups[p.label] = groups[p.label] || []).push({ x: p.x[ix], y: p.x[iy] });
  const ds = Object.entries(groups).sort((a) => (a[0] === "normal" ? -1 : 1)).map(([lab, pts]) => ({
    label: lab, data: pts, backgroundColor: alpha(labelColor(lab), lab === "normal" ? .45 : .7), pointRadius: 3, borderWidth: 0 }));
  const mu = rep.meta.mspc.mu, cov = rep.meta.mspc.cov;
  let lim = {};
  if (ix !== iy) {
    const el = ellipse([mu[ix], mu[iy]], [[cov[ix][ix], cov[ix][iy]], [cov[iy][ix], cov[iy][iy]]], rep.meta.thresholds.mspc);
    ds.push({ label: "Normal-water boundary (MSPC)", data: el, showLine: true, borderColor: css("--text"), borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, backgroundColor: "transparent" });
    if (S.scZoom) {
      const xs = el.map((p) => p.x), ys = el.map((p) => p.y);
      const pad = (lo, hi) => [lo - (hi - lo) * 1.2, hi + (hi - lo) * 1.2];
      const [x0, x1] = pad(Math.min(...xs), Math.max(...xs)), [y0, y1] = pad(Math.min(...ys), Math.max(...ys));
      lim = { x: { min: x0, max: x1 }, y: { min: y0, max: y1 } };
    }
  }
  const ax = (k, l) => ({ ...(l || {}), title: { display: true, text: `${SENSORS[k].name} ${SENSORS[k].unit ? "(" + SENSORS[k].unit + ")" : ""}` } });
  mkChart("cScatter", { type: "scatter", data: { datasets: ds },
    options: { plugins: { legend: { position: "bottom" } }, scales: { x: ax(f[ix], lim.x), y: ax(f[iy], lim.y) } } });
}

async function drawEmbedding(force) {
  if (!S.report) return;
  const m = S.emb.m;
  if (force || !S.emb.data || S.emb.data.method !== m) {
    $("embNote").innerHTML = `<span class="spin"></span>Computing ${m.toUpperCase()} (t-SNE/UMAP can take 5–30 s)…`;
    try { S.emb.data = await api(`/api/embedding?method=${m}`); }
    catch (e) { $("embNote").textContent = e.message; S.emb.data = null; return; }
  }
  const E = S.emb.data;
  $("embNote").textContent = `${E.note} · ${E.xy.length} points · ${E.seconds}s`;
  const groups = {};
  E.xy.forEach((p, i) => {
    const key = S.emb.c === "label" ? E.label[i] : (E.alarm[i] ? "Alarm" : "No alarm");
    (groups[key] = groups[key] || []).push({ x: p[0], y: p[1] });
  });
  const colorOf = (k) => (k === "Alarm" ? C.bad : k === "No alarm" ? C.ok : labelColor(k));
  mkChart("cEmbed", { type: "scatter",
    data: { datasets: Object.entries(groups).map(([k, pts]) => ({ label: k, data: pts, backgroundColor: alpha(colorOf(k), .65), pointRadius: 3.2, borderWidth: 0 })) },
    options: { plugins: { legend: { position: "bottom" }, tooltip: { enabled: false } },
      scales: { x: { title: { display: true, text: `${m.toUpperCase()} 1` }, ticks: { display: m === "pca" } },
        y: { title: { display: true, text: `${m.toUpperCase()} 2` }, ticks: { display: m === "pca" } } } } });
}

/* ------------------------------------------------------------------ tuning (WP4) */
const INSTANT = [
  ["margin", "Alarm line margin", 1.0, 3.0, 0.05, "higher = harder to alarm"],
  ["persist_s", "Must stay above the line for (s)", 1, 15, 1, "higher = ignores spikes better, but slower"],
  ["release", "Stop alarming below (fraction of the line)", 0.5, 0.95, 0.05, ""],
  ["ewma_lambda", "EWMA λ per second", 0.05, 1.0, 0.05, "lower = smoother, slower"],
  ["quantile", "Normal-water quantile", 0.99, 0.999, 0.001, ""],
  ["min_sigma", "MSPC: alarm at a distance of at least (× floor)", 1.5, 6, 0.1, ""],
  ["cusum_k", "Drift: ignore differences smaller than (× floor)", 0.25, 2, 0.05, ""],
  ["cusum_h_min", "Drift: minimum sensitivity h (z·s)", 2, 40, 1, "lower = earlier warnings"],
];
const PARAMS = { pca: [["var_target", "PCA: variance kept", 0.5, 0.999, 0.01]],
  iforest: [["n_estimators", "Number of trees", 50, 500, 10]], ocsvm: [["nu", "ν (outlier share)", 0.001, 0.2, 0.001]],
  lof: [["n_neighbors", "Number of neighbours", 5, 60, 1]], mspc: [] };

function tuneSettings() {
  const det = {}, floors = {}, mp = {};
  document.querySelectorAll("#tuneInstant input[type=range], #tuneRefit input[data-g=detector]").forEach((i) => (det[i.dataset.k] = +i.value));
  document.querySelectorAll("#tuneRefit input[data-g=floors]").forEach((i) => (floors[i.dataset.k] = +i.value));
  const key = $("tuneModel").value;
  document.querySelectorAll("#tuneRefit input[data-g=param]").forEach((i) => { (mp[key] = mp[key] || {})[i.dataset.k] = +i.value; });
  return { detector: det, floors, model_params: mp };
}

async function setupTuning() {
  const card = $("tuneCard");
  if (!S.report) { card.hidden = true; return; }
  if (isViewer()) {
    try { S.tuneMeta = await api("/api/tune/meta"); renderCompare(); } catch (e) { /* nothing to compare */ }
    return;
  }
  card.hidden = false;
  if (!S.cfg) S.cfg = await api("/api/config");
  try { S.tuneMeta = await api("/api/tune/meta"); } catch (e) { S.tuneMeta = { presets: {}, history: [] }; }
  const sel = $("tuneModel");
  if (sel.options.length !== Object.keys(S.report.models).length) {
    sel.innerHTML = Object.entries(S.report.model_names).filter(([k]) => k in S.report.models).map(([k, n]) => `<option value="${k}">${esc(n)}</option>`).join("");
    sel.onchange = () => { renderTuneInputs(); schedulePreview(); };
  }
  sel.value = (S.st && S.st.model.active) || S.report.meta.best;
  $("tunePresets").innerHTML = Object.entries(S.tuneMeta.presets).map(([k, p]) => `<span class="chip" data-p="${k}">${esc(p.name)}</span>`).join("");
  $("tunePresets").onclick = (e) => {
    const c = e.target.closest(".chip"); if (!c) return;
    const p = S.tuneMeta.presets[c.dataset.p];
    Object.entries(p.detector).forEach(([k, v]) => { const i = document.querySelector(`#tuneInstant input[data-k="${k}"]`); if (i) { i.value = v; i.dispatchEvent(new Event("input")); } });
    schedulePreview();
  };
  renderTuneInputs();
  renderTuneHistory();
  renderCompare();
  schedulePreview();
}

function renderTuneInputs() {
  const cfg = S.cfg, det = cfg.detector, key = $("tuneModel").value;
  $("tuneInstant").innerHTML = INSTANT.map(([k, label, lo, hi, step, hint]) => `<div class="slider">
      <label><span>${label}</span><span>${hint}</span></label>
      <input type="range" min="${lo}" max="${hi}" step="${step}" value="${det[k]}" data-k="${k}"><output id="o-${k}">${det[k]}</output></div>`).join("");
  document.querySelectorAll("#tuneInstant input[type=range]").forEach((i) => (i.oninput = () => { $("o-" + i.dataset.k).textContent = i.value; schedulePreview(); }));
  const feats = S.report.meta.features;
  const mp = (cfg.model_params || {})[key] || {};
  $("tuneRefit").innerHTML = feats.map((f) => `<div><label class="f">floor ${SENSORS[f].name} (${SENSORS[f].unit || "-"})</label>
      <input type="number" step="any" data-g="floors" data-k="${f}" value="${cfg.floors[f]}"></div>`).join("")
    + `<div><label class="f">Median window (s)</label><input type="number" step="any" data-g="detector" data-k="median_s" value="${det.median_s}"></div>
       <div><label class="f">Trim the start of each run (s)</label><input type="number" step="any" data-g="detector" data-k="warmup_s" value="${det.warmup_s}"></div>`
    + (PARAMS[key] || []).map(([k, label, lo, hi, step]) => `<div><label class="f">${label}</label>
      <input type="number" min="${lo}" max="${hi}" step="${step}" data-g="param" data-k="${k}" value="${mp[k] ?? ""}"></div>`).join("");
  document.querySelectorAll("#tuneRefit input").forEach((i) => (i.onchange = schedulePreview));
}

function schedulePreview() {
  clearTimeout(S.tuneTimer);
  S.tuneTimer = setTimeout(runPreview, 450);
}

async function runPreview() {
  const seq = (S.tuneSeq = (S.tuneSeq || 0) + 1);
  $("tuneStatus").innerHTML = '<span class="spin"></span>Computing…';
  try {
    const r = await api("/api/tune/preview", { model: $("tuneModel").value, settings: tuneSettings() });
    if (seq !== S.tuneSeq) return;            // a newer preview is on its way
    $("tuneStatus").textContent = "";
    renderPreview(r);
  } catch (e) { if (seq === S.tuneSeq) $("tuneStatus").textContent = e.message; }
}

function renderPreview(r) {
  const a = r.current, b = r.candidate;
  const rows = [
    ["False alarms on normal water", "false_alarm", pct, -1], ["Abnormal water detected", "detection", pct, 1],
    ["Balanced accuracy", "balanced_acc", pct, 1],
    ["Time to alarm", "delay_s", (v) => (v == null ? "–" : Math.round(v) + " s"), -1],
    ["Drift false warnings/hour", "drift_false_per_hour", (v) => (v == null ? "–" : fmt(v, 1)), -1],
    ["Drift early warning", "drift_lead_s", (v) => (v == null ? "–" : Math.round(v) + " s"), 1],
  ];
  $("tuneCompare").innerHTML = rows.map(([label, k, f, better]) => {
    const d = a[k] != null && b[k] != null ? b[k] - a[k] : null;
    const cls = d == null || Math.abs(d) < 1e-9 ? "" : (d * better > 0 ? "delta-good" : "delta-bad");
    return `<tr><td>${label}</td><td class="num">${f(a[k])}</td><td class="num">${f(b[k])} ${cls ? `<span class="${cls}">${d > 0 ? "▲" : "▼"}</span>` : ""}</td></tr>`;
  }).join("") + (a.detection == null ? `<tr><td colspan="3" class="hint">No anomaly runs yet, so only false alarms are shown</td></tr>` : "");
  const lim = (L, f) => { const v = L && L[f]; if (!v) return "–";
    const u = SENSORS[f].unit, dp = Math.max(SENSORS[f].dp, 1);
    return `${v[0] == null ? "No alarm" : "−" + fmt(v[0], dp)} / ${v[1] == null ? "No alarm" : "+" + fmt(v[1], dp)} ${u}`; };
  $("tuneLimits").innerHTML = r.features.map((f) => `<tr><td>${SENSORS[f].name}</td><td class="num">${lim(a.limits, f)}</td><td class="num">${lim(b.limits, f)}</td></tr>`).join("");
  const m = +document.querySelector('#tuneInstant input[data-k="margin"]').value;
  mkChart("cSweep", { type: "line", plugins: [vlinesPlugin],
    data: { datasets: [
      { label: "False alarms (%)", data: r.sweep.map((s) => ({ x: s.margin, y: (s.false_alarm ?? 0) * 100 })), borderColor: C.bad, pointRadius: 2, tension: .2 },
      { label: "Detected (%)", data: r.sweep.map((s) => ({ x: s.margin, y: s.detection == null ? null : s.detection * 100 })), borderColor: C.ok, pointRadius: 2, tension: .2 },
    ] },
    options: { plugins: { legend: { position: "bottom" }, vlines: { lines: [{ x: m, color: C.warn, text: "Trial" }] } },
      scales: { x: { type: "linear", title: { display: true, text: "margin" } }, y: { min: 0, max: 100 } } } });
}

function renderTuneHistory() {
  const h = (S.tuneMeta && S.tuneMeta.history) || [];
  $("tuneHistory").innerHTML = h.length ? h.map((x, i) => [x, i]).reverse().map(([x, i]) => `<tr><td>${esc(x.created)}</td>
    <td>${esc((S.report.model_names || {})[x.best] || x.best)}</td>
    <td class="num">${pct(x.metrics.mspc && x.metrics.mspc.false_alarm)}</td><td class="num">${pct(x.metrics.mspc && x.metrics.mspc.detection)}</td>
    <td class="hint" style="margin:0">margin ${x.detector.margin} · persist ${x.detector.persist_s ?? "-"} s · floors ${Object.values(x.floors).join("/")}</td>
    <td><button class="btn ghost sm" data-restore="${i}">Restore</button></td></tr>`).join("") : `<tr><td colspan="6" class="hint">No history yet</td></tr>`;
  $("tuneHistory").querySelectorAll("[data-restore]").forEach((b) => (b.onclick = async () => {
    $("tuneStatus").innerHTML = '<span class="spin"></span>Restoring and retraining…';
    try { await api("/api/tune/restore", { index: +b.dataset.restore }); S.cfg = await api("/api/config"); toast("Restored"); await loadReport(); }
    catch (e) { $("tuneStatus").textContent = e.message; }
  }));
}

/* ------------------------------------------------------------------ settings */
const ADV = [
  ["detector", "warmup_s", "Trim the start of normal runs (s)"], ["detector", "median_s", "Median window (s)"],
  ["detector", "ewma_lambda", "EWMA λ per second (0–1, lower = smoother)"], ["detector", "persist_s", "Must stay above the line for (s)"],
  ["detector", "release", "Stop-alarm level (fraction of the line)"], ["detector", "quantile", "Normal-water quantile"],
  ["detector", "margin", "Alarm line margin"], ["detector", "min_sigma", "MSPC: minimum (× floor)"],
  ["floors", "temp_c", "Temperature floor (°C)"], ["floors", "tds_ppm", "floor TDS (ppm)"], ["floors", "turb_v", "Turbidity floor (V)"],
  ["floors", "ph", "floor pH"],
  ["", "session_gap_min", "Gap in minutes that starts a new water session"], ["", "link_timeout_s", "Alert when no data for (s)"],
];
async function loadConfig() {
  try {
    const [cfg, ports] = await Promise.all([api("/api/config"), api("/api/ports")]);
    S.cfg = cfg;
    const opts = [["auto", "Auto"], ["SIM", "SIM (simulated)"]].concat(ports.ports.map((p) => [p.device, `${p.device} · ${p.desc}${p.chip ? " ✓ESP32" : ""}`]));
    if (!opts.some((o) => o[0] === cfg.port)) opts.push([cfg.port, cfg.port]);
    $("setPort").innerHTML = opts.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
    $("setPort").value = cfg.port;
    $("setPh").checked = cfg.use_ph; $("setDiv").value = cfg.ph_divider;
    const a = cfg.alerts;
    $("alSound").checked = a.sound; $("alNtfy").value = a.ntfy_topic; $("alTgTok").value = a.telegram_token; $("alTgChat").value = a.telegram_chat_id;
    $("alLineTok").value = a.line_token; $("alLineUser").value = a.line_user_id; $("alRecovery").checked = a.notify_recovery; $("alFault").checked = a.notify_fault; $("alCause").checked = !!a.notify_cause;
    $("rmOn").checked = cfg.remote.enabled; $("rmPin").value = cfg.remote.pin; $("rmToken").value = cfg.ingest_token;
    $("advGrid").innerHTML = ADV.map(([g, k, t]) => `<div><label class="f">${t}</label><input type="number" step="any" data-g="${g}" data-k="${k}" value="${g ? cfg[g][k] : cfg[k]}"></div>`).join("");
    $("autoSnap").checked = cfg.auto_snapshot !== false;
    showCalib(cfg.calib);
    notifState();
  } catch (e) { toast(e.message); }
}
function showCalib(c) {
  $("calTdsInfo").textContent = `offset ${fmt(c.tds_offset_v, 3)} V · K ${fmt(c.tds_k, 3)}`;
  $("ph1Info").textContent = `Saved: pH ${c.ph_points[0][0]} = ${fmt(c.ph_points[0][1], 3)} V`;
  $("ph2Info").textContent = `Saved: pH ${c.ph_points[1][0]} = ${fmt(c.ph_points[1][1], 3)} V`;
}
async function saveConfig() {
  const pin = $("rmPin").value;
  if ($("rmOn").checked && !pin.startsWith("••••") && pin.length < 6) { toast("The PIN must be at least 6 characters"); return; }
  const patch = {
    port: $("setPort").value, use_ph: $("setPh").checked, ph_divider: +$("setDiv").value,
    alerts: { sound: $("alSound").checked, ntfy_topic: $("alNtfy").value.trim(), telegram_token: $("alTgTok").value.trim(),
      telegram_chat_id: $("alTgChat").value.trim(), line_token: $("alLineTok").value.trim(), line_user_id: $("alLineUser").value.trim(),
      notify_recovery: $("alRecovery").checked, notify_fault: $("alFault").checked, notify_cause: $("alCause").checked },
    remote: { enabled: $("rmOn").checked, pin }, ingest_token: $("rmToken").value.trim(), detector: {}, floors: {},
  };
  document.querySelectorAll("#advGrid input").forEach((i) => {
    if (i.dataset.g) patch[i.dataset.g][i.dataset.k] = +i.value; else patch[i.dataset.k] = +i.value;
  });
  patch.auto_snapshot = $("autoSnap").checked;
  const remoteChanged = S.cfg && S.cfg.remote.enabled !== patch.remote.enabled;
  try {
    S.cfg = await api("/api/config", patch);
    $("saveMsg").textContent = "Saved ✓" + (remoteChanged ? " · restart the app for access from other devices to take effect" : "");
    toast("Settings saved");
  } catch (e) { toast(e.message); }
}
async function calibrate(action, value) {
  try { const r = await api("/api/calibrate", { action, value }); showCalib(r.calib); toast("Calibrated ✓"); }
  catch (e) { toast(e.message, 4000); }
}

/* ------------------------------------------------------------------ wiring */
function wire() {
  document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.v)));
  document.addEventListener("click", unlockAudio, { once: true });
  $("btnSound").onclick = askNotify; $("btnNotifPerm").onclick = askNotify;
  $("btnSaveLast").onclick = async () => {
    const min = prompt("Save how many minutes of live data as a new run?", "5");
    if (min === null) return;
    const label = prompt("Condition (label), e.g. salt_lvl1 (leave empty to label later)", "") || "unlabeled";
    try { const r = await api("/api/snapshot", { minutes: +min, label: label.trim() }); toast(`Saved as ${r.file}`, 4000); }
    catch (e) { toast(e.message, 4000); }
  };
  $("monitorToggle").onchange = async (e) => {
    try { await api("/api/monitor", { on: e.target.checked }); unlockAudio(); }
    catch (err) { toast(err.message); e.target.checked = !e.target.checked; }
  };
  const SIMS = [["normal", "Normal water"], ["salt", "Salt"], ["baking_soda", "Baking soda"], ["starch", "Starch"],
    ["vinegar", "Vinegar"], ["hot_water", "Hot water"], ["probe_out", "Probe lifted"],
    ["ramp_salt", "Slow salt ramp"], ["ramp_starch", "Slow starch ramp"], ["pulse", "Single salt drop"]];
  $("simChips").innerHTML = SIMS.map(([k, t]) => `<span class="chip" data-s="${k}">${t}</span>`).join("");
  $("simChips").onclick = async (e) => {
    const s = e.target.dataset.s; if (!s) return;
    try { await api("/api/sim", { scenario: s }); } catch (err) { toast(err.message); }
  };

  $("labelChips").innerHTML = LABELS.map((l) => `<span class="chip" data-l="${l}">${l}</span>`).join("");
  $("labelChips").onclick = (e) => {
    const l = e.target.dataset.l; if (!l) return;
    $("recLabel").value = l; $("recMin").value = l === "normal" ? 5 : 3;
  };
  $("recRun").oninput = () => (S.runDirty = true);
  S.markKind = "note";
  $("markChips").onclick = (e) => {
    const c = e.target.closest(".chip"); if (!c) return;
    document.querySelectorAll("#markChips .chip").forEach((x) => x.classList.toggle("on", x === c));
    S.markKind = c.dataset.k; if (!$("markText").value) $("markText").value = c.dataset.t;
    if (c.dataset.k === "dose") $("markDose").focus();
  };
  $("btnMark").onclick = async () => {
    try {
      const dose = $("markDose").value;
      await api("/api/record/mark", { text: $("markText").value || "Events", kind: S.markKind, dose: dose === "" ? null : +dose });
      $("markText").value = ""; $("markDose").value = ""; S.markKind = "note";
      document.querySelectorAll("#markChips .chip").forEach((x) => x.classList.remove("on"));
      toast("Event marked");
    } catch (e) { toast(e.message, 4000); }
  };
  $("btnRec").onclick = async () => {
    try {
      const r = await api("/api/record/start", { run: +$("recRun").value, label: $("recLabel").value.trim(), minutes: +$("recMin").value,
        note: $("recNote").value.trim(), new_water: $("recNew").checked });
      S.recWas = true; $("recNew").checked = false;
      $("recWarn").innerHTML = (r.warnings || []).map((w) => `<div class="notice warn">${esc(w)}</div>`).join("");
      toast("Start recording");
    } catch (e) { toast(e.message, 4000); }
  };
  $("btnRecStop").onclick = () => api("/api/record/stop", {}).catch((e) => toast(e.message));

  wireExplorer();
  wireShare();
  $("liveAlarmPill").onclick = () => setLiveMode("live");
}

function wire2() {
  $("btnSuggest").onclick = async () => {
    $("suggestMsg").innerHTML = '<span class="spin"></span>';
    try {
      const r = await api("/api/tune/suggest_floors");
      Object.entries(r.floors).forEach(([f, v]) => { const i = document.querySelector(`#tuneRefit input[data-g=floors][data-k="${f}"]`); if (i) i.value = v; });
      $("suggestMsg").textContent = `From ${r.n_sessions} normal-water sessions (not saved yet: press Save if you agree)`;
      schedulePreview();
    } catch (e) { $("suggestMsg").textContent = e.message; }
  };
  $("btnTuneReset").onclick = async () => {
    const meta = S.tuneMeta || {};
    const d = (meta.presets && meta.presets.balanced && meta.presets.balanced.detector) || {};
    Object.entries(d).forEach(([k, v]) => { const i = document.querySelector(`#tuneInstant input[data-k="${k}"]`); if (i) { i.value = v; $("o-" + k).textContent = v; } });
    schedulePreview();
  };
  $("btnTuneApply").onclick = async () => {
    $("btnTuneApply").disabled = true;
    $("tuneStatus").innerHTML = '<span class="spin"></span>Saving and retraining…';
    try {
      const r = await api("/api/tune/apply", { settings: tuneSettings(), retrain: true });
      S.cfg = await api("/api/config");
      toast(r.ok ? "Saved and retrained" : r.error, 4000);
      await loadReport();
    } catch (e) { $("tuneStatus").textContent = e.message; }
    $("btnTuneApply").disabled = false;
  };
  $("tlModel").onchange = drawTimeline;
  $("scX").onchange = drawScatter; $("scY").onchange = drawScatter;
  $("embSeg").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    document.querySelectorAll("#embSeg button").forEach((x) => x.classList.toggle("on", x === b));
    S.emb.m = b.dataset.m; drawEmbedding();
  };
  $("embColor").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    document.querySelectorAll("#embColor button").forEach((x) => x.classList.toggle("on", x === b));
    S.emb.c = b.dataset.c; drawEmbedding();
  };

  $("btnPorts").onclick = loadConfig;
  $("btnSave").onclick = saveConfig;
  $("btnTopic").onclick = () => {
    const r = Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => "abcdefghjkmnpqrstuvwxyz23456789"[b % 31]).join("");
    $("alNtfy").value = "water-sentinel-" + r;
  };
  $("btnTestAlert").onclick = async () => {
    $("testAlertMsg").textContent = "Sending… (settings are saved before the test)";
    try {
      await saveConfig();
      const r = await api("/api/alerts/test", {});
      $("testAlertMsg").textContent = r.results.map((x) => `${x.channel}: ${x.ok ? "Sent ✓" : "Failed " + x.error}`).join(" · ");
    } catch (e) { $("testAlertMsg").textContent = e.message; }
  };
  $("calAir").onclick = () => calibrate("tds_air");
  $("calStdBtn").onclick = () => calibrate("tds_std", +$("calStd").value);
  document.querySelectorAll("[data-slot]").forEach((b) => (b.onclick = () =>
    calibrate("ph_point", { slot: +b.dataset.slot, ph: +$(b.dataset.slot === "0" ? "ph1" : "ph2").value })));
  $("btnTheme").onclick = () => {
    const dark = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim().startsWith("#0");
    try { localStorage.setItem("ws-theme", dark ? "light" : "dark"); } catch (e) { /* ignore */ }
    location.reload();
  };
}

async function init() {
  applyTheme(); chartDefaults(); wire(); wire2(); initScoreChart();
  try { applyRole(await api("/api/state?since=" + Date.now() / 1000)); } catch (e) { /* first poll will report */ }
  try {
    S.cfg = await api("/api/config");
  } catch (e) { /* first poll will report */ }
  const tab = location.hash.slice(1);
  if (["live", "record", "model", "viz", "settings"].includes(tab)) showTab(tab);
  poll();
}
init();
