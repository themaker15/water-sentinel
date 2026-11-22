"use strict";
/* WP4b: choose which data trains the model (roles, sensors, models, named datasets, guard rails, compare trainings).
   Loaded before app.js; uses its globals at call time. */

const TR_SENS = ["temp_c", "tds_ppm", "turb_v", "ph"];
const TR_MODELS = [["mspc", "MSPC (always on: used for causes and drift)"], ["pca", "PCA"], ["iforest", "Isolation Forest"], ["ocsvm", "One-Class SVM"], ["lof", "LOF"], ["threshold", "Simple threshold (baseline)"]];
const TR_ROLE_TXT = { train: "train: train", test: "test: held-out test", anomaly: "anomaly: test", exclude: "Not used" };
const TRSTATE = () => ({ runs: [], datasets: {}, files: {}, sensors: [], models: [], dataset: null, base: null, touched: false,
  label: "", session: "", q: "", onlyNew: false, onlyPh: false, check: null, seq: 0, inited: false, missing: 0 });

const trDefaultRole = (r) => (r.label === "normal" ? "train" : "anomaly");
const trUsable = (r) => r.rows > 0 && !r.excluded && r.label !== "unlabeled";
const trRoles = (r) => (r.label === "normal" ? ["train", "test", "exclude"] : ["anomaly", "exclude"]);

async function initTrainPicker(keep) {
  const T = S.tr;
  try {
    const [runs, ds] = await Promise.all([api("/api/runs"), api("/api/datasets")]);
    T.runs = runs.runs; T.datasets = ds.datasets || {};
    S.runs = runs.runs; S.archived = runs.archived || [];
    const cfg = S.cfg || (S.cfg = await api("/api/config"));
    if (!T.inited || !keep) {
      T.sensors = cfg.use_ph ? TR_SENS.slice() : TR_SENS.filter((k) => k !== "ph");
      T.models = TR_MODELS.map((m) => m[0]);
      T.files = {}; T.dataset = null; T.base = null; T.touched = false;
      const cur = ds.current;          // what the current model was trained with
      if (cur && cur.files) trLoadSelection(cur, cur.dataset);
      T.inited = true;
    }
    // runs that appeared since: under a stored selection the server treats unlisted runs as "not used", so we do too
    const stored = !!(ds.current && ds.current.files);
    T.runs.forEach((r) => { if (!(r.file in T.files)) T.files[r.file] = trUsable(r) && !stored ? trDefaultRole(r) : "exclude"; });
    T.runs.forEach((r) => { if (!trUsable(r)) T.files[r.file] = "exclude"; });
  } catch (e) { toast(e.message); return; }
  renderTrainPicker();
  trCheck();
}

function trLoadSelection(sel, name) {
  const T = S.tr, known = new Set(T.runs.map((r) => r.file));
  T.files = {}; T.missing = 0;
  Object.entries(sel.files || {}).forEach(([f, role]) => { if (known.has(f)) T.files[f] = role; else T.missing++; });
  T.runs.forEach((r) => { if (!(r.file in T.files)) T.files[r.file] = "exclude"; });
  if (sel.sensors) T.sensors = sel.sensors.slice();
  if (sel.models) T.models = sel.models.slice();
  T.dataset = name || null; T.base = name || null; T.touched = false;
}

function trMark() { const T = S.tr; if (T.dataset) T.base = T.dataset; T.dataset = null; T.touched = true; }

function trIsDefault() {
  const T = S.tr, cfg = S.cfg || {};
  if (T.dataset) return false;
  const defSens = cfg.use_ph ? TR_SENS : TR_SENS.filter((k) => k !== "ph");
  if (T.sensors.join() !== defSens.join() || T.models.length !== TR_MODELS.length) return false;
  return T.runs.every((r) => T.files[r.file] === (trUsable(r) ? trDefaultRole(r) : "exclude"));
}

function trSelection() {
  const T = S.tr;
  return { dataset: T.dataset, files: { ...T.files }, sensors: T.sensors.slice(), models: T.models.slice() };
}

function trSeenSet() {
  const m = S.report && S.report.meta;
  return new Set(m ? (m.seen || m.files || []) : []);
}

