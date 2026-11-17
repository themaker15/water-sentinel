"""HTTP API + web page (FastAPI). Open http://127.0.0.1:8000 ; API docs at /docs.

Security model
  * Same computer (127.0.0.1, Host localhost, no proxy headers): full access, no login.
  * Anything that came through a proxy or tunnel (Cloudflare, Tailscale Funnel, ...) is NOT local, even though
    the tunnel connects from 127.0.0.1. It is judged by the rules below.
  * Admin from another device: only when remote.enabled (or public_view) and a PIN is set; PIN login sets a cookie.
    Wrong PINs are slowed down and locked out after repeated failures.
  * Viewer: when remote.public_view is on, visitors without the PIN may read (GET) but every change is refused
    server-side, secrets are removed from the config, and nothing expensive can be started.
  * POSTs must be JSON (blocks other websites from driving the app through your browser).
  * Host header is checked (blocks DNS-rebinding).
  * /api/ingest (ESP32 over WiFi) needs the X-Token header instead of the PIN.
"""
import asyncio
import copy
import hashlib
import hmac
import ipaddress
import json
import time
from urllib.parse import parse_qs

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import MODEL_DIR, WEB_DIR
from .datasets import dataset_selection
from .share import Tunnel
from .sources import list_ports
from .training import TrainError

LOCAL_CLIENTS = {"127.0.0.1", "::1", "testclient"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
# a tunnel connects from 127.0.0.1 but adds one of these: such a request is never "this computer"
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip", "cf-ray",
                 "tailscale-funnel-request", "x-forwarded-host", "true-client-ip")
PUBLIC_SUFFIXES = (".trycloudflare.com", ".ts.net")
# GETs a viewer may not use (they are not needed to look, or they reveal / compute too much)
VIEWER_BLOCKED = ("/docs", "/redoc", "/openapi.json", "/api/ports", "/api/tune/suggest_floors",
                  "/api/turbidity/tempco", "/api/share")
VIEWER_MSG = "โหมดผู้ชม: ดูได้อย่างเดียว (เข้าสู่ระบบด้วย PIN เพื่อแก้ไข)"
LOCK_AFTER, LOCK_WINDOW_S = 8, 900
VIEWER_SECRET_KEYS = (("remote", "pin"), ("remote", "allowed_hosts"), ("ingest_token",), ("port",),
                      ("alerts", "ntfy_topic"), ("alerts", "ntfy_server"), ("alerts", "telegram_token"),
                      ("alerts", "telegram_chat_id"), ("alerts", "line_token"), ("alerts", "line_user_id"))


def _host_of(request):
    host = (request.headers.get("host") or "").lower()
    return host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]


def _client_key(request):
    """Who is guessing PINs: the visitor's address from the proxy header, else the socket address."""
    for h in ("cf-connecting-ip", "x-real-ip"):
        if request.headers.get(h):
            return request.headers[h].strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def viewer_config(cfg):
    cfg = copy.deepcopy(cfg)
    for path in VIEWER_SECRET_KEYS:
        d = cfg
        for k in path[:-1]:
            d = d.get(k, {})
        if path[-1] in d:
            d[path[-1]] = [] if isinstance(d[path[-1]], list) else ""
    return cfg

LOGIN_PAGE = """<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Water Sentinel</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1220;color:#e2e8f0;
font-family:system-ui,sans-serif}form{background:#111a2e;padding:28px;border-radius:16px;width:min(320px,90vw)}
input,button{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;font-size:18px;margin-top:12px}
input{border:1px solid #334155;background:#0b1220;color:#e2e8f0;letter-spacing:4px;text-align:center}
button{border:0;background:#38bdf8;color:#082f49;font-weight:700}p{color:#f87171}</style></head>
<body><form method="post" action="/login"><h2>💧 Water Sentinel</h2><div>ใส่ PIN เพื่อดูจากระยะไกล</div>
<input name="pin" type="password" inputmode="numeric" autofocus><button>เข้าสู่ระบบ</button>{err}</form></body></html>"""


def _token(pin):
    return hashlib.sha256(f"water-sentinel:{pin}".encode()).hexdigest()


