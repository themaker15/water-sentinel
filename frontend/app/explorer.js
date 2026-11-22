"use strict";
/* WP2: data explorer (Overview -> "Recorded"). Loaded before app.js; uses its globals at call time. */

const EX_MAX = 8;
const EXSTATE = () => ({ runs: [], archived: [], sel: [], cache: {}, mode: "aligned", series: "auto", ntu: false, score: false,
  focus: "tds_ppm", label: "", q: "", seq: 0 });
const exRunNo = (file) => { const r = S.ex.runs.find((x) => x.file === file); return r && r.run != null ? r.run : "-"; };

const EX_SENS = ["temp_c", "tds_ppm", "turb_v", "ph"];
const exVisible = () => $("exploreBox") && !$("exploreBox").hidden;
const exColor = (file) => seriesColor(Math.max(0, S.ex.sel.indexOf(file)));

function setLiveMode(mode) {
  document.querySelectorAll("#liveSeg button").forEach((b) => b.classList.toggle("on", b.dataset.m === mode));
  $("liveBox").hidden = mode !== "live";
  $("exploreBox").hidden = mode !== "rec";
  if (mode === "rec") loadExplorer();
}

async function loadExplorer() {
  try {
    const r = await api("/api/runs");
    S.runs = r.runs; S.archived = r.archived || [];
    exAfterChange(r.runs);
  } catch (e) { toast(e.message); }
}

/* called by loadRuns() as well, so edits made on the Record tab show up here */
function exAfterChange(runs) {
  S.ex.runs = runs; S.ex.cache = {};
  S.ex.sel = S.ex.sel.filter((f) => runs.some((r) => r.file === f));
  if (!exVisible()) return;
  renderExTable();
  exRefresh();
}

function exRuns() {
  const q = S.ex.q.trim().toLowerCase();
  return S.ex.runs.filter((r) => r.rows > 0 && (!S.ex.label || r.label === S.ex.label) &&
    (!q || `${r.run} ${r.label} ${r.note} ${r.session}`.toLowerCase().includes(q))).slice().reverse();
}

function renderExTable() {
  const labels = [...new Set(S.ex.runs.map((r) => r.label))].sort();
  $("exLabel").innerHTML = `<option value="">All conditions</option>` + labels.map((l) => `<option ${l === S.ex.label ? "selected" : ""}>${esc(l)}</option>`).join("");
  const rows = exRuns();
  $("exBody").innerHTML = rows.length ? rows.map((x) => {
    const on = S.ex.sel.includes(x.file);
    return `<tr class="${x.excluded ? "muted" : ""}"><td><input type="checkbox" data-pick="${esc(x.file)}" ${on ? "checked" : ""}></td>
      <td><span style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:${on ? exColor(x.file) : "transparent"}"></span>${x.run ?? "-"}</td>
      <td><span class="tag" style="background:${alpha(labelColor(x.label), .18)};color:${labelColor(x.label)}">${esc(x.label)}</span></td>
      <td>${esc(x.time)}</td><td class="num">${x.rows}</td><td>${esc(x.session)}</td>
      <td style="white-space:normal;min-width:120px;max-width:260px">${esc(x.note)}</td>
      <td><button class="btn ghost sm admin-only" data-exedit="${esc(x.file)}">Edit</button>
        <button class="btn ghost sm admin-only" data-exexcl="${esc(x.file)}">${x.excluded ? "Apply" : "Not used"}</button>
        <button class="btn ghost sm admin-only" data-exarch="${esc(x.file)}" title="Move to data/_excluded (not deleted)">Archive</button></td></tr>`;
  }).join("") : `<tr><td colspan="8" class="hint">${S.ex.runs.length ? "No runs match the filters" : "No data yet: go to the \"Record\" tab"}</td></tr>`;
  $("exBody").querySelectorAll("[data-pick]").forEach((c) => (c.onchange = () => exToggle(c.dataset.pick, c.checked, c)));
  $("exBody").querySelectorAll("[data-exedit]").forEach((b) => (b.onclick = () => editRun(b.dataset.exedit)));
  $("exBody").querySelectorAll("[data-exexcl]").forEach((b) => (b.onclick = () => toggleExcluded(b.dataset.exexcl)));
  $("exBody").querySelectorAll("[data-exarch]").forEach((b) => (b.onclick = () => archiveRun(b.dataset.exarch)));
  $("exCount").textContent = S.ex.sel.length ? `${S.ex.sel.length}/${EX_MAX} runs selected` : "Tick the runs you want to view (up to " + EX_MAX + " runs)";
  renderArchived();
}

