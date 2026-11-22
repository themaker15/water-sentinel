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
  if (!url) { toast("No public link yet: start one in Settings → Viewer link"); return; }
  const px = Math.max(220, Math.min(window.innerWidth - 64, window.innerHeight - 220, 560));
  $("qrBox").innerHTML = `<div class="qr-img">${qrSvg(url, px)}</div>
    <div class="qr-url">${esc(url)}</div>
    <p class="hint" style="margin:6px 0 0;text-align:center">Scan to watch live sensor values (read only)</p>`;
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
    <p class="hint">People who open the link see live values, charts, model results and recorded data, but <b>cannot change anything</b> (the server rejects every change)
      The admin can sign in with the PIN · this computer must keep the app open during the presentation</p>
    ${!s.pin_set ? `<div class="notice warn">No PIN set yet: viewers can watch, but you cannot control the app from your phone (set a PIN of at least 6 characters in the "Remote access")</div>` : ""}
    <h3>Cloudflare (no sign-up)</h3>
    ${s.installed ? "" : `<div class="notice">Install cloudflared once: open cmd and run <code>winget install --id Cloudflare.cloudflared</code> then restart this app</div>`}
    <div class="row" style="margin-top:6px">
      ${s.running ? `<button class="btn danger sm" id="shStop">Stop public link</button>` : `<button class="btn sm" id="shStart" ${s.installed ? "" : "disabled"}>Start public link</button>`}
      <span class="hint" style="margin:0">${starting ? '<span class="spin"></span>Creating the link (about 5–15 s)…' : s.running ? "Online · the link changes every time it restarts" : "Off"}</span></div>
    ${s.error ? `<div class="notice bad" style="margin-top:6px">${esc(s.error)}</div>` : ""}
    <details style="margin-top:10px"><summary>Use Tailscale Funnel instead (fixed link)</summary>
      <p class="hint">Run in cmd: <code>tailscale funnel --bg 8000</code> then paste the https://….ts.net address you get here</p>
      <div class="row"><input type="text" id="shUrl" placeholder="https://machine-name.ts.net" value="${esc(s.public_url || "")}" style="flex:1;min-width:200px">
        <button class="btn ghost sm" id="shUrlSave">Save</button></div></details>
    <label class="switch" style="font-weight:600;margin-top:12px"><input type="checkbox" id="shView" ${s.public_view ? "checked" : ""}><span class="track"></span>
      <span>Let viewers in without a PIN</span></label>
    ${url ? `<div class="share-url"><a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a></div>
      <div class="row"><button class="btn sm" id="shQr">Full-screen QR</button><button class="btn ghost sm" id="shCopy">Copy link</button></div>` : ""}`;
  const post = async (body, ok) => {
    try { S.share = await api("/api/share", body); if (ok) toast(ok); renderShare(); }
    catch (e) { toast(e.message, 5000); loadShare(); }
  };
  if ($("shStart")) $("shStart").onclick = () => { post({ tunnel: true }); pollShare(); };
  if ($("shStop")) $("shStop").onclick = () => post({ tunnel: false }, "Link stopped");
  $("shUrlSave").onclick = () => post({ public_url: $("shUrl").value.trim() }, "Saved");
  $("shView").onchange = () => post({ public_view: $("shView").checked });
  if ($("shQr")) $("shQr").onclick = () => showQr(url);
  if ($("shCopy")) $("shCopy").onclick = async () => {
    try { await navigator.clipboard.writeText(url); toast("Copied"); } catch (e) { toast(url, 6000); }
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
