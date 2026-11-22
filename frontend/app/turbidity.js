"use strict";
/* WP5 (turbidity in NTU: tile text + calibration card) and WP6 (experimental likely cause).
   Loaded BEFORE app.js: it only defines functions; they use app.js globals ($, api, S, fmt, ...) at call time. */

/* ------------------------------------------------------------------ WP5: tile text */
function turbDisplay(L) {
  const t = L && L.turb, volts = L && L.turb_v;
  if (t && t.text && ["ok", "below_limit", "over_range"].includes(t.quality)) {
    const cal = t.estimate ? "Estimate (not calibrated against a real meter)" : "Calibrated with your standards";
    return { val: t.text.replace(/\s*NTU$/, ""), unit: "NTU", sub: `${fmt(volts, 3)} V · ${cal}`,
      title: `Raw voltage ${fmt(volts, 3)} V${t.limit ? ` · lowest reliable ~${t.limit} NTU` : ""}` };
  }
  const why = { no_reference: "No clear-water reference yet (Settings → Turbidity)", error: "The conversion curve has a problem, see Settings", off: "" }[t && t.quality] || "";
  return { val: fmt(volts, 3), unit: "V", sub: why, title: "" };
}

/* ------------------------------------------------------------------ WP5: settings card */
async function loadTurb() {
  try { S.turb = await api("/api/turbidity"); renderTurbSettings(); }
  catch (e) { $("turbBox").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
}

function renderTurbSettings() {
  const info = S.turb, c = info.cal, live = info.live || {};
  const noRef = !c.v_clear;
  const pts = (c.points || []).slice().map((p, i) => ({ p, i })).sort((a, b) => a.p[0] - b.p[0]);
  $("turbBox").innerHTML = `
    <p class="hint">The sensor outputs <b>a voltage</b>, not real NTU. NTU is shown only to make it easier to read (the detector still uses the raw voltage) ·
      Uncalibrated numbers are estimates: good for comparing "cloudier or clearer" , but not standard values</p>
    <div class="notice ${noRef ? "warn" : ""}" id="tbLive">${turbLiveText()}</div>
    <h3>1) Clear-water reference</h3>
    <div class="row"><button class="btn ghost sm" id="tbRef">Set as clear water (median of the last 30 s)</button>
      <span class="hint" style="margin:0">${noRef ? "Let the probe settle in clear or distilled water before pressing" :
        `Reference ${fmt(c.v_clear, 3)} V${c.t_clear != null ? ` at ${fmt(c.t_clear, 1)} °C` : ""} · noise ±${fmt(c.noise_v, 4)} V`}</span></div>
    <h3>2) Conversion curve</h3>
    <select id="tbCurve">${Object.entries(info.curves).map(([k, t]) => `<option value="${k}" ${c.curve === k ? "selected" : ""} ${k === "user_points" && !pts.length ? "disabled" : ""}>${esc(t)}</option>`).join("")}</select>
    <h3>3) Your standards (optional)</h3>
    <p class="hint">Have a turbidity meter or a standard (e.g. formazin 100 NTU)? Dip the probe, enter the NTU reading → press "Read now" ·
      More points are better (start with clear water at 0 NTU). Once you have points, values are no longer labelled "estimate"</p>
    <div class="row"><input type="number" id="tbNtu" min="0" max="5000" step="any" placeholder="NTU reading" style="width:150px">
      <button class="btn ghost sm" id="tbAdd" ${noRef ? "disabled" : ""}>Read now as a standard point</button></div>
    ${pts.length ? `<div class="table-wrap" style="margin-top:8px;max-height:200px"><table><thead><tr><th class="num">NTU</th><th class="num">Voltage (V)</th><th class="num">°C</th><th></th></tr></thead>
      <tbody>${pts.map(({ p, i }) => `<tr><td class="num">${fmt(p[0], p[0] < 100 ? 1 : 0)}</td><td class="num">${fmt(p[1], 3)}</td><td class="num">${p[2] == null ? "–" : fmt(p[2], 1)}</td>
        <td><button class="btn ghost sm" data-tbdel="${i}">Delete point</button></td></tr>`).join("")}</tbody></table></div>` : ""}
    <h3>4) Temperature compensation</h3>
    <div class="row"><input type="number" id="tbCoef" step="0.0001" min="-0.02" max="0" value="${c.temp_coeff}" style="width:130px"><span class="hint" style="margin:0">per °C (default −0.0022)</span>
      <button class="btn ghost sm" id="tbTempco">Suggest from my data</button></div>
    <p class="hint" id="tbCoefMsg"></p>
    <div class="row" style="margin-top:6px"><label class="switch" style="font-weight:600"><input type="checkbox" id="tbOn" ${c.ntu_display ? "checked" : ""}><span class="track"></span><span>Show turbidity in NTU</span></label>
      <button class="btn sm" id="tbSave">Save</button><span class="hint" style="margin:0" id="tbMsg"></span></div>
    <div class="chart-box" style="height:220px;margin-top:12px"><canvas id="cTurbCurve"></canvas></div>
    <p class="legend-note">Line = measured voltage → converted NTU · yellow points = your standards · red point = current value ·
      ${info.limit ? `below ~${info.limit} NTU is shown as "&lt; ${info.limit} NTU" (sensor noise prevents finer readings)` : "Set a clear-water reference to see the lowest reliable value"}</p>`;
  $("tbRef").onclick = () => turbAct("/api/turbidity/reference", {}, "Clear-water reference set");
  $("tbAdd").onclick = () => {
    const v = $("tbNtu").value;
    if (v === "") { toast("Enter an NTU value first"); return; }
    turbAct("/api/turbidity/point", { ntu: +v }, "Standard point added");
  };
  document.querySelectorAll("[data-tbdel]").forEach((b) => (b.onclick = () => turbAct("/api/turbidity/point/delete", { index: +b.dataset.tbdel }, "Point deleted")));
  $("tbTempco").onclick = async () => {
    $("tbCoefMsg").innerHTML = '<span class="spin"></span>';
    try {
      const r = await api("/api/turbidity/tempco");
      $("tbCoef").value = r.coeff.toFixed(4);
      $("tbCoefMsg").textContent = `Suggested ${r.coeff.toFixed(4)} /°C from ${r.n_sessions} sessions (temperature span ${r.t_span} °C, R² ${r.r2}) · ${r.note} · not saved yet`;
    } catch (e) { $("tbCoefMsg").textContent = e.message; }
  };
  $("tbSave").onclick = async () => {
    try {
      const cfg = await api("/api/config", { turbidity: { curve: $("tbCurve").value, temp_coeff: +$("tbCoef").value, ntu_display: $("tbOn").checked } });
      S.cfg = cfg; $("tbMsg").textContent = "Saved ✓"; toast("Saved"); loadTurb();
    } catch (e) { $("tbMsg").textContent = e.message; }
  };
  drawTurbCurve();
}

async function turbAct(path, body, ok) {
  try { S.turb = await api(path, body); toast(ok); S.cfg = await api("/api/config"); renderTurbSettings(); }
  catch (e) { toast(e.message, 4500); }
}

function turbLiveText() {
  const info = S.turb, c = info.cal, L = (S.st && S.st.latest) || {};
  const t = L.turb;
  if (!c.v_clear) return "No clear-water reference yet, so voltage (V) is shown · do step 1 first";
  if (!t || !t.quality) return "Waiting for live sensor data…";
  const q = { ok: "", below_limit: " (below what the sensor can resolve)", over_range: " (cloudier than the curve covers)", error: " (curve problem)", off: " (NTU display off)" }[t.quality] || "";
  return `Now: <b>${t.text ? esc(t.text) : "–"}</b>${q} · ${fmt(L.turb_v, 3)} V · r = ${t.r == null ? "–" : fmt(t.r, 3)}`;
}

function updateTurbLive() {
  if (!S.turb || !$("tbLive")) return;
  $("tbLive").innerHTML = turbLiveText();
  const ch = S.charts.cTurbCurve, L = S.st && S.st.latest;
  if (ch && L && L.turb && L.turb.ntu != null && ch.data.datasets[2]) {
    ch.data.datasets[2].data = [{ x: L.turb_v, y: L.turb.ntu }];
    ch.update("none");
  }
}

function drawTurbCurve() {
  const info = S.turb, pv = info.preview, c = info.cal;
  if (!pv || !$("cTurbCurve")) return;
  const L = S.st && S.st.latest;
  const live = L && L.turb && L.turb.ntu != null ? [{ x: L.turb_v, y: L.turb.ntu }] : [];
  mkChart("cTurbCurve", {
    type: "scatter",
    data: { datasets: [
      { label: "Conversion curve", data: pv.x.map((x, i) => ({ x, y: pv.ntu[i] })), showLine: true, borderColor: css("--accent"), borderWidth: 2, pointRadius: 0, tension: .2 },
      { label: "Your standard points", data: (c.points || []).map((p) => ({ x: p[1], y: p[0] })), backgroundColor: "#f59e0b", pointRadius: 5 },
      { label: "Now", data: live, backgroundColor: "#ef4444", pointRadius: 6 },
    ] },
    options: { plugins: { legend: { position: "bottom" } },
      scales: { x: { title: { display: true, text: pv.x_unit === "V" ? "Voltage (V)" : "V / V_clear" } }, y: { min: 0, title: { display: true, text: "NTU" } } } },
  });
}

/* ------------------------------------------------------------------ WP6: likely cause card (experimental) */
function renderLikely(st) {
  const box = $("likelyCard"), lk = st.likely;
  const recent = lk && (lk.active || Date.now() / 1000 - lk.time < 1800);
  if (!recent) { box.hidden = true; return; }
  box.hidden = false;
  const when = lk.active ? "Alarm active" : `Last alarm at ${new Date(lk.time * 1000).toLocaleTimeString("en-GB", { hour12: false })} (ended)`;
  const cands = (lk.candidates || []).map((c) => {
    const w = Math.max(0, Math.min(1, c.similarity)) * 100, ok = c.similarity >= 0.8;
    return `<div class="cand"><div class="cname">${esc(c.name)} <span class="tag" style="background:var(--idle-bg);color:var(--muted)">${c.source === "learned" ? "From your data" : "General rules"}</span></div>
      <div class="cbar"><i style="width:${w}%;background:${ok ? "var(--ok)" : "var(--muted)"}"></i></div><div class="cval">${fmt(c.similarity, 2)}</div></div>`;
  }).join("");
  const dr = (lk.drivers || []).map((d) => `${esc(d.text)} (z ${d.z > 0 ? "+" : ""}${d.z})`).join(" · ");
  box.innerHTML = `
    <div class="row"><h2>🔬 Likely cause</h2><span class="tag" style="background:var(--warn-bg);color:var(--warn)">Experimental</span><span class="spacer"></span><span class="hint" style="margin:0">${esc(when)}</span></div>
    <div class="likely-head v-${esc(lk.verdict)}">${esc(lk.headline)}</div>
    ${dr ? `<p class="hint" style="margin:4px 0 8px">Biggest change: ${dr}</p>` : ""}
    ${cands ? `<div class="cands">${cands}</div><p class="legend-note">Bar = similarity of the "direction of change" (cosine, 1 = exactly the same direction) · below 0.80 counts as not matching anything known</p>` : ""}
    ${(lk.notes || []).map((n) => `<p class="hint" style="margin:4px 0">• ${esc(n)}</p>`).join("")}
    ${(lk.disambiguate || []).map((n) => `<div class="notice">💡 ${esc(n)}</div>`).join("")}
    <p class="legend-note" style="margin-top:8px"><b>Keep in mind:</b> "Consistent with…" means the sensors changed in a pattern similar to that substance. It does not confirm the substance and says nothing about whether the water is safe ·
      With 3 sensors, table salt and baking soda cannot be told apart (pH is needed)</p>`;
}

/* ------------------------------------------------------------------ WP6: confusion matrix of the cause model */
function causeModelHtml(meta) {
  const cm = meta.cause_model;
  if (!cm || !cm.eval) {
    return `<div class="card span-12"><div class="row"><h2>🔬 Cause model</h2><span class="tag" style="background:var(--warn-bg);color:var(--warn)">Experimental</span></div>
      <p class="hint">No labelled anomaly runs yet, so only "General rules" (direction of change from physics) can be used · record 2 or more anomaly runs per substance and retrain; the system will learn their patterns</p></div>`;
  }
  const ev = cm.eval, cols = ev.columns, fams = ev.families;
  const acc = ev.accuracy == null ? "–" : pct(ev.accuracy, 0);
  const rule = ev.rule_tested ? `${ev.rule_correct}/${ev.rule_tested}` : "–";
  return `<div class="card span-12">
    <div class="row"><h2>🔬 Cause model (experimental)</h2><span class="tag" style="background:var(--warn-bg);color:var(--warn)">Experimental</span></div>
    <p class="hint">Leave-one-anomaly-run-out test: hide that run, build the model from the others, check the guess · rows = real substance, columns = system's guess (unknown = matches nothing known, ambiguous = cannot tell two apart)</p>
    <div class="kpis"><div class="tile"><div class="k">Correct (leave-one-run-out)</div><div class="v">${acc}<span class="u">${ev.tested ? `${ev.correct}/${ev.tested}` : ""}</span></div></div>
      <div class="tile"><div class="k">General rules match your labels</div><div class="v">${rule}</div></div>
      <div class="tile"><div class="k">Substances with one run only (cannot be tested)</div><div class="v">${ev.unseen}</div></div></div>
    <div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>Real ↓ / guessed →</th>${cols.map((c) => `<th class="num">${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${fams.map((f, i) => `<tr><td><b>${esc(f)}</b> <span class="hint" style="margin:0">(${cm.n_runs[f]} runs)</span></td>${ev.matrix[i].map((v, j) =>
        `<td class="num ${v && cols[j] === f ? "good" : (v ? "poor" : "")}">${v || "·"}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
    <p class="legend-note">Works only for substances you have recorded. A new substance shows as unknown (by design) ·
      ${meta.features.length < 4 ? "Currently using " + meta.features.length + " sensors: table salt and baking soda cannot be separated (both only raise TDS). Seeing ambiguous between them is correct behaviour · " : ""}
      These numbers come from a small amount of data and are not a guarantee of accuracy</p></div>`;
}