function exToggle(file, on, box) {
  if (on) {
    if (S.ex.sel.length >= EX_MAX) { toast(`You can select up to ${EX_MAX} runs`); if (box) box.checked = false; return; }
    S.ex.sel.push(file);
  } else S.ex.sel = S.ex.sel.filter((f) => f !== file);
  renderExTable(); exRefresh();
}

async function toggleExcluded(file) {
  const x = S.runs.find((y) => y.file === file);
  try { await api("/api/run/meta", { file, excluded: !x.excluded }); await loadRuns(); } catch (e) { toast(e.message); }
}
async function archiveRun(file) {
  if (!confirm(`Move ${file} to the data/_excluded folder?\n(The file is not deleted; you can bring it back from the "Archive" list below)`)) return;
  try { await api(`/api/run/${encodeURIComponent(file)}/archive`, {}); toast("Moved to the archive (not deleted)"); await loadRuns(); }
  catch (e) { toast(e.message, 4500); }
}
async function restoreRun(file) {
  try { await api(`/api/run/${encodeURIComponent(file)}/restore`, {}); toast("Restored"); await loadRuns(); }
  catch (e) { toast(e.message, 4500); }
}
function renderArchived() {
  const a = S.archived || [];
  $("exArch").hidden = !a.length;
  $("exArchList").innerHTML = a.map((f) => `<li><span>${esc(f)}</span> <button class="btn ghost sm" data-rest="${esc(f)}">Restore</button></li>`).join("");
  $("exArchList").querySelectorAll("[data-rest]").forEach((b) => (b.onclick = () => restoreRun(b.dataset.rest)));
  $("exArchN").textContent = a.length;
}

/* ---------------------------------------------------------------- fetch + draw */
async function exRefresh() {
  const seq = ++S.ex.seq, files = S.ex.sel.slice();
  const empty = !files.length;
  $("exCharts").hidden = empty; $("exStatsCard").hidden = empty; $("exOpts").hidden = empty;
  $("exEmpty").hidden = !empty;
  if (empty) return;
  $("exStatus").innerHTML = '<span class="spin"></span>Loading…';
  try {
    await Promise.all(files.filter((f) => !S.ex.cache[f]).map(async (f) => { S.ex.cache[f] = await api(`/api/run/${encodeURIComponent(f)}`); }));
  } catch (e) { if (seq === S.ex.seq) $("exStatus").textContent = e.message; return; }
  if (seq !== S.ex.seq) return;
  $("exStatus").textContent = "";
  drawExplorer();
}

function exDetails() { return S.ex.sel.map((f) => S.ex.cache[f]).filter(Boolean); }

function exOffsets(ds) {
  if (S.ex.mode === "aligned") return ds.map(() => 0);
  let off = 0; return ds.map((d) => { const o = off; off += d.duration_s + 25; return o; });
}

function exSeriesFor(d, k) {
  if (k === "turb_v" && S.ex.ntu && d.ntu) return { raw: d.ntu.raw, filt: d.ntu.filt };
  return { raw: d.raw[k], filt: d.filt[k] };
}

