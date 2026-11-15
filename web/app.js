"use strict";
/* Water Sentinel web UI. Plain JS + Chart.js (vendored, works offline). Talks to the FastAPI backend. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const pct = (v, d = 1) => (v == null ? "–" : (v * 100).toFixed(d) + "%");
const fmt = (v, d = 2) => (v == null || !isFinite(v) ? "–" : Number(v).toFixed(d));

const SENSORS = {
  temp_c: { name: "อุณหภูมิ", unit: "°C", dp: 2, icon: "🌡️" },
  tds_ppm: { name: "TDS", unit: "ppm", dp: 0, icon: "🧂" },
  turb_v: { name: "ความขุ่น", unit: "V", dp: 3, icon: "🌫️" },
  ph: { name: "pH", unit: "", dp: 2, icon: "⚗️" },
};
const LABELS = ["normal", "salt_lvl1", "salt_lvl2", "baking_soda_lvl1", "starch_lvl1", "hot_water", "vinegar_lvl1"];
const PALETTE = ["#ef4444", "#f59e0b", "#a855f7", "#3b82f6", "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#06b6d4", "#84cc16"];
const MODEL_COLORS = { mspc: "#22c55e", pca: "#3b82f6", iforest: "#f59e0b", ocsvm: "#a855f7", lof: "#ec4899" };

const S = { st: null, lastT: 0, hist: [], charts: {}, report: null, cfg: null, lastEvent: null, audio: null,
            recWas: false, runDirty: false, tileKeys: [], scZoom: true, emb: { m: "pca", c: "label", data: null } };

const LABEL_COLORS = {};
function registerLabels(labels) {   // distinct colour per label, stable order (alphabetical)
  [...new Set(labels)].filter((l) => l !== "normal").sort().forEach((l) => {
    if (!(l in LABEL_COLORS)) LABEL_COLORS[l] = PALETTE[Object.keys(LABEL_COLORS).length % PALETTE.length];
  });
}
function labelColor(label) {
  if (label === "normal") return "#22c55e";
  if (!(label in LABEL_COLORS)) registerLabels([label]);
  return LABEL_COLORS[label];
}
function alpha(hex, a) {
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
  Chart.defaults.font.family = css("--font");
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
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("on", s.id === "v-" + v));
  if (v === "record") loadRuns();
  if (v === "model" || v === "viz") loadReport();
  if (v === "settings") loadConfig();
  try { history.replaceState(null, "", "#" + v); } catch (e) { /* ignore */ }
}

/* ------------------------------------------------------------------ live */
function buildTiles(usePh) {
  const keys = ["temp_c", "tds_ppm", "turb_v"].concat(usePh ? ["ph"] : []);
  if (keys.join() === S.tileKeys.join()) return;
  S.tileKeys = keys;
  $("tiles").innerHTML = keys.map((k) => `
    <div class="tile"><div class="k"><span>${SENSORS[k].icon} ${SENSORS[k].name}</span><span class="z" id="z-${k}">z –</span></div>
    <div class="v"><span id="v-${k}">–</span><span class="u">${SENSORS[k].unit}</span></div>
    <canvas id="sp-${k}"></canvas></div>`).join("");
  keys.forEach((k) => mkChart("sp-" + k, {
    type: "line",
    data: { labels: [], datasets: [{ data: [], borderColor: css("--accent"), borderWidth: 1.6, pointRadius: 0, tension: .3,
      fill: true, backgroundColor: alpha("#38bdf8", 0.12) }] },
    options: { plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false } } },
  }));
}

function initScoreChart() {
  mkChart("cScore", {
    type: "line",
    data: { labels: [], datasets: [
      { label: "คะแนน", data: [], borderColor: css("--accent"), borderWidth: 2, pointRadius: 0, tension: .25, fill: true,
        backgroundColor: alpha("#38bdf8", 0.12),
        segment: { borderColor: (c) => (c.p1.parsed.y > 1 ? css("--bad") : undefined) } },
      { label: "เส้นเตือน", data: [], borderColor: css("--bad"), borderDash: [6, 5], borderWidth: 1.5, pointRadius: 0, fill: false },
    ] },
    options: { interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { maxTicksLimit: window.innerWidth < 600 ? 3 : 6, maxRotation: 0 }, grid: { display: false } },
        y: { suggestedMin: 0, suggestedMax: 1.5, title: { display: true, text: "คะแนน / เส้นเตือน" } } } },
  });
}

