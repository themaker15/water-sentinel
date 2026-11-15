"""HTTP API + web page (FastAPI). Open http://127.0.0.1:8000 ; API docs at /docs.

Security model
  * Same computer (127.0.0.1): full access, no login.
  * Other devices (phone via Tailscale/LAN): only when remote.enabled and a PIN is set; PIN login sets a cookie.
  * POSTs must be JSON (blocks other websites from driving the app through your browser).
  * Host header is checked (blocks DNS-rebinding).
  * /api/ingest (ESP32 over WiFi) needs the X-Token header instead of the PIN.
"""
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
from .sources import list_ports
from .training import TrainError

LOCAL_CLIENTS = {"127.0.0.1", "::1", "testclient"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}

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


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def create_app(hub):
    app = FastAPI(title="Water Sentinel", version=__version__)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        remote = hub.cfg["remote"]
        path = request.url.path
        host = (request.headers.get("host") or "").lower()
        host = host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
        client = request.client.host if request.client else ""
        local = client in LOCAL_CLIENTS
        host_ok = host in LOCAL_HOSTS or (remote["enabled"] and (
            _is_ip(host) or host.endswith(".ts.net") or host in remote.get("allowed_hosts", [])))
        if not host_ok and path != "/api/ingest":
            return PlainTextResponse("forbidden host", 403)
        if not local and path != "/api/ingest":
            if not (remote["enabled"] and remote["pin"]):
                return PlainTextResponse("remote access is off", 403)
            authed = hmac.compare_digest(request.cookies.get("ws_auth", ""), _token(remote["pin"]))
            if not authed and path != "/login":
                if path.startswith("/api/"):
                    return JSONResponse({"ok": False, "error": "ต้องเข้าสู่ระบบ"}, 401)
                return RedirectResponse("/login", 303)
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
        pin = parse_qs((await request.body()).decode())
        pin = (pin.get("pin") or [""])[0]
        real = hub.cfg["remote"]["pin"]
        if real and hmac.compare_digest(pin, real):
            resp = RedirectResponse("/", 303)
            resp.set_cookie("ws_auth", _token(real), max_age=30 * 86400, httponly=True, samesite="strict")
            return resp
        time.sleep(1.0)   # slow down guessing
        return HTMLResponse(LOGIN_PAGE.replace("{err}", "<p>PIN ไม่ถูกต้อง</p>"), 401)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ---------------------------------------------------------------- live
    @app.get("/api/state")
    def state(since: float = 0.0):
        return hub.state(since)

    @app.post("/api/monitor")
    def monitor(payload: dict = Body(...)):
        hub.set_monitor(bool(payload.get("on")))
        return {"ok": True}

    @app.post("/api/model")
    def set_model(payload: dict = Body(...)):
        hub.set_active_model(str(payload.get("key")))
        return {"ok": True}

    @app.post("/api/sim")
    def sim(payload: dict = Body(...)):
        hub.sim_scenario(str(payload.get("scenario")))
        return {"ok": True}

    # ---------------------------------------------------------------- data
    @app.get("/api/runs")
    def runs():
        return {"runs": hub.runs(), "next_run": hub.next_run()}

    @app.post("/api/record/start")
    def record_start(payload: dict = Body(...)):
        hub.start_record(payload.get("run"), payload.get("label"), payload.get("minutes", 5))
        return {"ok": True}

    @app.post("/api/record/stop")
    def record_stop():
        hub.stop_record()
        return {"ok": True}

    # ---------------------------------------------------------------- model
    @app.post("/api/train")
    def do_train(payload: dict = Body(default={})):
        return hub.train(bool(payload.get("use_ph", hub.cfg["use_ph"])))

    @app.get("/api/report")
    def report():
        p = MODEL_DIR / "report.json"
        if not p.exists():
            return JSONResponse({"ok": False, "error": "ยังไม่มีผลการเทรน"}, 404)
        return JSONResponse(json.loads(p.read_text(encoding="utf-8")))

    @app.get("/api/embedding")
    def embed(method: str = "pca"):
        if not (MODEL_DIR / "report.json").exists():
            raise ValueError("ยังไม่มีผลการเทรน")
        return hub.embedding(method)

    # ---------------------------------------------------------------- settings
    @app.get("/api/config")
    def get_config():
        return hub.public_config()

    @app.post("/api/config")
    def set_config(payload: dict = Body(...)):
        return hub.update_config(payload)

    @app.get("/api/ports")
    def ports():
        return {"ports": list_ports()}

    @app.post("/api/calibrate")
    def calibrate(payload: dict = Body(...)):
        return {"ok": True, "calib": hub.calibrate(payload.get("action"), payload.get("value"))}

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