function drawExplorer() {
  const ds = exDetails();
  if (!ds.length) return;
  const offs = exOffsets(ds);
  const sensors = EX_SENS.filter((k) => ds.some((d) => d.raw[k]));
  const anyNtu = ds.some((d) => d.ntu);
  $("exNtuWrap").hidden = !anyNtu;
  if (!anyNtu) S.ex.ntu = false;
  document.querySelectorAll("#exNtu button").forEach((b) => b.classList.toggle("on", (b.dataset.v === "ntu") === S.ex.ntu));
  const series = S.ex.series === "auto" ? (ds.length === 1 ? "both" : "median") : S.ex.series;
  document.querySelectorAll("#exSeries button").forEach((b) => b.classList.toggle("on", b.dataset.v === series));
  document.querySelectorAll("#exMode button").forEach((b) => b.classList.toggle("on", b.dataset.v === S.ex.mode));
  $("exScoreBtn").classList.toggle("on", S.ex.score);

  // chart cards (rebuilt every time: the sensor set can change with the selection)
  const prev = Object.keys(S.charts).filter((k) => k.startsWith("exc-"));
  prev.forEach((k) => { S.charts[k].destroy(); delete S.charts[k]; });
  const sName = (k) => (k === "turb_v" && S.ex.ntu ? "Turbidity (NTU)" : SENSORS[k].name + (SENSORS[k].unit ? ` (${SENSORS[k].unit})` : ""));
  $("exCharts").innerHTML = sensors.map((k) => `<div class="card"><div class="row"><h3 style="margin:0">${esc(sName(k))}</h3><span class="spacer"></span>
      <button class="btn ghost sm" data-png="exc-${k}">Save PNG</button></div>
      <div class="chart-box"><canvas id="exc-${k}"></canvas></div><p class="legend-note" id="exn-${k}"></p></div>`).join("")
    + (S.ex.score && !ds.some((d) => d.score) ? `<div class="card" style="grid-column:1/-1"><p class="hint" style="margin:0">Cannot score yet: there is no trained model, or the selected runs lack a sensor the model uses (e.g. the model uses pH but this run has none)</p></div>`
      : S.ex.score ? `<div class="card" style="grid-column:1/-1"><div class="row"><h3 style="margin:0">Anomaly score (the active model re-scores these runs)</h3><span class="spacer"></span>
      <button class="btn ghost sm" data-png="exc-score">Save PNG</button></div><div class="chart-box"><canvas id="exc-score"></canvas></div><p class="legend-note" id="exn-score"></p></div>` : "");
  $("exCharts").querySelectorAll("[data-png]").forEach((b) => (b.onclick = () => exPng(b.dataset.png)));

  const showBands = ds.length <= 3;
  sensors.forEach((k) => {
    const sets = [], bands = [], lines = [];
    ds.forEach((d, i) => {
      const s = exSeriesFor(d, k);
      if (!s.raw) return;
      const col = exColor(d.file), off = offs[i];
      const mk = (arr) => d.t.map((t, j) => ({ x: t + off, y: arr[j] }));
      const tag = `#${exRunNo(d.file)} ${d.label}`;
      if (series !== "median") sets.push({ label: "·raw " + tag, data: mk(s.raw), borderColor: alpha(col, series === "raw" ? 1 : .3), borderWidth: series === "raw" ? 1.4 : 1, pointRadius: 0, spanGaps: false });
      if (series !== "raw") sets.push({ label: tag, data: mk(s.filt), borderColor: col, borderWidth: 1.8, pointRadius: 0, spanGaps: false });
      if (showBands) {
        if (d.warmup_s) bands.push({ x0: off, x1: off + d.warmup_s, color: alpha(C.grid, .16) });
        (d.faults || []).forEach((f) => bands.push({ x0: off + f.t0, x1: off + f.t1 + d.interval_s, color: alpha(C.warn, .22) }));
        if (ds.length === 1) (d.markers || []).forEach((m) => lines.push({ x: off + m.t_s, color: C.warn, text: m.text, dash: [] }));
      }
      if (k === "turb_v" && S.ex.ntu && d.ntu && d.ntu.limit && i === 0) {
        sets.push({ label: `Reliable limit ${d.ntu.limit} NTU`, data: [{ x: off, y: d.ntu.limit }, { x: off + d.duration_s, y: d.ntu.limit }],
          borderColor: css("--muted"), borderDash: [5, 4], borderWidth: 1, pointRadius: 0 });
      }
    });
    mkChart("exc-" + k, { type: "line", plugins: [vlinesPlugin], data: { datasets: sets },
      options: { parsing: false, normalized: true, interaction: { mode: "nearest", intersect: false, axis: "x" },
        plugins: { legend: { position: "bottom", labels: { filter: (it) => !it.text.startsWith("·raw") } }, vlines: { lines, bands },
          tooltip: { callbacks: { label: (c) => `${c.dataset.label.replace("·raw ", "Raw ")}: ${fmt(c.parsed.y, k === "turb_v" && S.ex.ntu ? 1 : SENSORS[k].dp + 1)}` } } },
        scales: { x: { type: "linear", title: { display: true, text: S.ex.mode === "aligned" ? "Time since run start (s)" : "Time (s) · runs placed end to end" } } } } });
    const nf = ds.reduce((a, d) => a + (d.faults || []).length, 0);
    const note = [showBands ? "Grey band = start of run, trimmed for training" : "", showBands && nf ? "Orange band = sensor read error (value not used)" : "",
      k === "turb_v" && S.ex.ntu ? "Values below the limit are pinned to the limit, not real values" : ""].filter(Boolean).join(" · ");
    $("exn-" + k).textContent = note;
  });
  if (S.ex.score && ds.some((d) => d.score)) drawExScore(ds, offs);
  $("exFocus").innerHTML = sensors.map((k) => `<option value="${k}">${SENSORS[k].name}</option>`).join("")
    + (ds.some((d) => d.stats.turb_ntu) ? `<option value="turb_ntu">Turbidity (NTU)</option>` : "");
  if (![...$("exFocus").options].some((o) => o.value === S.ex.focus)) S.ex.focus = sensors[0];
  $("exFocus").value = S.ex.focus;
  renderExStats(ds);
}