def selection_from(payload):
    """Training selection from an API body: {"dataset": name} | {"files"/"roles", "sensors", "models"} | none = default."""
    payload = payload or {}
    if isinstance(payload.get("selection"), dict):
        payload = payload["selection"]
    files = payload.get("files", payload.get("roles"))
    if payload.get("dataset") and files is None:
        return dataset_selection(str(payload["dataset"]))
    if files is None and payload.get("sensors") is None and payload.get("models") is None:
        return None
    return {"dataset": payload.get("dataset"), "files": files, "sensors": payload.get("sensors"),
            "models": payload.get("models")}


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def create_app(hub):
    app = FastAPI(title="Water Sentinel", version=__version__)

    tunnel = Tunnel()
    app.state.tunnel = tunnel
    failures = {}                     # client -> [timestamps of wrong PINs]

    def public_host(host, remote):
        url_host = (remote.get("public_url") or "").lower().split("://")[-1].split("/")[0].split(":")[0]
        return host.endswith(PUBLIC_SUFFIXES) or (url_host and host == url_host)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        remote = hub.cfg["remote"]
        path = request.url.path
        host = _host_of(request)
        client = request.client.host if request.client else ""
        proxied = any(h in request.headers for h in PROXY_HEADERS)
        local = client in LOCAL_CLIENTS and host in LOCAL_HOSTS and not proxied
        public = bool(remote.get("public_view"))
        host_ok = host in LOCAL_HOSTS or host in remote.get("allowed_hosts", []) \
            or (remote["enabled"] and (_is_ip(host) or host.endswith(".ts.net"))) \
            or (public and public_host(host, remote))
        if not host_ok and path != "/api/ingest":
            return PlainTextResponse("forbidden host", 403)
        role = "admin"
        if not local and path != "/api/ingest":
            pin = remote["pin"]
            can_login = bool(pin) and (remote["enabled"] or public)
            authed = can_login and hmac.compare_digest(request.cookies.get("ws_auth", ""), _token(pin))
            if not authed:
                if path == "/login" and can_login:
                    role = "guest"
                elif public:
                    role = "viewer"
                elif not can_login:
                    return PlainTextResponse("remote access is off", 403)
                elif path.startswith("/api/"):
                    return JSONResponse({"ok": False, "error": "ต้องเข้าสู่ระบบ"}, 401)
                else:
                    return RedirectResponse("/login", 303)
        if role == "viewer":
            if request.method != "GET" and path != "/login":
                return JSONResponse({"ok": False, "error": VIEWER_MSG, "viewer": True}, 403)
            if path.startswith(VIEWER_BLOCKED):
                return JSONResponse({"ok": False, "error": VIEWER_MSG, "viewer": True}, 403)
        request.state.role = role
        if request.method == "POST" and path != "/login" and \
                not request.headers.get("content-type", "").startswith("application/json"):
            return PlainTextResponse("JSON only", 415)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def value_error(_req, exc):
        return JSONResponse({"ok": False, "error": str(exc)}, 400)

    @app.exception_handler(TrainError)
    async def train_error(_req, exc):
        return JSONResponse({"ok": False, "error": str(exc)}, 400)

    # ---------------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/login", include_in_schema=False)
    def login_page():
        return HTMLResponse(LOGIN_PAGE.replace("{err}", ""))

    @app.post("/login", include_in_schema=False)
    async def login(request: Request):
        key, now = _client_key(request), time.time()
        recent = [t for t in failures.get(key, []) if now - t < LOCK_WINDOW_S]
        if len(recent) >= LOCK_AFTER:
            return HTMLResponse(LOGIN_PAGE.replace("{err}", "<p>ใส่ PIN ผิดหลายครั้งเกินไป ลองใหม่ใน 15 นาที</p>"), 429)
        pin = parse_qs((await request.body()).decode())
        pin = (pin.get("pin") or [""])[0]
        real = hub.cfg["remote"]["pin"]
        if real and hmac.compare_digest(pin, real):
            failures.pop(key, None)
            resp = RedirectResponse("/", 303)
            https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            resp.set_cookie("ws_auth", _token(real), max_age=30 * 86400, httponly=True, samesite="strict", secure=https)
            return resp
        failures[key] = recent + [now]
        if len(failures) > 5000:
            failures.clear()
        await asyncio.sleep(1.0)   # slow down guessing without blocking everyone else
        return HTMLResponse(LOGIN_PAGE.replace("{err}", "<p>PIN ไม่ถูกต้อง</p>"), 401)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ---------------------------------------------------------------- live
    @app.get("/api/state")
    def state(request: Request, since: float = 0.0):
        st = dict(hub.state(since))
        st["role"] = getattr(request.state, "role", "admin")
        r = hub.cfg["remote"]
        st["share"] = {"public_view": bool(r.get("public_view")),
                       "url": tunnel.status()["url"] or r.get("public_url") or None}
        return st

    @app.post("/api/monitor")
    def monitor(payload: dict = Body(...)):
        hub.set_monitor(bool(payload.get("on")))
        return {"ok": True}

    @app.post("/api/model")
    def set_model(payload: dict = Body(...)):
        hub.set_active_model(str(payload.get("key")))
        return {"ok": True}

    @app.get("/api/snapshots")
    def snapshots():
        return {"ok": True, **hub.snapshots()}

    @app.post("/api/snapshots/use")
    def use_snapshot(payload: dict = Body(...)):
        return {"ok": True, **hub.use_snapshot(str(payload.get("id", "")))}

    @app.post("/api/sim")
    def sim(payload: dict = Body(...)):
        hub.sim_scenario(str(payload.get("scenario")))
        return {"ok": True}

    # ---------------------------------------------------------------- data
    @app.get("/api/runs")
    def runs():
        rs = hub.runs()
        return {"runs": rs, "next_run": hub.next_run(rs), "archived": hub.archived()}

    @app.get("/api/runs/export.csv")
    def runs_export(files: str = ""):
        text = hub.stats_csv([f for f in files.split(",") if f])
        return PlainTextResponse("\ufeff" + text, media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="run_stats.csv"'})

    @app.post("/api/run/meta")
    def run_meta(payload: dict = Body(...)):
        return {"ok": True, "meta": hub.update_run(str(payload.get("file", "")), payload)}

    @app.get("/api/run/{file}")
    def run_detail(file: str):
        return hub.run_detail(file)

    @app.post("/api/run/{file}/meta")
    def run_meta_file(file: str, payload: dict = Body(...)):
        return {"ok": True, "meta": hub.update_run(file, payload)}

    @app.post("/api/run/{file}/archive")
    def run_archive(file: str):
        return {"ok": True, "file": hub.archive_run(file)}

    @app.post("/api/run/{file}/restore")
    def run_restore(file: str):
        return {"ok": True, "file": hub.restore_run(file)}

    @app.post("/api/snapshot")
    def snapshot(payload: dict = Body(...)):
        return {"ok": True, "file": hub.save_last(payload.get("minutes", 5), payload.get("label") or "unlabeled")}

    @app.post("/api/record/start")
    def record_start(payload: dict = Body(...)):
        warnings = hub.start_record(payload.get("run"), payload.get("label"), payload.get("minutes", 5),
                                    payload.get("note", ""), bool(payload.get("new_water", False)))
        return {"ok": True, "warnings": warnings}

    @app.post("/api/record/mark")
    def record_mark(payload: dict = Body(...)):
        return {"ok": True, "marker": hub.mark(payload.get("text"), payload.get("kind", "note"), payload.get("dose"))}

    @app.post("/api/record/stop")
    def record_stop():
        hub.stop_record()
        return {"ok": True}

    # ---------------------------------------------------------------- model
    @app.post("/api/train")
    def do_train(payload: dict = Body(default={})):
        # no selection in the body = every usable run, exactly like v1.0
        return hub.train(bool(payload.get("use_ph", hub.cfg["use_ph"])), bool(payload.get("force", False)),
                         selection_from(payload))

    @app.post("/api/train/check")
    def train_check(payload: dict = Body(default={})):
        return {"ok": True, "check": hub.train_check(selection_from(payload))}

    @app.get("/api/datasets")
    def datasets():
        return {"ok": True, **hub.datasets()}

    @app.post("/api/datasets")
    def dataset_save(payload: dict = Body(...)):
        return {"ok": True, "dataset": hub.dataset_save(payload.get("name"), selection_from(payload), payload.get("note", ""))}

    @app.post("/api/datasets/delete")
    def dataset_delete(payload: dict = Body(...)):
        hub.dataset_delete(str(payload.get("name", "")))
        return {"ok": True}

    @app.get("/api/report")
    def report():
        p = MODEL_DIR / "report.json"
        if not p.exists():
            return JSONResponse({"ok": False, "error": "ยังไม่มีผลการเทรน"}, 404)
        return JSONResponse(json.loads(p.read_text(encoding="utf-8")))

    @app.get("/api/embedding")
    def embed(request: Request, method: str = "pca"):
        if not (MODEL_DIR / "report.json").exists():
            raise ValueError("ยังไม่มีผลการเทรน")
        if getattr(request.state, "role", "admin") == "viewer" and not (MODEL_DIR / f"embed_{method}.json").exists():
            raise ValueError("ยังไม่ได้คำนวณภาพนี้ (ผู้ดูแลต้องเปิดดูก่อนหนึ่งครั้ง)")
        return hub.embedding(method)

    # ---------------------------------------------------------------- tuning
    @app.post("/api/tune/preview")
    def tune_preview(payload: dict = Body(...)):
        return {"ok": True, **hub.tune_preview(str(payload.get("model", "mspc")), payload.get("settings") or {})}

    @app.get("/api/tune/suggest_floors")
    def tune_suggest():
        return {"ok": True, **hub.tune_suggest()}

    @app.get("/api/tune/meta")
    def tune_meta():
        from .tuning import PRESETS, load_history
        return {"ok": True, "presets": PRESETS, "history": load_history(MODEL_DIR)}

    @app.post("/api/tune/apply")
    def tune_apply(payload: dict = Body(...)):
        return hub.tune_apply(payload.get("settings") or {}, bool(payload.get("retrain", True)))

    @app.post("/api/tune/restore")
    def tune_restore(payload: dict = Body(...)):
        return hub.tune_restore(payload.get("index", -1))

    # ---------------------------------------------------------------- settings
    @app.get("/api/config")
    def get_config(request: Request):
        cfg = hub.public_config()
        return viewer_config(cfg) if getattr(request.state, "role", "admin") == "viewer" else cfg

    # ---------------------------------------------------------------- viewer link (presentations)
    @app.get("/api/share")
    def share_status():
        r = hub.cfg["remote"]
        return {"ok": True, **tunnel.status(), "public_view": bool(r.get("public_view")),
                "public_url": r.get("public_url", ""), "pin_set": bool(r.get("pin"))}

    @app.post("/api/share")
    def share_set(request: Request, payload: dict = Body(...)):
        """{"tunnel": true|false} starts/stops cloudflared (only from this computer);
        {"public_view": bool, "public_url": str} sets the viewer mode and a manual address (e.g. Tailscale Funnel)."""
        patch = {}
        if "public_url" in payload:
            url = str(payload["public_url"] or "").strip()
            if url and not url.startswith("https://"):
                raise ValueError("ที่อยู่สาธารณะต้องขึ้นต้นด้วย https://")
            patch["public_url"] = url[:200]
        if "public_view" in payload:
            patch["public_view"] = bool(payload["public_view"])
        if "tunnel" in payload:
            if request.client is None or request.client.host not in LOCAL_CLIENTS or \
                    any(h in request.headers for h in PROXY_HEADERS):
                raise ValueError("เริ่ม/หยุดลิงก์สาธารณะได้จากคอมเครื่องนี้เท่านั้น")
            if payload["tunnel"]:
                patch["public_view"] = True
            else:
                tunnel.stop()
        pin = hub.cfg["remote"].get("pin") or ""
        if patch.get("public_view") and pin and len(pin) < 6:
            raise ValueError("PIN สั้นเกินไปสำหรับลิงก์สาธารณะ: ตั้ง PIN อย่างน้อย 6 ตัวก่อน")
        if patch:
            hub.update_config({"remote": patch})
        if payload.get("tunnel"):
            tunnel.start(hub.cfg["http_port"])
        return share_status()



    @app.post("/api/config")
    def set_config(payload: dict = Body(...)):
        return hub.update_config(payload)

    @app.get("/api/ports")
    def ports():
        return {"ports": list_ports()}

    @app.post("/api/calibrate")
    def calibrate(payload: dict = Body(...)):
        return {"ok": True, "calib": hub.calibrate(payload.get("action"), payload.get("value"))}

    # ---------------------------------------------------------------- turbidity (NTU display)
    @app.get("/api/turbidity")
    def turb():
        return {"ok": True, **hub.turb_info()}

    @app.post("/api/turbidity/reference")
    def turb_reference():
        return {"ok": True, **hub.turb_set_clear()}

    @app.post("/api/turbidity/point")
    def turb_point(payload: dict = Body(...)):
        return {"ok": True, **hub.turb_add_point(payload.get("ntu"))}

    @app.post("/api/turbidity/point/delete")
    def turb_point_delete(payload: dict = Body(...)):
        return {"ok": True, **hub.turb_delete_point(payload.get("index", -1))}

    @app.get("/api/turbidity/tempco")
    def turb_tempco():
        return {"ok": True, **hub.turb_tempco()}

    @app.post("/api/alerts/test")
    def test_alerts():
        return {"ok": True, "results": hub.test_alerts()}

    # ---------------------------------------------------------------- ESP32 over WiFi (future)
    @app.post("/api/ingest")
    def ingest(request: Request, payload: dict = Body(...)):
        token = hub.cfg.get("ingest_token") or ""
        if not token or not hmac.compare_digest(request.headers.get("x-token", ""), token):
            return JSONResponse({"ok": False, "error": "bad token"}, 401)
        raw = {k: float(payload[k]) if payload.get(k) is not None else float("nan")
               for k in ("temp_c", "tds_v", "turb_v", "ph_pin_v")}
        raw["ms"] = float(payload.get("ms", time.time() * 1000))
        hub.on_sample(raw, "wifi")
        return {"ok": True}

    return app