function trVisible() {
  const T = S.tr, q = T.q.trim().toLowerCase(), seen = trSeenSet(), hasModel = !!(S.report && S.report.meta);
  return T.runs.filter((r) => (!T.label || r.label === T.label) && (!T.session || r.session === T.session) &&
    (!q || `${r.run} ${r.label} ${r.note}`.toLowerCase().includes(q)) && (!T.onlyPh || r.has_ph) &&
    (!T.onlyNew || (hasModel && !seen.has(r.file) && trUsable(r))));
}

function renderTrainPicker() {
  const T = S.tr, box = $("trPicker");
  if (!box) return;
  const labels = [...new Set(T.runs.map((r) => r.label))].sort(), sessions = [...new Set(T.runs.map((r) => r.session))];
  const m = S.st && S.st.model;
  const prov = m && m.exists ? `The current model was trained on: ${m.dataset ? `dataset "${esc(m.dataset)}"` : "all usable runs"} · ${m.n_files} files · sensors ${m.features.map((f) => SENSORS[f].name).join(", ")}
      · ${(m.models || []).length || 5} models${(m.test_runs || []).length ? ` · ${m.test_runs.length} test-only runs` : ""}` : "No trained model yet";
  const dsNames = Object.keys(T.datasets);
  box.innerHTML = `
    <div class="row"><div><h2>Choose training data</h2><p class="hint" style="margin:0">${prov}</p></div></div>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-top:10px">
      <div><label class="f">Dataset (saved for reuse)</label>
        <select id="trDs"><option value="">— ${T.base && !T.dataset ? `Edited from dataset "${esc(T.base)}"` : "Unnamed"} —</option>${dsNames.map((n) => `<option value="${esc(n)}" ${T.dataset === n ? "selected" : ""}>${esc(n)} (${Object.values(T.datasets[n].files).filter((r) => r !== "exclude").length} runs)</option>`).join("")}</select>
        <div class="row" style="margin-top:6px"><button class="btn ghost sm" id="trDsSave">Save as dataset…</button>
          <button class="btn ghost sm" id="trDsDel" ${T.dataset ? "" : "disabled"} title="Deletes only the dataset name, not the data">Delete dataset</button></div>
        ${T.missing ? `<p class="hint" style="margin:6px 0 0;color:var(--warn)">This dataset refers to ${T.missing} files that no longer exist (archived?), so they are skipped</p>` : ""}</div>
      <div><label class="f">Sensors used (at least 2)</label>
        <div class="checks" id="trSens">${TR_SENS.map((k) => `<label><input type="checkbox" value="${k}" ${T.sensors.includes(k) ? "checked" : ""}> ${SENSORS[k].name}</label>`).join("")}</div>
        <p class="hint" style="margin:4px 0 0">With pH ticked, runs without pH data are skipped</p></div>
      <div><label class="f">Models to train and compare</label>
        <div class="checks" id="trModels">${TR_MODELS.map(([k, n]) => `<label><input type="checkbox" value="${k}" ${T.models.includes(k) ? "checked" : ""} ${k === "mspc" ? "disabled" : ""}> ${esc(n)}</label>`).join("")}</div></div>
    </div>
    <h3>Runs</h3>
    <div class="row" style="gap:8px">
      <select id="trLabel" style="width:auto"><option value="">All conditions</option>${labels.map((l) => `<option ${T.label === l ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
      <select id="trSession" style="width:auto"><option value="">All sessions</option>${sessions.map((s) => `<option ${T.session === s ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
      <input type="text" id="trQ" placeholder="Search (run no., note)" value="${esc(T.q)}" style="width:170px">
      <label class="chip ${T.onlyNew ? "on" : ""}" id="trNew">New runs only (never trained)</label>
      <label class="chip ${T.onlyPh ? "on" : ""}" id="trPh">Runs with pH only</label>
    </div>
    <div class="row" style="gap:8px;margin-top:8px"><span class="hint" style="margin:0">Set shown runs to:</span>
      <select id="trBulk" style="width:auto"><option value="default">Default (normal→train, others→anomaly)</option><option value="exclude">Not used</option><option value="test">normal → test (held out)</option></select>
      <button class="btn ghost sm" id="trBulkGo">Apply</button>
      <button class="btn ghost sm" id="trLastTest" title="Use the normal runs of the latest session as test runs the model never sees">Hold out latest session</button></div>
    <div class="table-wrap" style="margin-top:10px;max-height:380px"><table><thead><tr><th>Run</th><th>Condition</th><th>Time</th><th class="num">Rows</th><th>pH</th><th>Role</th><th></th></tr></thead>
      <tbody id="trBody"></tbody></table></div>
    <div id="trCheck" style="margin-top:10px"></div>
    <div class="row" style="margin-top:10px"><button class="btn" id="btnTrain">Train models</button>
      <span class="hint" style="margin:0" id="trSummary"></span></div>
    <div id="trainMsg"></div>`;
  renderTrBody();
  wireTrainPicker();
}

function renderTrBody() {
  const T = S.tr, seen = trSeenSet(), hasModel = !!(S.report && S.report.meta);
  const rows = trVisible().slice().sort((a, b) => a.start.localeCompare(b.start));
  let sess = null, html = "";
  rows.forEach((r) => {
    if (r.session !== sess) {
      sess = r.session;
      const inS = T.runs.filter((x) => x.session === sess);
      html += `<tr class="sess"><td colspan="7"><b>${esc(sess)}</b> <span class="hint" style="margin:0">${inS.length} runs · ${esc(inS[0].time)}</span></td></tr>`;
    }
    const role = T.files[r.file], ok = trUsable(r);
    const why = !ok ? (r.excluded ? "Set to not used on the Record tab" : r.label === "unlabeled" ? "No condition label yet" : "No data") : "";
    html += `<tr class="${role === "exclude" ? "muted" : ""}"><td>${r.run ?? "-"}${hasModel && ok && !seen.has(r.file) ? ' <span class="tag" style="background:var(--info-bg);color:var(--info)">New</span>' : ""}</td>
      <td><span class="tag" style="background:${alpha(labelColor(r.label), .18)};color:${labelColor(r.label)}">${esc(r.label)}</span></td>
      <td>${esc(r.time)}</td><td class="num">${r.rows}</td><td>${r.has_ph ? "✓" : "–"}</td>
      <td>${ok ? `<select data-trrole="${esc(r.file)}" style="width:auto;padding:5px 8px">${trRoles(r).map((x) => `<option value="${x}" ${role === x ? "selected" : ""}>${TR_ROLE_TXT[x]}</option>`).join("")}</select>`
        : `<span class="hint" style="margin:0">${why}</span>`}</td>
      <td style="white-space:normal;max-width:240px" class="hint">${esc(r.note)}</td></tr>`;
  });
  $("trBody").innerHTML = html || `<tr><td colspan="7" class="hint">No runs match the filters</td></tr>`;
  $("trBody").querySelectorAll("[data-trrole]").forEach((s) => (s.onchange = () => { T.files[s.dataset.trrole] = s.value; trChanged(); }));
}

function trChanged() {
  trMark();
  renderTrainPickerHead();
  renderTrBody();
  clearTimeout(S.tr.timer);
  S.tr.timer = setTimeout(trCheck, 350);
}

function renderTrainPickerHead() {          // only the dataset hint changes while editing
  const T = S.tr, sel = $("trDs");
  if (!sel) return;
  sel.options[0].textContent = `— ${T.base && !T.dataset ? `Edited from dataset "${T.base}"` : "Unnamed"} —`;
  sel.value = T.dataset || "";
  $("trDsDel").disabled = !T.dataset;
}

async function trCheck() {
  const T = S.tr, seq = ++T.seq;
  if (!$("trCheck")) return;
  try {
    const r = await api("/api/train/check", { selection: trSelection() });
    if (seq !== T.seq) return;
    T.check = r.check;
  } catch (e) { if (seq === T.seq) T.check = { ok: false, blockers: [e.message], warnings: [], counts: {} }; }
  const c = T.check, n = c.counts || {};
  $("trCheck").innerHTML = (n.train != null ? `<div class="chips" style="margin:0 0 6px"><span class="tag" style="background:var(--ok-bg);color:var(--ok)">train ${n.train || 0} runs (${n.sessions || 0} sessions)</span>
      <span class="tag" style="background:var(--info-bg);color:var(--info)">test ${n.test || 0}</span><span class="tag" style="background:var(--bad-bg);color:var(--bad)">anomaly ${n.anomaly || 0}</span>
      ${n.skipped ? `<span class="tag" style="background:var(--idle-bg);color:var(--muted)">skipped ${n.skipped}</span>` : ""}</div>` : "")
    + c.blockers.map((b) => `<div class="notice bad">⛔ ${esc(b)}</div>`).join("") + c.warnings.map((w) => `<div class="notice warn">⚠ ${esc(w)}</div>`).join("");
  $("btnTrain").disabled = !c.ok || !!(S.st && S.st.training);
  $("trSummary").textContent = trIsDefault() ? "Using defaults: all usable runs, all models" : `Custom: ${T.sensors.length} sensors · ${T.models.length} models`;
}

function wireTrainPicker() {
  const T = S.tr;
  $("trDs").onchange = () => {
    const n = $("trDs").value;
    if (!n) return;
    trLoadSelection({ files: T.datasets[n].files, sensors: T.datasets[n].sensors, models: T.datasets[n].models }, n);
    renderTrainPicker(); trCheck();
  };
  $("trDsSave").onclick = async () => {
    const name = prompt("Name this dataset (40 characters max)", T.dataset || T.base || "");
    if (!name) return;
    const note = prompt("Short note (optional)", "") || "";
    try {
      await api("/api/datasets", { name: name.trim(), files: T.files, sensors: T.sensors, models: T.models, note });
      T.datasets = (await api("/api/datasets")).datasets; T.dataset = name.trim(); T.base = T.dataset; T.touched = false;
      toast("Dataset saved"); renderTrainPicker(); trCheck();
    } catch (e) { toast(e.message, 4500); }
  };
  $("trDsDel").onclick = async () => {
    if (!T.dataset || !confirm(`Delete dataset name "${T.dataset}" ?\n(Only the dataset entry is deleted, no data files)`)) return;
    try { await api("/api/datasets/delete", { name: T.dataset }); T.datasets = (await api("/api/datasets")).datasets; T.base = T.dataset; T.dataset = null; toast("Dataset deleted");
      renderTrainPicker(); trCheck(); } catch (e) { toast(e.message); }
  };
  $("trSens").onchange = (e) => {
    const v = [...document.querySelectorAll("#trSens input:checked")].map((i) => i.value);
    if (v.length < 2) { e.target.checked = true; toast("Select at least 2 sensors"); return; }
    T.sensors = TR_SENS.filter((k) => v.includes(k)); trChanged();
  };
  $("trModels").onchange = () => {
    T.models = TR_MODELS.map((m) => m[0]).filter((k) => k === "mspc" || document.querySelector(`#trModels input[value="${k}"]`).checked);
    trChanged();
  };
  const refilter = () => { renderTrBody(); };
  $("trLabel").onchange = () => { T.label = $("trLabel").value; refilter(); };
  $("trSession").onchange = () => { T.session = $("trSession").value; refilter(); };
  $("trQ").oninput = () => { T.q = $("trQ").value; refilter(); };
  $("trNew").onclick = () => { T.onlyNew = !T.onlyNew; $("trNew").classList.toggle("on", T.onlyNew); refilter(); };
  $("trPh").onclick = () => { T.onlyPh = !T.onlyPh; $("trPh").classList.toggle("on", T.onlyPh); refilter(); };
  $("trBulkGo").onclick = () => {
    const mode = $("trBulk").value, vis = trVisible().filter(trUsable);
    let n = 0;
    vis.forEach((r) => {
      const to = mode === "default" ? trDefaultRole(r) : mode === "exclude" ? "exclude" : (r.label === "normal" ? "test" : null);
      if (to && trRoles(r).includes(to)) { T.files[r.file] = to; n++; }
    });
    toast(`Set ${n} runs`); trChanged();
  };
  $("trLastTest").onclick = () => {
    const normals = T.runs.filter((r) => trUsable(r) && r.label === "normal");
    if (!normals.length) { toast("No normal runs yet"); return; }
    const last = normals[normals.length - 1].session;
    normals.forEach((r) => { if (T.files[r.file] !== "exclude") T.files[r.file] = r.session === last ? "test" : "train"; });
    toast(`Session ${last} held out for testing`); trChanged();
  };
  $("btnTrain").onclick = () => doTrain(false);
}

function trainBody(force) {
  const cfg = S.cfg || {};
  if (!S.tr.inited || trIsDefault()) return { use_ph: !!cfg.use_ph, force };
  return { force, selection: trSelection() };
}

async function doTrain(force) {
  $("btnTrain").disabled = true;
  $("trainMsg").innerHTML = `<p class="hint"><span class="spin"></span>Training and comparing ${S.tr.models.length || 5} models (about 10–60 s)…</p>`;
  try {
    const x = await fetch("/api/train", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(trainBody(force)) });
    if (x.status === 401) { location.href = "/login"; return; }
    const r = await x.json();
    $("trainMsg").innerHTML = (r.unchanged ? `<div class="notice">Data and settings are the same as the last training, so nothing was retrained <button class="btn ghost sm" id="btnForce">Retrain anyway</button></div>`
      : r.ok ? `<div class="notice">Training done ✓ The best model is now active</div>` : `<div class="notice bad">Training failed: ${esc(r.error || r.detail || "")}</div>`)
      + (r.log && r.log.length ? `<details><summary>log</summary><pre class="log">${esc(r.log.join("\n"))}</pre></details>` : "");
    const f = $("btnForce"); if (f) f.onclick = () => doTrain(true);
    S.emb.data = null;
    S.cfg = await api("/api/config");
    const msg = $("trainMsg").innerHTML;
    await loadReport();
    loadSnaps();
    await initTrainPicker(true);
    $("trainMsg").innerHTML = msg;
    const f2 = $("btnForce"); if (f2) f2.onclick = () => doTrain(true);
  } catch (e) { $("trainMsg").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; if ($("btnTrain")) $("btnTrain").disabled = false; }
}

/* ---------------------------------------------------------------- compare two trainings */
function renderCompare() {
  const card = $("cmpCard"), h = (S.tuneMeta && S.tuneMeta.history) || [];
  if (h.length < 2) { card.hidden = true; return; }
  card.hidden = false;
  const label = (x, i) => `#${i + 1} · ${x.created} · ${x.dataset ? x.dataset : "All runs"} · ${x.n_files ?? "?"} files · ${(x.sensors || []).length || "?"} sensors`;
  const opts = (sel) => h.map((x, i) => `<option value="${i}" ${i === sel ? "selected" : ""}>${esc(label(x, i))}</option>`).join("");
  const a0 = S.cmp ? S.cmp.a : h.length - 2, b0 = S.cmp ? S.cmp.b : h.length - 1;
  $("cmpA").innerHTML = opts(a0); $("cmpB").innerHTML = opts(b0);
  $("cmpA").onchange = $("cmpB").onchange = drawCompare;
  drawCompare();
}

function drawCompare() {
  const h = S.tuneMeta.history, a = +$("cmpA").value, b = +$("cmpB").value;
  S.cmp = { a, b };
  const A = h[a], B = h[b];
  const diffs = [];
  const keysOf = (o) => Object.keys(o || {});
  if (A.files_hash !== B.files_hash) diffs.push("Data file contents");
  if (A.roles && B.roles) {
    const n = [...new Set([...keysOf(A.roles), ...keysOf(B.roles)])].filter((f) => (A.roles[f] || "exclude") !== (B.roles[f] || "exclude")).length;
    if (n) diffs.push(`Roles of ${n} runs`);
  } else diffs.push("Role of each run (not stored in older history)");
  if ((A.sensors || []).join() !== (B.sensors || []).join()) diffs.push("Sensors");
  const same = !diffs.length;
  const names = (S.report && S.report.model_names) || {};
  const keys = [...new Set([...Object.keys(A.metrics), ...Object.keys(B.metrics)])];
  const cols = [["false_alarm", "False alarms", -1, pct], ["test_false_alarm", "False alarms on test runs", -1, pct], ["detection", "Detected", 1, pct], ["auc", "AUC", 1, (v) => fmt(v, 3)]];
  const cell = (va, vb, better, f) => {
    const d = va != null && vb != null ? vb - va : null;
    const c = d == null || Math.abs(d) < 1e-9 ? "" : (d * better > 0 ? "delta-good" : "delta-bad");
    return `<td class="num">${f(va)}</td><td class="num">${f(vb)} ${c ? `<span class="${c}">${d > 0 ? "▲" : "▼"}</span>` : ""}</td>`;
  };
  const info = (x) => `${x.dataset ? `dataset "${esc(x.dataset)}"` : "All usable runs"} · ${x.n_files ?? "?"} files · ${(x.sensors || []).map((f) => SENSORS[f] ? SENSORS[f].name : f).join(", ") || "?"} · ${(x.models || []).length || "?"} models`;
  $("cmpBody").innerHTML = `
    <div class="notice ${same ? "" : "warn"}">${same ? "Same data, run roles and sensors: only settings/models differ, so the numbers compare directly" : `These two trainings differ in: ${diffs.join(", ")} · the numbers do not fully compare, because the evaluation runs differ`}</div>
    <p class="hint" style="margin:6px 0">A: ${info(A)}<br>B: ${info(B)}</p>
    <div class="table-wrap"><table><thead><tr><th>Model</th>${cols.map((c) => `<th class="num">${c[1]} A</th><th class="num">B</th>`).join("")}</tr></thead>
      <tbody>${keys.map((k) => `<tr><td>${esc((names[k] || k).split(" (")[0])}</td>${cols.map(([m, , better, f]) => cell((A.metrics[k] || {})[m], (B.metrics[k] || {})[m], better, f)).join("")}</tr>`).join("")}</tbody></table></div>
    <p class="legend-note">▲▼ = change from A to B · green = better, red = worse · "–" = not measured (e.g. no test runs)</p>`;
}


/* ---------------------------------------------------------------- saved models (pick an earlier training) */
async function loadSnaps() {
  let d;
  try { d = await api("/api/snapshots"); } catch (e) { return; }
  const list = d.snapshots || [];
  $("snapCard").hidden = !list.length;
  if (!list.length) return;
  const names = { temp_c: "Temperature", tds_ppm: "TDS", turb_v: "Turbidity", ph: "pH" };
  const activeKey = (d.active_features || []).join(",");
  let activeShown = false;
  const rows = list.map((s) => {
    const f = s.features || [];
    const m = (s.metrics || {})[s.best] || {};
    const isActive = !activeShown && s.created === d.active_created && f.join(",") === activeKey;
    if (isActive) activeShown = true;
    const btn = isActive ? `<span class="pill" style="background:var(--ok-bg);color:var(--ok);border-color:transparent">✓ In use</span>`
      : (isViewer() ? "" : `<button class="btn sm" data-snap="${esc(s.id)}">Use this model</button>`);
    return `<tr${isActive ? ' style="background:var(--ok-bg)"' : ""}><td>${esc(s.created || s.id)}</td>
      <td><b>${f.length}</b> Sensors<div class="hint" style="margin:0">${f.map((x) => names[x] || x).join(", ")}</div></td>
      <td>${esc(((S.report && S.report.model_names) || {})[s.best] || (s.best || "").toUpperCase())}</td>
      <td>${pct(m.false_alarm)}</td><td>${pct(m.detection)}</td><td>${m.auc == null ? "–" : fmt(m.auc, 3)}</td>
      <td>${s.n_sessions ?? "–"} / ${s.n_files ?? "–"}</td><td>${btn}</td></tr>`;
  }).join("");
  $("snapBody").innerHTML = `<div style="overflow-x:auto"><table><thead><tr><th>Trained</th><th>Sensors</th><th>Best model</th>
    <th>False alarms</th><th>Detected</th><th>AUC</th><th>Sessions / files</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  $("snapBody").querySelectorAll("[data-snap]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try {
      await api("/api/snapshots/use", { id: b.dataset.snap });
      toast("Model switched");
      S.tileKeys = [];
      await loadReport(); loadSnaps(); refreshModelSel();
    } catch (e) { toast(e.message, 5000); b.disabled = false; }
  }));
}


/* header dropdown: switch between saved trainings from any tab */
async function refreshModelSel() {
  const sel = $("modelSel");
  if (!sel || isViewer()) { if (sel) sel.hidden = true; return; }
  let d;
  try { d = await api("/api/snapshots"); } catch (e) { sel.hidden = true; return; }
  const list = d.snapshots || [];
  if (list.length < 2) { sel.hidden = true; return; }
  const activeKey = (d.active_features || []).join(",");
  let activeId = null;
  for (const s of list) if (!activeId && s.created === d.active_created && (s.features || []).join(",") === activeKey) activeId = s.id;
  sel.innerHTML = (activeId ? "" : `<option value="" selected>Current model</option>`) + list.map((s) => {
    const m = (s.metrics || {})[s.best] || {};
    const t = (s.created || s.id).slice(5);   // MM-DD HH:MM
    return `<option value="${esc(s.id)}"${s.id === activeId ? " selected" : ""}>${(s.features || []).length} sensors · ${esc(t)} · detected ${pct(m.detection)}</option>`;
  }).join("");
  sel.hidden = false;
  sel.onchange = async () => {
    if (!sel.value) return;
    sel.disabled = true;
    try {
      await api("/api/snapshots/use", { id: sel.value });
      toast("Model switched");
      S.tileKeys = [];
      if (typeof loadReport === "function") loadReport();
      if (typeof loadSnaps === "function") loadSnaps();
    } catch (e) { toast(e.message, 5000); }
    sel.disabled = false;
  };
}