const HERO = {
  normal: ["✅", "น้ำปกติ"], anomaly: ["⚠️", "ตรวจพบน้ำผิดปกติ!"], fault: ["🔧", "เซนเซอร์มีปัญหา"],
  starting: ["⏳", "กำลังเริ่มวัด..."], off: ["💤", "ยังไม่ได้เปิดเฝ้าระวัง"], no_model: ["📋", "ยังไม่มีโมเดล"],
  no_data: ["🔌", "ไม่มีข้อมูลจากเซนเซอร์"],
};

function renderLive(st) {
  const d = st.detector || { status: "off" };
  const stale = st.link.age == null || st.link.age > 6;
  let s = stale ? "no_data" : d.status;
  if (!st.monitor_on && s !== "no_data") s = st.model.exists ? "off" : "no_model";
  const [icon, title] = HERO[s] || HERO.off;
  let sub = "";
  if (s === "normal") sub = `คะแนน ${fmt(d.ratio)} (เตือนเมื่อเกิน 1.00)`;
  else if (s === "anomaly") sub = (d.cause || []).map((c) => c.text).join(" · ") || "ค่าหลายเซนเซอร์เปลี่ยนพร้อมกัน";
  else if (s === "fault") sub = d.fault_text;
  else if (s === "starting") sub = `รอค่าอีก ${d.left ?? "?"} ค่าเพื่อกรองสัญญาณรบกวน`;
  else if (s === "off") sub = "เปิดสวิตช์ด้านขวาเพื่อเริ่มตรวจจับความผิดปกติแบบเรียลไทม์";
  else if (s === "no_model") sub = "เก็บข้อมูลน้ำปกติอย่างน้อย 5 รอบ แล้วกดเทรนที่แท็บ \"เทรน & เปรียบเทียบโมเดล\"";
  else if (s === "no_data") sub = st.link.msg;
  $("hero").className = "card hero span-12 s-" + s;
  $("heroIcon").textContent = icon; $("heroTitle").textContent = title; $("heroSub").textContent = sub;
  const tg = $("monitorToggle");
  if (document.activeElement !== tg) tg.checked = !!st.monitor_on;
  tg.disabled = !st.model.exists;
  $("heroModel").textContent = st.model.exists ? `โมเดล: ${st.model.active_name}` : "";
  document.title = s === "anomaly" ? "⚠️ ผิดปกติ · Water Sentinel" : "Water Sentinel";

  buildTiles(st.use_ph);
  const L = st.latest || {};
  for (const k of S.tileKeys) {
    $("v-" + k).textContent = fmt(L[k], SENSORS[k].dp);
    const z = d.z ? d.z[k] : null;
    const zEl = $("z-" + k);
    zEl.textContent = z == null ? "z –" : `z ${z > 0 ? "+" : ""}${fmt(z, 1)}`;
    zEl.classList.toggle("hot", z != null && Math.abs(z) >= 3);
    const ch = S.charts["sp-" + k];
    const tail = S.hist.slice(-120);
    ch.data.labels = tail.map((h) => h.t);
    ch.data.datasets[0].data = tail.map((h) => h[k]);
    ch.update("none");
  }

  const r = d.ratio;
  $("gaugeBar").style.width = r == null ? "0%" : Math.max(0, Math.min(100, r * 50)) + "%";
  $("ratioText").textContent = r == null ? "–" : `ตอนนี้ ${fmt(r)}`;
  const sc = S.charts.cScore;
  const tail = S.hist.slice(-600);
  sc.data.labels = tail.map((h) => new Date(h.t * 1000).toLocaleTimeString("th-TH", { hour12: false }));
  sc.data.datasets[0].data = tail.map((h) => (h.ratio == null ? null : Math.min(h.ratio, 5)));
  sc.data.datasets[1].data = tail.map(() => 1);
  sc.update("none");

  renderEvents(st.events);
  $("simCard").hidden = !st.sim;
  if (st.sim) document.querySelectorAll("#simChips .chip").forEach((c) => c.classList.toggle("on", c.dataset.s === st.sim_scenario));
}