function drawExScore(ds, offs) {
  const sets = [], notes = [];
  let xmax = 0;
  ds.forEach((d, i) => {
    const sc = d.score;
    if (!sc) { notes.push(`#${exRunNo(d.file)}: cannot score (no model, or this run lacks a sensor the model uses)`); return; }
    const col = exColor(d.file), off = offs[i];
    xmax = Math.max(xmax, off + sc.t[sc.t.length - 1]);
    sets.push({ label: `#${exRunNo(d.file)} ${d.label}`, data: sc.t.map((t, j) => ({ x: t + off, y: Math.max(sc.ratio[j], 0.01) })),
      borderColor: col, borderWidth: 1.6, pointRadius: 0, spanGaps: false });
    if (sc.in_training) notes.push(`#${exRunNo(d.file)} was used to train this model → its score looks better than reality`);
    else if (sc.first_alarm_s != null) notes.push(`#${exRunNo(d.file)} first alarm at ${Math.round(sc.first_alarm_s)} s`);
  });
  sets.push({ label: "Alarm line (1.0)", data: [{ x: 0, y: 1 }, { x: xmax || 1, y: 1 }], borderColor: css("--bad"), borderDash: [6, 5], borderWidth: 1.3, pointRadius: 0 });
  mkChart("exc-score", { type: "line", data: { datasets: sets },
    options: { parsing: false, plugins: { legend: { position: "bottom" } },
      scales: { x: { type: "linear", title: { display: true, text: "Time (s)" } }, y: { type: "logarithmic", min: 0.01, title: { display: true, text: "Score / alarm line (log)" } } } } });
  const m = ds.find((d) => d.score);
  $("exn-score").textContent = (m ? `Model: ${(S.st && S.st.model.active_name) || m.score.model}. ` : "") + notes.join(" · ");
}

