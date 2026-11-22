"use strict";
/* Viewer link for presentations: role handling, share card, QR overlay. Loaded before app.js. */

const isViewer = () => S.role === "viewer";

/* called on every poll with the server's view of who we are */
function applyRole(st) {
  const role = st.role || "admin";
  if (role === S.role) { updateQrButton(st); return; }
  S.role = role;
  document.body.classList.toggle("viewer", role === "viewer");
  $("viewerBar").hidden = role !== "viewer";
  if (role === "viewer") {
    const t = document.querySelector("#tabs button.on");
    if (t && ["record", "settings"].includes(t.dataset.v)) showTab("live");
    $("monitorToggle").disabled = true;
  }
  updateQrButton(st);
}

function updateQrButton(st) {
  const url = st.share && st.share.url;
  S.shareUrl = url || null;
  $("btnQr").hidden = !url || isViewer();
}

/* ---------------------------------------------------------------- QR overlay */
function qrSvg(text, px) {
  const q = qrcode(0, "M");
  q.addData(text); q.make();
  const n = q.getModuleCount(), quiet = 4, size = n + quiet * 2;
  let d = "";
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (q.isDark(r, c)) d += `M${c + quiet},${r + quiet}h1v1h-1z`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${px}" height="${px}" shape-rendering="crispEdges" role="img" aria-label="QR code">
    <rect width="${size}" height="${size}" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
}

function showQr(url) {
  url = url || S.shareUrl;
  if (!url) { toast("ยังไม่มีลิงก์สาธารณะ: เริ่มที่หน้าตั้งค่า → ลิงก์ให้คนดู"); return; }
  const px = Math.max(220, Math.min(window.innerWidth - 64, window.innerHeight - 220, 560));
  $("qrBox").innerHTML = `<div class="qr-img">${qrSvg(url, px)}</div>
    <div class="qr-url">${esc(url)}</div>
    <p class="hint" style="margin:6px 0 0;text-align:center">สแกนเพื่อดูค่าสดจากเซนเซอร์ (ดูได้อย่างเดียว)</p>`;
  $("qrOverlay").hidden = false;
}

/* ---------------------------------------------------------------- settings card */
async function loadShare() {
  try { S.share = await api("/api/share"); renderShare(); }
  catch (e) { $("shareBox").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
}

function renderShare() {
  const s = S.share, url = s.url || s.public_url;
  const starting = s.running && !s.url;
  $("shareBox").innerHTML = `
    <p class="hint">คนที่เปิดลิงก์เห็นค่าสด กราฟ ผลโมเดล และข้อมูลที่บันทึก แต่ <b>แก้ไขอะไรไม่ได้</b> (เซิร์ฟเวอร์ปฏิเสธทุกคำสั่งแก้ไข)
      ผู้ดูแลเข้าสู่ระบบด้วย PIN ได้ · คอมเครื่องนี้ต้องเปิดโปรแกรมค้างไว้ระหว่างพรีเซนต์</p>
    ${!s.pin_set ? `<div class="notice warn">ยังไม่ได้ตั้ง PIN: คนดูเข้าได้ แต่คุณจะควบคุมจากมือถือไม่ได้ (ตั้ง PIN อย่างน้อย 6 ตัวในการ์ด "ดูจากมือถือ")</div>` : ""}
    <h3>Cloudflare (ไม่ต้องสมัคร)</h3>
    ${s.installed ? "" : `<div class="notice">ต้องติดตั้ง cloudflared ครั้งเดียว: เปิด cmd แล้วรัน <code>winget install --id Cloudflare.cloudflared</code> แล้วปิด-เปิดโปรแกรมนี้ใหม่</div>`}
    <div class="row" style="margin-top:6px">
      ${s.running ? `<button class="btn danger sm" id="shStop">หยุดลิงก์สาธารณะ</button>` : `<button class="btn sm" id="shStart" ${s.installed ? "" : "disabled"}>เริ่มลิงก์สาธารณะ</button>`}
      <span class="hint" style="margin:0">${starting ? '<span class="spin"></span>กำลังสร้างลิงก์ (ประมาณ 5-15 วินาที)...' : s.running ? "ออนไลน์อยู่ · ลิงก์จะเปลี่ยนทุกครั้งที่เริ่มใหม่" : "ปิดอยู่"}</span></div>
    ${s.error ? `<div class="notice bad" style="margin-top:6px">${esc(s.error)}</div>` : ""}
    <details style="margin-top:10px"><summary>ใช้ Tailscale Funnel แทน (ลิงก์คงที่)</summary>
      <p class="hint">รันใน cmd: <code>tailscale funnel --bg 8000</code> แล้วนำที่อยู่ https://….ts.net ที่ได้มาใส่ช่องนี้</p>
      <div class="row"><input type="text" id="shUrl" placeholder="https://ชื่อเครื่อง.ts.net" value="${esc(s.public_url || "")}" style="flex:1;min-width:200px">
        <button class="btn ghost sm" id="shUrlSave">บันทึก</button></div></details>
    <label class="switch" style="font-weight:600;margin-top:12px"><input type="checkbox" id="shView" ${s.public_view ? "checked" : ""}><span class="track"></span>
      <span>เปิดให้คนดูเข้าได้โดยไม่ต้องใช้ PIN</span></label>
    ${url ? `<div class="share-url"><a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a></div>
      <div class="row"><button class="btn sm" id="shQr">แสดง QR เต็มจอ</button><button class="btn ghost sm" id="shCopy">คัดลอกลิงก์</button></div>` : ""}`;
  const post = async (body, ok) => {
    try { S.share = await api("/api/share", body); if (ok) toast(ok); renderShare(); }
    catch (e) { toast(e.message, 5000); loadShare(); }
  };
  if ($("shStart")) $("shStart").onclick = () => { post({ tunnel: true }); pollShare(); };
  if ($("shStop")) $("shStop").onclick = () => post({ tunnel: false }, "หยุดลิงก์แล้ว");
  $("shUrlSave").onclick = () => post({ public_url: $("shUrl").value.trim() }, "บันทึกแล้ว");
  $("shView").onchange = () => post({ public_view: $("shView").checked });
  if ($("shQr")) $("shQr").onclick = () => showQr(url);
  if ($("shCopy")) $("shCopy").onclick = async () => {
    try { await navigator.clipboard.writeText(url); toast("คัดลอกแล้ว"); } catch (e) { toast(url, 6000); }
  };
}

/* the tunnel address appears a few seconds after start */
function pollShare(n = 0) {
  setTimeout(async () => {
    await loadShare();
    if (S.share && S.share.running && !S.share.url && n < 30) pollShare(n + 1);
  }, 1500);
}

function wireShare() {
  $("btnQr").onclick = () => showQr();
  $("qrOverlay").onclick = (e) => { if (e.target.id === "qrOverlay" || e.target.closest("#qrClose")) $("qrOverlay").hidden = true; };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("qrOverlay").hidden = true; });
}