function renderEvents(evs) {
  const top = evs.length ? evs[0].id : 0;
  if (S.lastEvent === null) S.lastEvent = top;          // first load: don't replay old alarms
  else if (top > S.lastEvent) {
    evs.filter((e) => e.id > S.lastEvent).reverse().forEach(onNewEvent);
    S.lastEvent = top;
  }
  const kindIcon = { anomaly: "⚠️", recovered: "✅", fault: "🔧", info: "ℹ️" };
  $("events").innerHTML = evs.length ? evs.map((e) => `<li><time>${esc(e.time)}</time><div>
      <div class="k-${e.kind}">${kindIcon[e.kind] || ""} ${esc(e.msg)}</div>
      ${e.sent && e.sent.length ? `<div class="sent">${e.sent.map((x) => `${x.channel} ${x.ok ? "✓" : "✗ " + esc(x.error || "")}`).join(" · ")}</div>` : ""}
    </div></li>`).join("") : `<li class="hint">ยังไม่มีเหตุการณ์</li>`;
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
    notify("⚠️ น้ำผิดปกติ", e.msg);
  } else if (e.kind === "recovered") {
    beep([523, 784], 0.15);
    notify("✅ กลับปกติ", e.msg);
  } else if (e.kind === "fault") {
    beep([300], 0.25, 0, 0.15);
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
  toast("เปิดเสียงเตือนแล้ว");
}
function notifState() {
  const el = $("notifState");
  if (!("Notification" in window)) { el.textContent = "เบราว์เซอร์นี้ไม่รองรับ"; return; }
  el.textContent = { granted: "อนุญาตแล้ว ✓", denied: "ถูกปฏิเสธ: เปิดในการตั้งค่าเบราว์เซอร์", default: "ยังไม่ได้อนุญาต" }[Notification.permission];
}

/* ------------------------------------------------------------------ polling */
async function poll() {
  try {
    const st = await api(`/api/state?since=${S.lastT}`);
    S.st = st;
    for (const h of st.history) S.hist.push(h);
    if (S.hist.length > 900) S.hist = S.hist.slice(-900);
    if (st.history.length) S.lastT = st.history[st.history.length - 1].t;
    renderHeader(st); renderLive(st); renderRec(st);
  } catch (e) {
    $("linkText").textContent = "ติดต่อโปรแกรมไม่ได้ (ปิดอยู่?)"; $("linkDot").className = "dot bad";
  }
  setTimeout(poll, 1000);
}
function renderHeader(st) {
  const stale = st.link.age == null || st.link.age > 6;
  $("linkDot").className = "dot " + (st.link.ok && !stale ? "ok" : "bad");
  $("linkText").textContent = st.link.msg;
  const m = st.model;
  $("modelPill").textContent = !m.exists ? "ไม่มีโมเดล" : (m.mismatch ? "⚠ โมเดลไม่ตรงกับเซนเซอร์ที่เลือก: เทรนใหม่" : `โมเดล ${m.features.length} เซนเซอร์ · ${m.created}`);
}

