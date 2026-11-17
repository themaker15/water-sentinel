"use strict";
/* WP4b: choose which data trains the model (roles, sensors, models, named datasets, guard rails, compare trainings).
   Loaded before app.js; uses its globals at call time. */

const TR_SENS = ["temp_c", "tds_ppm", "turb_v", "ph"];
const TR_MODELS = [["mspc", "MSPC (ล็อก: ใช้อธิบายสาเหตุและ drift)"], ["pca", "PCA"], ["iforest", "Isolation Forest"], ["ocsvm", "One-Class SVM"], ["lof", "LOF"]];
const TR_ROLE_TXT = { train: "train: เทรน", test: "test: กันไว้ทดสอบ", anomaly: "anomaly: ทดสอบ", exclude: "ไม่ใช้" };
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
  const prov = m && m.exists ? `โมเดลปัจจุบันเทรนด้วย: ${m.dataset ? `ชุด "${esc(m.dataset)}"` : "ข้อมูลทุกรอบที่ใช้ได้"} · ${m.n_files} ไฟล์ · เซนเซอร์ ${m.features.map((f) => SENSORS[f].name).join(", ")}
      · โมเดล ${(m.models || []).length || 5} ตัว${(m.test_runs || []).length ? ` · รอบ test-only ${m.test_runs.length}` : ""}` : "ยังไม่มีโมเดลที่เทรนไว้";
  const dsNames = Object.keys(T.datasets);
  box.innerHTML = `
    <div class="row"><div><h2>เลือกข้อมูลที่ใช้เทรน</h2><p class="hint" style="margin:0">${prov}</p></div></div>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-top:10px">
      <div><label class="f">ชุดข้อมูล (บันทึกไว้เรียกใช้ซ้ำ)</label>
        <select id="trDs"><option value="">— ${T.base && !T.dataset ? `แก้ไขจากชุด "${esc(T.base)}"` : "ยังไม่ตั้งชื่อ"} —</option>${dsNames.map((n) => `<option value="${esc(n)}" ${T.dataset === n ? "selected" : ""}>${esc(n)} (${Object.values(T.datasets[n].files).filter((r) => r !== "exclude").length} รอบ)</option>`).join("")}</select>
        <div class="row" style="margin-top:6px"><button class="btn ghost sm" id="trDsSave">บันทึกเป็นชุดข้อมูล…</button>
          <button class="btn ghost sm" id="trDsDel" ${T.dataset ? "" : "disabled"} title="ลบเฉพาะชื่อชุด ไม่ลบข้อมูล">ลบชุดนี้</button></div>
        ${T.missing ? `<p class="hint" style="margin:6px 0 0;color:var(--warn)">ชุดนี้อ้างถึงไฟล์ ${T.missing} ไฟล์ที่ไม่พบแล้ว (ถูกย้ายเก็บ?) จึงข้ามไป</p>` : ""}</div>
      <div><label class="f">เซนเซอร์ที่ใช้ (อย่างน้อย 2)</label>
        <div class="checks" id="trSens">${TR_SENS.map((k) => `<label><input type="checkbox" value="${k}" ${T.sensors.includes(k) ? "checked" : ""}> ${SENSORS[k].name}</label>`).join("")}</div>
        <p class="hint" style="margin:4px 0 0">ติ๊ก pH แล้วรอบที่ไม่มีข้อมูล pH จะถูกข้าม</p></div>
      <div><label class="f">โมเดลที่เทรนเปรียบเทียบ</label>
        <div class="checks" id="trModels">${TR_MODELS.map(([k, n]) => `<label><input type="checkbox" value="${k}" ${T.models.includes(k) ? "checked" : ""} ${k === "mspc" ? "disabled" : ""}> ${esc(n)}</label>`).join("")}</div></div>
    </div>
    <h3>รอบข้อมูล</h3>
    <div class="row" style="gap:8px">
      <select id="trLabel" style="width:auto"><option value="">ทุกเงื่อนไข</option>${labels.map((l) => `<option ${T.label === l ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
      <select id="trSession" style="width:auto"><option value="">ทุก session</option>${sessions.map((s) => `<option ${T.session === s ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
      <input type="text" id="trQ" placeholder="ค้นหา (เลขรอบ/โน้ต)" value="${esc(T.q)}" style="width:170px">
      <label class="chip ${T.onlyNew ? "on" : ""}" id="trNew">เฉพาะรอบใหม่ (ยังไม่เคยเทรน)</label>
      <label class="chip ${T.onlyPh ? "on" : ""}" id="trPh">เฉพาะที่มี pH</label>
    </div>
    <div class="row" style="gap:8px;margin-top:8px"><span class="hint" style="margin:0">ตั้งให้รอบที่แสดงอยู่:</span>
      <select id="trBulk" style="width:auto"><option value="default">ค่าเริ่มต้น (normal→train, อื่นๆ→anomaly)</option><option value="exclude">ไม่ใช้</option><option value="test">normal → test (กันไว้ทดสอบ)</option></select>
      <button class="btn ghost sm" id="trBulkGo">ใช้</button>
      <button class="btn ghost sm" id="trLastTest" title="เอา normal ของ session ล่าสุดไปเป็นรอบทดสอบที่โมเดลไม่เคยเห็น">กัน session ล่าสุดไว้ทดสอบ</button></div>
    <div class="table-wrap" style="margin-top:10px;max-height:380px"><table><thead><tr><th>รอบ</th><th>เงื่อนไข</th><th>เวลา</th><th class="num">แถว</th><th>pH</th><th>ใช้เป็น</th><th></th></tr></thead>
      <tbody id="trBody"></tbody></table></div>
    <div id="trCheck" style="margin-top:10px"></div>
    <div class="row" style="margin-top:10px"><button class="btn" id="btnTrain">เทรนโมเดล</button>
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
      html += `<tr class="sess"><td colspan="7"><b>${esc(sess)}</b> <span class="hint" style="margin:0">${inS.length} รอบ · ${esc(inS[0].time)}</span></td></tr>`;
    }
    const role = T.files[r.file], ok = trUsable(r);
    const why = !ok ? (r.excluded ? "ตั้งเป็นไม่ใช้ในหน้าเก็บข้อมูล" : r.label === "unlabeled" ? "ยังไม่ได้ตั้งชื่อเงื่อนไข" : "ไม่มีข้อมูล") : "";
    html += `<tr class="${role === "exclude" ? "muted" : ""}"><td>${r.run ?? "-"}${hasModel && ok && !seen.has(r.file) ? ' <span class="tag" style="background:var(--info-bg);color:var(--info)">ใหม่</span>' : ""}</td>
      <td><span class="tag" style="background:${alpha(labelColor(r.label), .18)};color:${labelColor(r.label)}">${esc(r.label)}</span></td>
      <td>${esc(r.time)}</td><td class="num">${r.rows}</td><td>${r.has_ph ? "✓" : "–"}</td>
      <td>${ok ? `<select data-trrole="${esc(r.file)}" style="width:auto;padding:5px 8px">${trRoles(r).map((x) => `<option value="${x}" ${role === x ? "selected" : ""}>${TR_ROLE_TXT[x]}</option>`).join("")}</select>`
        : `<span class="hint" style="margin:0">${why}</span>`}</td>
      <td style="white-space:normal;max-width:240px" class="hint">${esc(r.note)}</td></tr>`;
  });
  $("trBody").innerHTML = html || `<tr><td colspan="7" class="hint">ไม่มีรอบที่ตรงกับตัวกรอง</td></tr>`;
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
  sel.options[0].textContent = `— ${T.base && !T.dataset ? `แก้ไขจากชุด "${T.base}"` : "ยังไม่ตั้งชื่อ"} —`;
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
  $("trCheck").innerHTML = (n.train != null ? `<div class="chips" style="margin:0 0 6px"><span class="tag" style="background:var(--ok-bg);color:var(--ok)">train ${n.train || 0} รอบ (${n.sessions || 0} session)</span>
      <span class="tag" style="background:var(--info-bg);color:var(--info)">test ${n.test || 0}</span><span class="tag" style="background:var(--bad-bg);color:var(--bad)">anomaly ${n.anomaly || 0}</span>
      ${n.skipped ? `<span class="tag" style="background:var(--idle-bg);color:var(--muted)">ข้าม ${n.skipped}</span>` : ""}</div>` : "")
    + c.blockers.map((b) => `<div class="notice bad">⛔ ${esc(b)}</div>`).join("") + c.warnings.map((w) => `<div class="notice warn">⚠ ${esc(w)}</div>`).join("");
  $("btnTrain").disabled = !c.ok || !!(S.st && S.st.training);
  $("trSummary").textContent = trIsDefault() ? "ใช้ค่าเริ่มต้น: ทุกรอบที่ใช้ได้ ทุกโมเดล" : `เลือกเอง: ${T.sensors.length} เซนเซอร์ · ${T.models.length} โมเดล`;
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
    const name = prompt("ตั้งชื่อชุดข้อมูลนี้ (ไม่เกิน 40 ตัวอักษร)", T.dataset || T.base || "");
    if (!name) return;
    const note = prompt("โน้ตสั้นๆ (ไม่บังคับ)", "") || "";
    try {
      await api("/api/datasets", { name: name.trim(), files: T.files, sensors: T.sensors, models: T.models, note });
      T.datasets = (await api("/api/datasets")).datasets; T.dataset = name.trim(); T.base = T.dataset; T.touched = false;
      toast("บันทึกชุดข้อมูลแล้ว"); renderTrainPicker(); trCheck();
    } catch (e) { toast(e.message, 4500); }
  };
  $("trDsDel").onclick = async () => {
    if (!T.dataset || !confirm(`ลบชื่อชุดข้อมูล "${T.dataset}" ?\n(ลบเฉพาะรายการชุด ไม่ลบไฟล์ข้อมูลใดๆ)`)) return;
    try { await api("/api/datasets/delete", { name: T.dataset }); T.datasets = (await api("/api/datasets")).datasets; T.base = T.dataset; T.dataset = null; toast("ลบชุดข้อมูลแล้ว");
      renderTrainPicker(); trCheck(); } catch (e) { toast(e.message); }
  };
  $("trSens").onchange = (e) => {
    const v = [...document.querySelectorAll("#trSens input:checked")].map((i) => i.value);
    if (v.length < 2) { e.target.checked = true; toast("เลือกเซนเซอร์อย่างน้อย 2 ตัว"); return; }
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
    toast(`ตั้งให้ ${n} รอบ`); trChanged();
  };
  $("trLastTest").onclick = () => {
    const normals = T.runs.filter((r) => trUsable(r) && r.label === "normal");
    if (!normals.length) { toast("ยังไม่มีรอบ normal"); return; }
    const last = normals[normals.length - 1].session;
    normals.forEach((r) => { if (T.files[r.file] !== "exclude") T.files[r.file] = r.session === last ? "test" : "train"; });
    toast(`กัน session ${last} ไว้ทดสอบ`); trChanged();
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
  $("trainMsg").innerHTML = `<p class="hint"><span class="spin"></span>กำลังเทรนและเปรียบเทียบ ${S.tr.models.length || 5} โมเดล (ประมาณ 10-60 วินาที)...</p>`;
  try {
    const x = await fetch("/api/train", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(trainBody(force)) });
    if (x.status === 401) { location.href = "/login"; return; }
    const r = await x.json();
    $("trainMsg").innerHTML = (r.unchanged ? `<div class="notice">ข้อมูลและการตั้งค่าเหมือนการเทรนครั้งก่อน จึงไม่ได้เทรนซ้ำ <button class="btn ghost sm" id="btnForce">เทรนใหม่อยู่ดี</button></div>`
      : r.ok ? `<div class="notice">เทรนเสร็จ ✓ ระบบเลือกโมเดลที่ดีที่สุดให้ใช้งานแล้ว</div>` : `<div class="notice bad">เทรนไม่สำเร็จ: ${esc(r.error || r.detail || "")}</div>`)
      + (r.log && r.log.length ? `<details><summary>log</summary><pre class="log">${esc(r.log.join("\n"))}</pre></details>` : "");
    const f = $("btnForce"); if (f) f.onclick = () => doTrain(true);
    S.emb.data = null;
    S.cfg = await api("/api/config");
    const msg = $("trainMsg").innerHTML;
    await loadReport();
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
  const label = (x, i) => `#${i + 1} · ${x.created} · ${x.dataset ? x.dataset : "ทุกรอบ"} · ${x.n_files ?? "?"} ไฟล์ · ${(x.sensors || []).length || "?"} เซนเซอร์`;
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
  if (A.files_hash !== B.files_hash) diffs.push("เนื้อหาไฟล์ข้อมูล");
  if (A.roles && B.roles) {
    const n = [...new Set([...keysOf(A.roles), ...keysOf(B.roles)])].filter((f) => (A.roles[f] || "exclude") !== (B.roles[f] || "exclude")).length;
    if (n) diffs.push(`บทบาทของ ${n} รอบ`);
  } else diffs.push("บทบาทของแต่ละรอบ (ประวัติเก่าไม่ได้เก็บไว้)");
  if ((A.sensors || []).join() !== (B.sensors || []).join()) diffs.push("เซนเซอร์");
  const same = !diffs.length;
  const names = (S.report && S.report.model_names) || {};
  const keys = [...new Set([...Object.keys(A.metrics), ...Object.keys(B.metrics)])];
  const cols = [["false_alarm", "เตือนผิด", -1, pct], ["test_false_alarm", "เตือนผิดบนรอบ test", -1, pct], ["detection", "จับได้", 1, pct], ["auc", "AUC", 1, (v) => fmt(v, 3)]];
  const cell = (va, vb, better, f) => {
    const d = va != null && vb != null ? vb - va : null;
    const c = d == null || Math.abs(d) < 1e-9 ? "" : (d * better > 0 ? "delta-good" : "delta-bad");
    return `<td class="num">${f(va)}</td><td class="num">${f(vb)} ${c ? `<span class="${c}">${d > 0 ? "▲" : "▼"}</span>` : ""}</td>`;
  };
  const info = (x) => `${x.dataset ? `ชุด "${esc(x.dataset)}"` : "ทุกรอบที่ใช้ได้"} · ${x.n_files ?? "?"} ไฟล์ · ${(x.sensors || []).map((f) => SENSORS[f] ? SENSORS[f].name : f).join(", ") || "?"} · ${(x.models || []).length || "?"} โมเดล`;
  $("cmpBody").innerHTML = `
    <div class="notice ${same ? "" : "warn"}">${same ? "ข้อมูล บทบาทของรอบ และเซนเซอร์เหมือนกัน: ที่ต่างคือการตั้งค่า/โมเดล เทียบตัวเลขกันตรง ๆ ได้" : `สองครั้งนี้ต่างกันที่: ${diffs.join(", ")} · ตัวเลขเทียบกันตรง ๆ ไม่ได้เต็มที่ เพราะชุดรอบที่ใช้ประเมินไม่เหมือนกัน`}</div>
    <p class="hint" style="margin:6px 0">A: ${info(A)}<br>B: ${info(B)}</p>
    <div class="table-wrap"><table><thead><tr><th>โมเดล</th>${cols.map((c) => `<th class="num">${c[1]} A</th><th class="num">B</th>`).join("")}</tr></thead>
      <tbody>${keys.map((k) => `<tr><td>${esc((names[k] || k).split(" (")[0])}</td>${cols.map(([m, , better, f]) => cell((A.metrics[k] || {})[m], (B.metrics[k] || {})[m], better, f)).join("")}</tr>`).join("")}</tbody></table></div>
    <p class="legend-note">▲▼ = B เปลี่ยนจาก A · เขียว = ดีขึ้น แดง = แย่ลง · "–" = ไม่ได้วัด (เช่นไม่มีรอบ test)</p>`;
}