function renderExStats(ds) {
  const k = S.ex.focus, unit = k === "turb_ntu" ? "NTU" : (SENSORS[k] || {}).unit || "";
  const dp = k === "turb_ntu" ? 1 : ((SENSORS[k] || {}).dp ?? 2) + 1;
  const f = (v, d = dp) => (v == null ? "–" : fmt(v, d));
  $("exStatsHead").innerHTML = `<tr><th>Run</th><th>Condition</th><th class="num">Rows</th><th class="num">Duration (s)</th><th class="num">Every (s)</th><th class="num">Errors</th>
    <th class="num">Mean</th><th class="num">SD</th><th class="num">Min</th><th class="num">Max</th><th class="num">Trend /min</th><th class="num">noise</th><th class="num">Settled after (s)</th>${k === "turb_ntu" ? '<th class="num">Below limit</th>' : ""}</tr>`;
  $("exStatsBody").innerHTML = ds.map((d) => {
    const st = d.stats[k], run = exRunNo(d.file);
    if (!st) return `<tr><td>${run ?? "-"}</td><td>${esc(d.label)}</td><td colspan="12" class="hint">No data for this sensor</td></tr>`;
    return `<tr><td><span style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:${exColor(d.file)}"></span>${run ?? "-"}</td>
      <td><span class="tag" style="background:${alpha(labelColor(d.label), .18)};color:${labelColor(d.label)}">${esc(d.label)}</span></td>
      <td class="num">${d.rows}</td><td class="num">${fmt(d.duration_s, 0)}</td><td class="num">${fmt(d.interval_s, 2)}</td><td class="num">${d.fault_rows || ""}</td>
      <td class="num">${f(st.mean)}</td><td class="num">${f(st.sd)}</td><td class="num">${f(st.min)}</td><td class="num">${f(st.max)}</td>
      <td class="num">${st.slope_per_min == null ? "–" : (st.slope_per_min > 0 ? "+" : "") + fmt(st.slope_per_min, dp)}</td><td class="num">${f(st.noise, dp + 1)}</td>
      <td class="num">${st.settle_s == null ? "not settled" : fmt(st.settle_s, 0)}</td>${k === "turb_ntu" ? `<td class="num">${st.below_limit_frac == null ? "–" : pct(st.below_limit_frac, 0)}</td>` : ""}</tr>`;
  }).join("");
  $("exUnit").textContent = unit ? `Unit: ${unit}` : "";
  $("exCsv").href = "/api/runs/export.csv?files=" + encodeURIComponent(S.ex.sel.join(","));
}

function exPng(id) {
  const ch = S.charts[id];
  if (!ch) return;
  const src = ch.canvas, c = document.createElement("canvas");
  c.width = src.width; c.height = src.height;
  const g = c.getContext("2d");
  g.fillStyle = css("--panel"); g.fillRect(0, 0, c.width, c.height);
  g.drawImage(src, 0, 0);
  const a = document.createElement("a");
  a.href = c.toDataURL("image/png"); a.download = `water-sentinel-${id.replace("exc-", "")}.png`;
  document.body.appendChild(a); a.click(); a.remove();
}

function wireExplorer() {
  $("liveSeg").onclick = (e) => { const b = e.target.closest("button"); if (b) setLiveMode(b.dataset.m); };
  $("exLabel").onchange = () => { S.ex.label = $("exLabel").value; renderExTable(); };
  $("exSearch").oninput = () => { S.ex.q = $("exSearch").value; renderExTable(); };
  $("exClear").onclick = () => { S.ex.sel = []; renderExTable(); exRefresh(); };
  $("exAllShown").onclick = () => {
    S.ex.sel = exRuns().slice(0, EX_MAX).map((r) => r.file);
    if (exRuns().length > EX_MAX) toast(`Selected the first ${EX_MAX} runs shown`);
    renderExTable(); exRefresh();
  };
  const seg = (id, key, after) => ($(id).onclick = (e) => { const b = e.target.closest("button"); if (!b) return; S.ex[key] = b.dataset.v; after && after(b.dataset.v); drawExplorer(); });
  seg("exMode", "mode"); seg("exSeries", "series");
  $("exNtu").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; S.ex.ntu = b.dataset.v === "ntu"; drawExplorer(); };
  $("exScoreBtn").onclick = () => { S.ex.score = !S.ex.score; drawExplorer(); };
  $("exFocus").onchange = () => { S.ex.focus = $("exFocus").value; renderExStats(exDetails()); };
}