/* ------------------------------------------------------------------ record */
async function loadRuns() {
  try {
    const r = await api("/api/runs");
    if (!S.runDirty && !(S.st && S.st.rec)) $("recRun").value = r.next_run;
    const runs = r.runs;
    $("runsBody").innerHTML = runs.length ? runs.slice().reverse().map((x) => `<tr><td>${x.run ?? "-"}</td>
      <td><span class="tag" style="background:${alpha(labelColor(x.label), .18)};color:${labelColor(x.label)}">${esc(x.label)}</span></td>
      <td>${esc(x.time)}</td><td class="num">${x.rows}</td><td>${x.has_ph ? "✓" : ""}</td><td class="hint" style="margin:0">${esc(x.file)}</td></tr>`).join("")
      : `<tr><td colspan="6" class="hint">ยังไม่มีข้อมูล</td></tr>`;
    const nN = runs.filter((x) => x.label === "normal" && x.rows > 0).length;
    const nA = runs.filter((x) => x.label !== "normal" && x.rows > 0).length;
    $("runsSummary").textContent = `normal ${nN} รอบ · anomaly ${nA} รอบ`;
    $("recPlan").innerHTML = `<div class="notice ${nN >= 5 ? "" : "warn"}">${nN >= 5 ? "✓" : "○"} normal ${nN}/5 รอบ</div>
      <div class="notice ${nA >= 2 ? "" : "warn"}">${nA >= 2 ? "✓" : "○"} anomaly ${nA} รอบ (แนะนำอย่างน้อย 2 ชนิด)</div>`;
  } catch (e) { toast(e.message); }
}
function renderRec(st) {
  const r = st.rec;
  $("btnRec").disabled = !!r; $("btnRecStop").disabled = !r;
  if (r) {
    $("recBar").style.width = Math.min(100, (r.elapsed / (r.minutes * 60)) * 100) + "%";
    $("recRows").textContent = r.rows;
    $("recTime").innerHTML = `${Math.round(r.elapsed)}<span class="u">/ ${Math.round(r.minutes * 60)} s</span>`;
    $("recMsg").textContent = `กำลังเก็บรอบ ${r.run} (${r.label})`;
  } else if (S.recWas) {
    $("recBar").style.width = "0%";
    $("recMsg").textContent = st.rec_last ? `เสร็จ: รอบ ${st.rec_last.run} ได้ ${st.rec_last.rows} แถว` : "";
    S.runDirty = false;
    loadRuns();
    toast("เก็บข้อมูลรอบนี้เสร็จแล้ว");
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
  renderReport(); renderViz();
}

function renderReport() {
  const box = $("modelResults");
  const rep = S.report;
  if (!rep) { box.innerHTML = `<div class="card"><p class="hint">ยังไม่มีผลการเทรน</p></div>`; return; }
  const meta = rep.meta, models = Object.values(rep.models);
  const active = S.st && S.st.model.active;
  const hasAnom = meta.anomaly_runs.length > 0;
  const best = rep.models[meta.best];
  const cls = (v, goodHigh, okv) => (v == null ? "" : (goodHigh ? (v >= okv ? "good" : "poor") : (v <= okv ? "good" : "poor")));
  box.innerHTML = `
  <div class="grid">
    <div class="card span-12">
      <div class="row"><h2>🏆 แนะนำ: ${esc(best.name)}</h2><span class="spacer"></span><span class="hint" style="margin:0">เทรนเมื่อ ${esc(meta.created)}</span></div>
      <p class="hint">${hasAnom ? "เลือกจาก balanced accuracy สูงสุด โดย false alarm ไม่เกิน 2%" : "ยังไม่มีรอบ anomaly จึงเทียบได้เฉพาะ false alarm (เก็บรอบ anomaly เพื่อเทียบครบ)"}</p>
      <div class="kpis">
        <div class="tile"><div class="k">เตือนผิดบนน้ำปกติ</div><div class="v ${cls(best.false_alarm, false, .02)}">${pct(best.false_alarm)}</div></div>
        <div class="tile"><div class="k">จับน้ำผิดปกติได้</div><div class="v ${cls(best.detection, true, .8)}">${pct(best.detection)}</div></div>
        <div class="tile"><div class="k">AUC</div><div class="v">${fmt(best.auc, 3)}</div></div>
        <div class="tile"><div class="k">เวลาจนเตือน (มัธยฐาน)</div><div class="v">${best.delay_s == null ? "–" : Math.round(best.delay_s)}<span class="u">s</span></div></div>
      </div>
      <p class="hint" style="margin-top:12px">ข้อมูล: normal ${meta.normal_runs.length} รอบ, anomaly ${meta.anomaly_runs.length} รอบ, ${meta.n_train_rows} แถวสำหรับเทรน ·
        เซนเซอร์: ${meta.features.map((f) => SENSORS[f].name).join(", ")} · วิธีประเมิน: ${esc(meta.protocol)}</p>
      ${meta.normal_runs.length < 5 ? `<div class="notice warn">มี normal แค่ ${meta.normal_runs.length} รอบ: ผลประเมินยังแกว่งได้มาก ควรเก็บให้ถึง 5 รอบขึ้นไป</div>` : ""}
      ${rep.skipped.length ? `<details><summary>ไฟล์ที่ไม่ได้ใช้ (${rep.skipped.length})</summary><ul class="hint">${rep.skipped.map((s) => `<li>${esc(s.file)}: ${esc(s.reason)}</li>`).join("")}</ul></details>` : ""}
    </div>
    <div class="card span-12">
      <h2>เปรียบเทียบโมเดล</h2>
      <p class="hint">ประเมินแบบ leave-one-run-out: ทุกตัวเลขมาจากรอบที่โมเดลไม่เคยเห็นตอนเทรน · กด "ใช้" เพื่อเลือกโมเดลที่ใช้เฝ้าระวังจริง</p>
      <div class="table-wrap"><table>
        <thead><tr><th>โมเดล</th><th class="num">เตือนผิด ↓</th><th class="num">จับได้ ↑</th><th class="num">Balanced acc ↑</th><th class="num">AUC ↑</th>
          <th class="num">เวลาจนเตือน</th><th class="num">รอบ anomaly ที่จับได้</th><th class="num">ความเร็ว/ค่า</th><th></th></tr></thead>
        <tbody>${models.map((m) => `<tr class="${m.key === meta.best ? "best" : ""}">
          <td><span class="tag" style="background:${alpha(MODEL_COLORS[m.key], .18)};color:${MODEL_COLORS[m.key]}">●</span> ${esc(m.name)}${m.key === meta.best ? " 🏆" : ""}</td>
          <td class="num ${cls(m.false_alarm, false, .02)}">${pct(m.false_alarm)}</td>
          <td class="num ${cls(m.detection, true, .8)}">${pct(m.detection)}</td>
          <td class="num">${pct(m.balanced_acc)}</td><td class="num">${fmt(m.auc, 3)}</td>
          <td class="num">${m.delay_s == null ? "–" : Math.round(m.delay_s) + " s"}</td>
          <td class="num">${hasAnom ? `${m.runs_caught}/${meta.anomaly_runs.length}` : "–"}</td>
          <td class="num">${m.score_us < 1000 ? Math.round(m.score_us) + " µs" : (m.score_us / 1000).toFixed(1) + " ms"}</td>
          <td>${m.key === active ? `<span class="tag" style="background:${alpha("#22c55e", .2)};color:var(--ok)">ใช้งานอยู่</span>`
            : `<button class="btn ghost sm" data-use="${m.key}">ใช้</button>`}</td></tr>`).join("")}</tbody></table></div>
      <div class="chart-box" style="margin-top:14px"><canvas id="cCompare"></canvas></div>
      <details><summary>ความหมายของตัวชี้วัด</summary><ul class="hint" style="line-height:1.8">
        <li><b>เตือนผิด (false alarm)</b>: สัดส่วนเวลาที่เตือนทั้งที่เป็นน้ำปกติ วัดบนรอบ normal ที่กันไว้ไม่ให้โมเดลเห็น</li>
        <li><b>จับได้ (detection)</b>: สัดส่วนเวลาที่เตือนระหว่างรอบ anomaly (รวมช่วงที่สารยังผสมไม่ทั่ว จึงไม่ถึง 100%)</li>
        <li><b>Balanced accuracy</b> = (1 − เตือนผิด + จับได้) / 2 ไม่ลำเอียงตามจำนวนข้อมูลแต่ละฝั่ง</li>
        <li><b>AUC</b>: ความสามารถแยกปกติ/ผิดปกติโดยไม่ขึ้นกับเส้นเตือน ถ้า AUC สูงแต่เตือนผิดเยอะ แปลว่าโมเดลแยกได้ แต่เส้นเตือนตั้งแคบไป</li>
        <li><b>เวลาจนเตือน</b>: วินาทีจากเริ่มรอบ anomaly ถึงการเตือนครั้งแรก</li></ul></details>
    </div>
    <div class="card span-12">
      <h2>ผลรายรอบ (โมเดลสุดท้ายที่เทรนด้วย normal ทุกรอบ)</h2>
      <p class="hint">รอบ normal ในตารางนี้ถูกใช้เทรนแล้ว ตัวเลขจึงดูดีกว่าความจริง ใช้ตารางเปรียบเทียบด้านบนเป็นหลัก</p>
      <div class="table-wrap"><table><thead><tr><th>รอบ</th><th>เงื่อนไข</th><th class="num">แถว</th><th class="num">ค่าเซนเซอร์ผิดพลาด</th>
        ${models.map((m) => `<th class="num">${esc(m.key.toUpperCase())}</th>`).join("")}<th>สาเหตุที่น่าจะเป็น</th></tr></thead>
        <tbody>${rep.runs.map((r) => `<tr><td>${r.run_id}</td>
          <td><span class="tag" style="background:${alpha(labelColor(r.label), .18)};color:${labelColor(r.label)}">${esc(r.label)}</span></td>
          <td class="num">${r.rows}</td><td class="num">${r.faults || ""}</td>
          ${models.map((m) => { const v = r.models[m.key].alarm_rate; const good = r.role === "normal" ? v <= .02 : v >= .5;
            return `<td class="num ${good ? "good" : "poor"}">${pct(v, 0)}</td>`; }).join("")}
          <td>${(r.cause || []).map((c) => esc(c.text)).join(", ")}</td></tr>`).join("")}</tbody></table></div>
    </div>
  </div>`;
  box.querySelectorAll("[data-use]").forEach((b) => (b.onclick = async () => {
    try { await api("/api/model", { key: b.dataset.use }); toast("เปลี่ยนโมเดลแล้ว"); setTimeout(renderReport, 1200); } catch (e) { toast(e.message); }
  }));
  mkChart("cCompare", {
    type: "bar",
    data: { labels: models.map((m) => m.name.split(" (")[0]), datasets: [
      { label: "เตือนผิด (%)", data: models.map((m) => (m.false_alarm ?? 0) * 100), backgroundColor: alpha("#ef4444", .75), borderRadius: 6 },
      { label: "จับได้ (%)", data: models.map((m) => (m.detection ?? 0) * 100), backgroundColor: alpha("#22c55e", .75), borderRadius: 6 },
      { label: "AUC × 100", data: models.map((m) => (m.auc ?? 0) * 100), backgroundColor: alpha("#3b82f6", .75), borderRadius: 6 },
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
  drawTimeline(); drawRoc(); setupScatter(); drawEmbedding();
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
    ds.push({ label: `รอบ ${run.run_id} · ${run.label}`, data: pts, borderColor: labelColor(run.label), backgroundColor: labelColor(run.label),
      borderWidth: 1.6, pointRadius: 0, tension: .2 });
  }
  ds.push({ label: "เส้นเตือน", data: [{ x: 0, y: 1 }, { x: off, y: 1 }], borderColor: css("--text"), borderDash: [6, 5], borderWidth: 1.2, pointRadius: 0 });
  mkChart("cTimeline", {
    type: "line", data: { datasets: ds },
    options: { parsing: true, interaction: { mode: "nearest", intersect: false },
      plugins: { legend: { position: "bottom", labels: { filter: (it, d) => {
        // one legend entry per label, not per run
        const lab = d.datasets[it.datasetIndex].label.split(" · ")[1];
        return !lab || d.datasets.findIndex((x) => x.label.split(" · ")[1] === lab) === it.datasetIndex;
      }, generateLabels: (ch) => Chart.defaults.plugins.legend.labels.generateLabels(ch).map((l) => ({ ...l, text: l.text.includes(" · ") ? l.text.split(" · ")[1] : l.text })) } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmt(c.parsed.y)}` } } },
      scales: { x: { type: "linear", title: { display: true, text: "เวลา (วินาที) · รอบเรียงต่อกัน" }, grid: { display: false } },
        y: { type: "logarithmic", min: 0.01, title: { display: true, text: "คะแนน / เส้นเตือน (log)" } } } },
  });
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
    g.font = "14px " + css("--font"); g.fillText("ต้องมีรอบ anomaly ก่อนจึงวาด ROC ได้", 20, 40);
    return;
  }
  ds.push({ label: "สุ่ม", data: [{ x: 0, y: 0 }, { x: 1, y: 1 }], borderColor: css("--muted"), borderDash: [4, 4], showLine: true, pointRadius: 0, borderWidth: 1 });
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
    zb.className = "btn ghost sm"; zb.id = "scZoom"; zb.textContent = "ดูทั้งหมด";
    zb.onclick = () => { S.scZoom = !S.scZoom; zb.textContent = S.scZoom ? "ดูทั้งหมด" : "ซูมน้ำปกติ"; drawScatter(); };
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
    ds.push({ label: "ขอบเขตน้ำปกติ (MSPC)", data: el, showLine: true, borderColor: css("--text"), borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, backgroundColor: "transparent" });
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
    $("embNote").innerHTML = `<span class="spin"></span>กำลังคำนวณ ${m.toUpperCase()} (t-SNE/UMAP อาจใช้ 5-30 วินาที)...`;
    try { S.emb.data = await api(`/api/embedding?method=${m}`); }
    catch (e) { $("embNote").textContent = e.message; S.emb.data = null; return; }
  }
  const E = S.emb.data;
  $("embNote").textContent = `${E.note} · ${E.xy.length} จุด · ${E.seconds}s`;
  const groups = {};
  E.xy.forEach((p, i) => {
    const key = S.emb.c === "label" ? E.label[i] : (E.alarm[i] ? "เตือน" : "ไม่เตือน");
    (groups[key] = groups[key] || []).push({ x: p[0], y: p[1] });
  });
  const colorOf = (k) => (k === "เตือน" ? "#ef4444" : k === "ไม่เตือน" ? "#22c55e" : labelColor(k));
  mkChart("cEmbed", { type: "scatter",
    data: { datasets: Object.entries(groups).map(([k, pts]) => ({ label: k, data: pts, backgroundColor: alpha(colorOf(k), .65), pointRadius: 3.2, borderWidth: 0 })) },
    options: { plugins: { legend: { position: "bottom" }, tooltip: { enabled: false } },
      scales: { x: { title: { display: true, text: `${m.toUpperCase()} 1` }, ticks: { display: m === "pca" } },
        y: { title: { display: true, text: `${m.toUpperCase()} 2` }, ticks: { display: m === "pca" } } } } });
}

/* ------------------------------------------------------------------ settings */
const ADV = [
  ["detector", "warmup_s", "ตัดช่วงเริ่มต้นของรอบ normal (วินาที)"], ["detector", "median_win", "หน้าต่าง median (ค่า)"],
  ["detector", "ewma_lambda", "EWMA λ (0-1, น้อย = เรียบกว่า)"], ["detector", "persist", "ต้องเกินเส้นติดกัน (ค่า)"],
  ["detector", "release", "เกณฑ์เลิกเตือน (สัดส่วนของเส้น)"], ["detector", "quantile", "quantile ของน้ำปกติ"],
  ["detector", "margin", "ระยะเผื่อของเส้นเตือน"], ["detector", "min_sigma", "MSPC: ขั้นต่ำ (เท่าของ floor)"],
  ["floors", "temp_c", "floor อุณหภูมิ (°C)"], ["floors", "tds_ppm", "floor TDS (ppm)"], ["floors", "turb_v", "floor ความขุ่น (V)"],
  ["floors", "ph", "floor pH"],
];
async function loadConfig() {
  try {
    const [cfg, ports] = await Promise.all([api("/api/config"), api("/api/ports")]);
    S.cfg = cfg;
    const opts = [["auto", "อัตโนมัติ"], ["SIM", "SIM (จำลอง)"]].concat(ports.ports.map((p) => [p.device, `${p.device} · ${p.desc}${p.chip ? " ✓ESP32" : ""}`]));
    if (!opts.some((o) => o[0] === cfg.port)) opts.push([cfg.port, cfg.port]);
    $("setPort").innerHTML = opts.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
    $("setPort").value = cfg.port;
    $("setPh").checked = cfg.use_ph; $("setDiv").value = cfg.ph_divider;
    const a = cfg.alerts;
    $("alSound").checked = a.sound; $("alNtfy").value = a.ntfy_topic; $("alTgTok").value = a.telegram_token; $("alTgChat").value = a.telegram_chat_id;
    $("alLineTok").value = a.line_token; $("alLineUser").value = a.line_user_id; $("alRecovery").checked = a.notify_recovery; $("alFault").checked = a.notify_fault;
    $("rmOn").checked = cfg.remote.enabled; $("rmPin").value = cfg.remote.pin; $("rmToken").value = cfg.ingest_token;
    $("advGrid").innerHTML = ADV.map(([g, k, t]) => `<div><label class="f">${t}</label><input type="number" step="any" data-g="${g}" data-k="${k}" value="${cfg[g][k]}"></div>`).join("");
    showCalib(cfg.calib);
    notifState();
  } catch (e) { toast(e.message); }
}
function showCalib(c) {
  $("calTdsInfo").textContent = `offset ${fmt(c.tds_offset_v, 3)} V · K ${fmt(c.tds_k, 3)}`;
  $("ph1Info").textContent = `บันทึกไว้: pH ${c.ph_points[0][0]} = ${fmt(c.ph_points[0][1], 3)} V`;
  $("ph2Info").textContent = `บันทึกไว้: pH ${c.ph_points[1][0]} = ${fmt(c.ph_points[1][1], 3)} V`;
}
async function saveConfig() {
  const pin = $("rmPin").value;
  if ($("rmOn").checked && !pin.startsWith("••••") && pin.length < 6) { toast("PIN ต้องยาวอย่างน้อย 6 ตัว"); return; }
  const patch = {
    port: $("setPort").value, use_ph: $("setPh").checked, ph_divider: +$("setDiv").value,
    alerts: { sound: $("alSound").checked, ntfy_topic: $("alNtfy").value.trim(), telegram_token: $("alTgTok").value.trim(),
      telegram_chat_id: $("alTgChat").value.trim(), line_token: $("alLineTok").value.trim(), line_user_id: $("alLineUser").value.trim(),
      notify_recovery: $("alRecovery").checked, notify_fault: $("alFault").checked },
    remote: { enabled: $("rmOn").checked, pin }, ingest_token: $("rmToken").value.trim(), detector: {}, floors: {},
  };
  document.querySelectorAll("#advGrid input").forEach((i) => (patch[i.dataset.g][i.dataset.k] = +i.value));
  const remoteChanged = S.cfg && S.cfg.remote.enabled !== patch.remote.enabled;
  try {
    S.cfg = await api("/api/config", patch);
    $("saveMsg").textContent = "บันทึกแล้ว ✓" + (remoteChanged ? " · ปิดแล้วเปิดโปรแกรมใหม่เพื่อให้การเข้าจากเครื่องอื่นมีผล" : "");
    toast("บันทึกการตั้งค่าแล้ว");
  } catch (e) { toast(e.message); }
}
async function calibrate(action, value) {
  try { const r = await api("/api/calibrate", { action, value }); showCalib(r.calib); toast("คาลิเบรตแล้ว ✓"); }
  catch (e) { toast(e.message, 4000); }
}

/* ------------------------------------------------------------------ wiring */
function wire() {
  document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.v)));
  document.addEventListener("click", unlockAudio, { once: true });
  $("btnSound").onclick = askNotify; $("btnNotifPerm").onclick = askNotify;
  $("monitorToggle").onchange = async (e) => {
    try { await api("/api/monitor", { on: e.target.checked }); unlockAudio(); }
    catch (err) { toast(err.message); e.target.checked = !e.target.checked; }
  };
  const SIMS = [["normal", "💧 น้ำปกติ"], ["salt", "🧂 เกลือ"], ["baking_soda", "🧁 เบกกิ้งโซดา"], ["starch", "🌽 แป้ง"],
    ["vinegar", "🍶 น้ำส้มสายชู"], ["hot_water", "♨️ น้ำร้อน"], ["probe_out", "🖐️ ยกหัววัด"]];
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
  $("btnRec").onclick = async () => {
    try {
      await api("/api/record/start", { run: +$("recRun").value, label: $("recLabel").value.trim(), minutes: +$("recMin").value });
      S.recWas = true; toast("เริ่มเก็บข้อมูล");
    } catch (e) { toast(e.message, 4000); }
  };
  $("btnRecStop").onclick = () => api("/api/record/stop", {}).catch((e) => toast(e.message));

  $("sensorSeg").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    document.querySelectorAll("#sensorSeg button").forEach((x) => x.classList.toggle("on", x === b));
  };
  $("btnTrain").onclick = async () => {
    const usePh = document.querySelector("#sensorSeg button.on")?.dataset.ph === "1";
    $("btnTrain").disabled = true;
    $("trainMsg").innerHTML = `<p class="hint"><span class="spin"></span>กำลังเทรนและเปรียบเทียบ 5 โมเดล (ประมาณ 10-60 วินาที)...</p>`;
    try {
      const r = await fetch("/api/train", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ use_ph: usePh }) }).then((x) => x.json());
      $("trainMsg").innerHTML = (r.ok ? `<div class="notice">เทรนเสร็จ ✓ ระบบเลือกโมเดลที่ดีที่สุดให้ใช้งานแล้ว</div>` : `<div class="notice bad">เทรนไม่สำเร็จ: ${esc(r.error)}</div>`)
        + (r.log && r.log.length ? `<details><summary>log</summary><pre class="log">${esc(r.log.join("\n"))}</pre></details>` : "");
      S.emb.data = null;
      await loadReport();
    } catch (e) { $("trainMsg").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
    $("btnTrain").disabled = false;
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
    $("testAlertMsg").textContent = "กำลังส่ง... (บันทึกการตั้งค่าก่อนทดสอบ)";
    try {
      await saveConfig();
      const r = await api("/api/alerts/test", {});
      $("testAlertMsg").textContent = r.results.map((x) => `${x.channel}: ${x.ok ? "ส่งแล้ว ✓" : "ล้มเหลว " + x.error}`).join(" · ");
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
  applyTheme(); chartDefaults(); wire(); initScoreChart();
  try {
    S.cfg = await api("/api/config");
    document.querySelectorAll("#sensorSeg button").forEach((b) => b.classList.toggle("on", (b.dataset.ph === "1") === S.cfg.use_ph));
  } catch (e) { /* first poll will report */ }
  const tab = location.hash.slice(1);
  if (["live", "record", "model", "viz", "settings"].includes(tab)) showTab(tab);
  poll();
}
init();
