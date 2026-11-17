"use strict";
/* WP5 (turbidity in NTU: tile text + calibration card) and WP6 (experimental likely cause).
   Loaded BEFORE app.js: it only defines functions; they use app.js globals ($, api, S, fmt, ...) at call time. */

/* ------------------------------------------------------------------ WP5: tile text */
function turbDisplay(L) {
  const t = L && L.turb, volts = L && L.turb_v;
  if (t && t.text && ["ok", "below_limit", "over_range"].includes(t.quality)) {
    const cal = t.estimate ? "ประมาณ (ยังไม่ได้คาลิเบรตกับเครื่องวัดจริง)" : "คาลิเบรตด้วยสารมาตรฐานของคุณ";
    return { val: t.text.replace(/\s*NTU$/, ""), unit: "NTU", sub: `${fmt(volts, 3)} V · ${cal}`,
      title: `แรงดันดิบ ${fmt(volts, 3)} V${t.limit ? ` · ต่ำสุดที่เชื่อถือได้ ~${t.limit} NTU` : ""}` };
  }
  const why = { no_reference: "ยังไม่ได้ตั้งน้ำใสอ้างอิง (ตั้งค่า → ความขุ่น)", error: "กราฟแปลงค่ามีปัญหา ดูหน้าตั้งค่า", off: "" }[t && t.quality] || "";
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
    <p class="hint">เซนเซอร์ให้ <b>แรงดัน</b> ไม่ใช่ NTU จริง ระบบจึงแปลงเป็น NTU เพื่อให้อ่านง่ายเท่านั้น (โมเดลตรวจจับยังใช้แรงดันดิบ) ·
      ตัวเลขที่ไม่ได้คาลิเบรตเป็นค่าประมาณ ใช้เทียบ "ขุ่นขึ้น/ใสขึ้น" ได้ แต่ไม่ใช่ค่ามาตรฐาน</p>
    <div class="notice ${noRef ? "warn" : ""}" id="tbLive">${turbLiveText()}</div>
    <h3>1) น้ำใสอ้างอิง</h3>
    <div class="row"><button class="btn ghost sm" id="tbRef">ตั้งเป็นน้ำใส (มัธยฐาน 30 วินาทีล่าสุด)</button>
      <span class="hint" style="margin:0">${noRef ? "จุ่มหัววัดในน้ำใส/น้ำกลั่นให้นิ่งก่อนกด" :
        `อ้างอิง ${fmt(c.v_clear, 3)} V${c.t_clear != null ? ` ที่ ${fmt(c.t_clear, 1)} °C` : ""} · noise ±${fmt(c.noise_v, 4)} V`}</span></div>
    <h3>2) กราฟแปลงค่า</h3>
    <select id="tbCurve">${Object.entries(info.curves).map(([k, t]) => `<option value="${k}" ${c.curve === k ? "selected" : ""} ${k === "user_points" && !pts.length ? "disabled" : ""}>${esc(t)}</option>`).join("")}</select>
    <h3>3) สารมาตรฐานของคุณ (ไม่บังคับ)</h3>
    <p class="hint">มีเครื่องวัดความขุ่นหรือสารมาตรฐาน (เช่น formazin 100 NTU)? จุ่มหัววัดแล้วใส่ค่า NTU ที่อ่านได้ → กด "อ่านตอนนี้" ·
      ใส่หลายจุดยิ่งดี (เริ่มจากน้ำใส 0 NTU) เมื่อมีจุดแล้วตัวเลขจะไม่ถูกเรียกว่า "ประมาณ"</p>
    <div class="row"><input type="number" id="tbNtu" min="0" max="5000" step="any" placeholder="NTU ที่อ่านได้" style="width:150px">
      <button class="btn ghost sm" id="tbAdd" ${noRef ? "disabled" : ""}>อ่านตอนนี้เป็นจุดมาตรฐาน</button></div>
    ${pts.length ? `<div class="table-wrap" style="margin-top:8px;max-height:200px"><table><thead><tr><th class="num">NTU</th><th class="num">แรงดัน (V)</th><th class="num">°C</th><th></th></tr></thead>
      <tbody>${pts.map(({ p, i }) => `<tr><td class="num">${fmt(p[0], p[0] < 100 ? 1 : 0)}</td><td class="num">${fmt(p[1], 3)}</td><td class="num">${p[2] == null ? "–" : fmt(p[2], 1)}</td>
        <td><button class="btn ghost sm" data-tbdel="${i}">ลบจุด</button></td></tr>`).join("")}</tbody></table></div>` : ""}
    <h3>4) ชดเชยอุณหภูมิ</h3>
    <div class="row"><input type="number" id="tbCoef" step="0.0001" min="-0.02" max="0" value="${c.temp_coeff}" style="width:130px"><span class="hint" style="margin:0">ต่อ °C (ค่าเริ่มต้น −0.0022)</span>
      <button class="btn ghost sm" id="tbTempco">แนะนำจากข้อมูลของฉัน</button></div>
    <p class="hint" id="tbCoefMsg"></p>
    <div class="row" style="margin-top:6px"><label class="switch" style="font-weight:600"><input type="checkbox" id="tbOn" ${c.ntu_display ? "checked" : ""}><span class="track"></span><span>แสดงความขุ่นเป็น NTU</span></label>
      <button class="btn sm" id="tbSave">บันทึก</button><span class="hint" style="margin:0" id="tbMsg"></span></div>
    <div class="chart-box" style="height:220px;margin-top:12px"><canvas id="cTurbCurve"></canvas></div>
    <p class="legend-note">เส้น = แรงดันที่อ่านได้ → NTU ที่แปลงให้ · จุดเหลือง = จุดมาตรฐานของคุณ · จุดแดง = ค่าตอนนี้ ·
      ${info.limit ? `ต่ำกว่า ~${info.limit} NTU จะแสดงเป็น "&lt; ${info.limit} NTU" (noise ของเซนเซอร์ทำให้อ่านละเอียดกว่านี้ไม่ได้)` : "ตั้งน้ำใสอ้างอิงเพื่อดูค่าต่ำสุดที่เชื่อถือได้"}</p>`;
  $("tbRef").onclick = () => turbAct("/api/turbidity/reference", {}, "ตั้งน้ำใสอ้างอิงแล้ว");
  $("tbAdd").onclick = () => {
    const v = $("tbNtu").value;
    if (v === "") { toast("ใส่ค่า NTU ก่อน"); return; }
    turbAct("/api/turbidity/point", { ntu: +v }, "เพิ่มจุดมาตรฐานแล้ว");
  };
  document.querySelectorAll("[data-tbdel]").forEach((b) => (b.onclick = () => turbAct("/api/turbidity/point/delete", { index: +b.dataset.tbdel }, "ลบจุดแล้ว")));
  $("tbTempco").onclick = async () => {
    $("tbCoefMsg").innerHTML = '<span class="spin"></span>';
    try {
      const r = await api("/api/turbidity/tempco");
      $("tbCoef").value = r.coeff.toFixed(4);
      $("tbCoefMsg").textContent = `แนะนำ ${r.coeff.toFixed(4)} /°C จาก ${r.n_sessions} session (อุณหภูมิต่างกัน ${r.t_span} °C, R² ${r.r2}) · ${r.note} · ยังไม่ได้บันทึก`;
    } catch (e) { $("tbCoefMsg").textContent = e.message; }
  };
  $("tbSave").onclick = async () => {
    try {
      const cfg = await api("/api/config", { turbidity: { curve: $("tbCurve").value, temp_coeff: +$("tbCoef").value, ntu_display: $("tbOn").checked } });
      S.cfg = cfg; $("tbMsg").textContent = "บันทึกแล้ว ✓"; toast("บันทึกแล้ว"); loadTurb();
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
  if (!c.v_clear) return "ยังไม่ได้ตั้งน้ำใสอ้างอิง จึงแสดงเป็นแรงดัน (V) · ทำข้อ 1 ก่อน";
  if (!t || !t.quality) return "รอข้อมูลสดจากเซนเซอร์...";
  const q = { ok: "", below_limit: " (ต่ำกว่าที่เซนเซอร์แยกได้)", over_range: " (ขุ่นเกินช่วงของกราฟ)", error: " (กราฟมีปัญหา)", off: " (ปิดการแสดง NTU)" }[t.quality] || "";
  return `ตอนนี้: <b>${t.text ? esc(t.text) : "–"}</b>${q} · ${fmt(L.turb_v, 3)} V · r = ${t.r == null ? "–" : fmt(t.r, 3)}`;
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
      { label: "กราฟแปลงค่า", data: pv.x.map((x, i) => ({ x, y: pv.ntu[i] })), showLine: true, borderColor: css("--accent"), borderWidth: 2, pointRadius: 0, tension: .2 },
      { label: "จุดมาตรฐานของคุณ", data: (c.points || []).map((p) => ({ x: p[1], y: p[0] })), backgroundColor: "#f59e0b", pointRadius: 5 },
      { label: "ตอนนี้", data: live, backgroundColor: "#ef4444", pointRadius: 6 },
    ] },
    options: { plugins: { legend: { position: "bottom" } },
      scales: { x: { title: { display: true, text: pv.x_unit === "V" ? "แรงดัน (V)" : "V / V_clear" } }, y: { min: 0, title: { display: true, text: "NTU" } } } },
  });
}

/* ------------------------------------------------------------------ WP6: likely cause card (experimental) */
function renderLikely(st) {
  const box = $("likelyCard"), lk = st.likely;
  const recent = lk && (lk.active || Date.now() / 1000 - lk.time < 1800);
  if (!recent) { box.hidden = true; return; }
  box.hidden = false;
  const when = lk.active ? "กำลังเตือนอยู่" : `เตือนล่าสุดเมื่อ ${new Date(lk.time * 1000).toLocaleTimeString("th-TH", { hour12: false })} (จบแล้ว)`;
  const cands = (lk.candidates || []).map((c) => {
    const w = Math.max(0, Math.min(1, c.similarity)) * 100, ok = c.similarity >= 0.8;
    return `<div class="cand"><div class="cname">${esc(c.name)} <span class="tag" style="background:var(--idle-bg);color:var(--muted)">${c.source === "learned" ? "จากข้อมูลของคุณ" : "กฎทั่วไป"}</span></div>
      <div class="cbar"><i style="width:${w}%;background:${ok ? "var(--ok)" : "var(--muted)"}"></i></div><div class="cval">${fmt(c.similarity, 2)}</div></div>`;
  }).join("");
  const dr = (lk.drivers || []).map((d) => `${esc(d.text)} (z ${d.z > 0 ? "+" : ""}${d.z})`).join(" · ");
  box.innerHTML = `
    <div class="row"><h2>🔬 สาเหตุที่เป็นไปได้</h2><span class="tag" style="background:var(--warn-bg);color:var(--warn)">ทดลอง</span><span class="spacer"></span><span class="hint" style="margin:0">${esc(when)}</span></div>
    <div class="likely-head v-${esc(lk.verdict)}">${esc(lk.headline)}</div>
    ${dr ? `<p class="hint" style="margin:4px 0 8px">ที่เปลี่ยนมากที่สุด: ${dr}</p>` : ""}
    ${cands ? `<div class="cands">${cands}</div><p class="legend-note">แถบ = ความคล้ายของ "ทิศทางการเปลี่ยน" (cosine, 1 = ทิศเดียวกันเป๊ะ) · ต่ำกว่า 0.80 ถือว่าไม่ตรงกับสิ่งที่รู้จัก</p>` : ""}
    ${(lk.notes || []).map((n) => `<p class="hint" style="margin:4px 0">• ${esc(n)}</p>`).join("")}
    ${(lk.disambiguate || []).map((n) => `<div class="notice">💡 ${esc(n)}</div>`).join("")}
    <p class="legend-note" style="margin-top:8px"><b>ข้อควรจำ:</b> "สอดคล้องกับ…" หมายถึงรูปแบบการเปลี่ยนของเซนเซอร์คล้ายสารชนิดนั้น ไม่ใช่การยืนยันว่าเป็นสารอะไร และไม่ได้บอกว่าน้ำปลอดภัยหรือไม่ ·
      เซนเซอร์ 3 ตัวแยกเกลือธรรมดากับเบกกิ้งโซดาไม่ออก (ต้องมี pH)</p>`;
}

/* ------------------------------------------------------------------ WP6: confusion matrix of the cause model */
function causeModelHtml(meta) {
  const cm = meta.cause_model;
  if (!cm || !cm.eval) {
    return `<div class="card span-12"><div class="row"><h2>🔬 โมเดลสาเหตุ</h2><span class="tag" style="background:var(--warn-bg);color:var(--warn)">ทดลอง</span></div>
      <p class="hint">ยังไม่มีรอบ anomaly ที่ตั้งชื่อ จึงใช้ได้เฉพาะ "กฎทั่วไป" (ทิศทางการเปลี่ยนตามหลักฟิสิกส์) · เก็บรอบ anomaly ของแต่ละสาร 2 รอบขึ้นไป แล้วเทรนใหม่ ระบบจะเรียนรู้รูปแบบของสารเหล่านั้นเอง</p></div>`;
  }
  const ev = cm.eval, cols = ev.columns, fams = ev.families;
  const acc = ev.accuracy == null ? "–" : pct(ev.accuracy, 0);
  const rule = ev.rule_tested ? `${ev.rule_correct}/${ev.rule_tested}` : "–";
  return `<div class="card span-12">
    <div class="row"><h2>🔬 โมเดลสาเหตุ (ทดลอง)</h2><span class="tag" style="background:var(--warn-bg);color:var(--warn)">ทดลอง</span></div>
    <p class="hint">ทดสอบแบบเว้นทีละรอบ anomaly: ซ่อนรอบนั้น สร้างโมเดลจากรอบอื่น แล้วดูว่าทายถูกไหม · แถว = สารจริง, คอลัมน์ = สิ่งที่ระบบทาย (unknown = ไม่ตรงกับสิ่งที่รู้จัก, ambiguous = แยกสองอย่างไม่ออก)</p>
    <div class="kpis"><div class="tile"><div class="k">ทายถูก (leave-one-run-out)</div><div class="v">${acc}<span class="u">${ev.tested ? `${ev.correct}/${ev.tested}` : ""}</span></div></div>
      <div class="tile"><div class="k">กฎทั่วไปตรงกับชื่อที่คุณตั้ง</div><div class="v">${rule}</div></div>
      <div class="tile"><div class="k">สารที่มีรอบเดียว (ทดสอบไม่ได้)</div><div class="v">${ev.unseen}</div></div></div>
    <div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>สารจริง ↓ / ระบบทาย →</th>${cols.map((c) => `<th class="num">${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${fams.map((f, i) => `<tr><td><b>${esc(f)}</b> <span class="hint" style="margin:0">(${cm.n_runs[f]} รอบ)</span></td>${ev.matrix[i].map((v, j) =>
        `<td class="num ${v && cols[j] === f ? "good" : (v ? "poor" : "")}">${v || "·"}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
    <p class="legend-note">ใช้ได้กับสารที่คุณเก็บข้อมูลไว้เท่านั้น สารใหม่ที่ไม่เคยเก็บจะขึ้น unknown (ตั้งใจให้เป็นอย่างนั้น) ·
      ${meta.features.length < 4 ? "ตอนนี้ใช้เซนเซอร์ " + meta.features.length + " ตัว: เกลือธรรมดากับเบกกิ้งโซดาแยกกันไม่ได้ (ทั้งคู่แค่ทำให้ TDS สูงขึ้น) ถ้าเห็น ambiguous ระหว่างสองอย่างนี้คือพฤติกรรมที่ถูกต้อง · " : ""}
      ตัวเลขนี้เป็นการประเมินจากข้อมูลจำนวนน้อย ไม่ใช่การรับรองความแม่นยำ</p></div>`;
}
