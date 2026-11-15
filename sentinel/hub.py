"""Central state of the running app. Framework-independent: the web server only calls Hub methods,
so the UI or transport can change (FastAPI today, MQTT/cloud later) without touching this logic."""
import copy
import csv
import math
import re
import threading
import time
from collections import deque

import numpy as np

from . import alerts
from .config import DATA_DIR, DEFAULT_CONFIG, LOG_DIR, MODEL_DIR, deep_merge, load_config, save_config
from .features import NAME_RE, derive_one, tds_ppm
from .live import LiveMonitor
from .models import MODEL_NAMES
from .sources import SourceThread, Simulator
from .training import TrainError, embedding, load_bundle, train

SECRET_KEYS = {("alerts", "telegram_token"), ("alerts", "line_token"), ("remote", "pin"), ("ingest_token",)}
MASK = "••••"


def clean(o):
    """NaN/inf -> None so the JSON is valid for the browser."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, deque)):
        return [clean(v) for v in o]
    if isinstance(o, np.generic):
        return clean(o.item())
    return o


def known_only(defaults, patch):
    """Keep only keys that exist in DEFAULT_CONFIG, so the API cannot write arbitrary settings."""
    out = {}
    for k, v in (patch or {}).items():
        if k not in defaults:
            continue
        out[k] = known_only(defaults[k], v) if isinstance(defaults[k], dict) and isinstance(v, dict) else v
    return out


class Hub:
    def __init__(self, sim=False, sim_speed=1.0):
        self.lock = threading.RLock()
        self.cfg = load_config()
        self.sim_forced = sim
        self.sim_speed = sim_speed
        self.gen = 0                       # bumped when the port changes -> source reconnects
        self.link = {"ok": False, "msg": "กำลังเริ่มระบบ...", "port": None}
        self.last_rx = 0.0
        self.latest = None
        self.history = deque(maxlen=900)   # ~15 min at 1 sample/s
        self.raw_recent = deque(maxlen=15)
        self.events = deque(maxlen=300)
        self.event_id = 0
        self.last_sent = {}
        self.rec = None
        self.rec_last = None
        self.det_out = {"status": "no_model"}
        self.prev_alarm = False
        self.last_fault = None
        self.training = False
        self._row_cache = {}
        self.bundle = None
        self.live = None
        self.load_model()
        self.source = SourceThread(self)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self.source.start()

    def shutdown(self):
        self.source.stopping = True
        with self.lock:
            if self.rec:
                self._finish_record("ปิดโปรแกรม")

    def port_pref(self):
        return "SIM" if self.sim_forced else self.cfg["port"]

    def set_link(self, ok, msg, port):
        with self.lock:
            self.link = {"ok": ok, "msg": msg, "port": port}

    def load_model(self):
        with self.lock:
            self.bundle = load_bundle()
            self.live = LiveMonitor(self.bundle, self.cfg["active_model"]) if self.bundle else None
            self.det_out = {"status": "off" if self.live else "no_model"}
            self.prev_alarm = False

    # ------------------------------------------------------------ samples
    def on_sample(self, raw, source):
        with self.lock:
            now = time.time()
            self.last_rx = now
            if not self.link["ok"] or "รอข้อมูล" in self.link["msg"] or "ไม่ส่งข้อมูล" in self.link["msg"]:
                port = self.link.get("port")
                self.link = {"ok": True, "port": port, "msg": {"sim": "โหมดจำลอง (ไม่ได้ใช้บอร์ดจริง)",
                                                              "wifi": "รับข้อมูลผ่าน WiFi"}.get(source, f"รับข้อมูลจาก {port}")}
            d = derive_one(raw, self.cfg["calib"], self.cfg["ph_divider"])
            self.latest = {**d, "time": now, "source": source}
            self.raw_recent.append(raw)
            self._record(raw)
            if self.live and self.cfg["monitor_on"]:
                out = self.live.update(raw)
            else:
                out = {"status": "off" if self.live else "no_model"}
            self._transitions(out)
            self.det_out = out
            self.history.append({"t": round(now, 2), "temp_c": d["temp_c"], "tds_ppm": d["tds_ppm"],
                                 "turb_v": d["turb_v"], "ph": d["ph"] if self.cfg["use_ph"] else None,
                                 "ratio": out.get("ratio"), "status": out["status"]})

    def _transitions(self, out):
        a = self.cfg["alerts"]
        if out["status"] == "fault":
            if out["fault"] != self.last_fault:
                self._event("fault", f"เซนเซอร์มีปัญหา: {out['fault_text']}", notify=a["notify_fault"])
            self.last_fault = out["fault"]
        else:
            self.last_fault = None
        alarm = bool(out.get("in_alarm"))
        if alarm and not self.prev_alarm:
            cause = ", ".join(c["text"] for c in out.get("cause") or []) or "ค่าหลายเซนเซอร์เปลี่ยนพร้อมกัน"
            self._event("anomaly", f"ตรวจพบน้ำผิดปกติ: {cause}", notify=True)
        elif self.prev_alarm and not alarm and out["status"] == "normal":
            self._event("recovered", "น้ำกลับสู่สภาวะปกติ", notify=a["notify_recovery"])
        self.prev_alarm = alarm

    def _event(self, kind, msg, notify=False):
        self.event_id += 1
        ev = {"id": self.event_id, "time": time.strftime("%H:%M:%S"), "date": time.strftime("%Y-%m-%d"),
              "kind": kind, "msg": msg, "sent": []}
        self.events.appendleft(ev)
        try:
            new = not (LOG_DIR / "events.csv").exists()
            with open(LOG_DIR / "events.csv", "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["date", "time", "kind", "message"])
                w.writerow([ev["date"], ev["time"], kind, msg])
        except OSError:
            pass
        a = self.cfg["alerts"]
        if notify and alerts.configured(a):
            now = time.time()
            if now - self.last_sent.get(kind, 0) >= a["cooldown_s"]:
                self.last_sent[kind] = now
                title = {"anomaly": "⚠️ Water Sentinel: น้ำผิดปกติ", "recovered": "✅ Water Sentinel: กลับปกติ",
                         "fault": "🔧 Water Sentinel: เซนเซอร์มีปัญหา"}.get(kind, "Water Sentinel")
                threading.Thread(target=self._send, args=(ev, title, dict(a), kind != "recovered"),
                                 daemon=True).start()
        return ev

    def _send(self, ev, title, a, urgent):
        res = alerts.send_all(a, title, f"{ev['msg']} ({ev['time']})", urgent)
        with self.lock:
            ev["sent"] = res

    # ------------------------------------------------------------ monitor / model
    def set_monitor(self, on):
        with self.lock:
            if on and not self.live:
                raise ValueError("ยังไม่มีโมเดล: เก็บข้อมูลน้ำปกติแล้วกดเทรนก่อน")
            self.cfg["monitor_on"] = bool(on)
            save_config(self.cfg)
            if self.live:
                self.live.reset()
            self.prev_alarm = False
            self.det_out = {"status": "starting" if on else "off", "left": self.cfg["detector"]["median_win"]}
            self._event("info", "เปิดโหมดเฝ้าระวัง" if on else "ปิดโหมดเฝ้าระวัง")

    def set_active_model(self, key):
        if key not in MODEL_NAMES:
            raise ValueError("ไม่รู้จักโมเดลนี้")
        with self.lock:
            self.cfg["active_model"] = key
            save_config(self.cfg)
            if self.live:
                self.live.set_model(key)
                self.prev_alarm = False
            self._event("info", f"ใช้โมเดล {MODEL_NAMES[key]}")

    def train(self, use_ph):
        with self.lock:
            if self.training:
                raise ValueError("กำลังเทรนอยู่")
            if self.rec:
                raise ValueError("กำลังเก็บข้อมูลอยู่ รอให้เสร็จก่อนเทรน")
            self.training = True
            cfg = copy.deepcopy(self.cfg)
        logs = []
        try:
            report = train(cfg, use_ph=bool(use_ph), log=logs.append)
            with self.lock:
                self.cfg["use_ph"] = bool(use_ph)
                self.cfg["active_model"] = report["meta"]["best"]
                save_config(self.cfg)
                self.load_model()
                self._event("info", f"เทรนเสร็จ: ใช้ {MODEL_NAMES[report['meta']['best']]}")
            return {"ok": True, "log": logs, "best": report["meta"]["best"]}
        except TrainError as e:
            return {"ok": False, "error": str(e), "log": logs}
        finally:
            with self.lock:
                self.training = False

    def embedding(self, method):
        return embedding(method)

    # ------------------------------------------------------------ recording
    def runs(self):
        out = []
        for fp in sorted(DATA_DIR.glob("*.csv")):
            st = fp.stat()
            key = (st.st_mtime_ns, st.st_size)
            cached = self._row_cache.get(fp.name)
            if not cached or cached[0] != key:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    header = f.readline()
                    n = sum(1 for _ in f)
                cached = (key, n, "ph_pin_v" in header)
                self._row_cache[fp.name] = cached
            m = NAME_RE.match(fp.name)
            out.append({"file": fp.name, "run": int(m.group(1)) if m else None,
                        "label": m.group(2) if m else "?", "time": m.group(3) if m else "",
                        "rows": cached[1], "has_ph": cached[2]})
        return out

    def next_run(self):
        return max([r["run"] for r in self.runs() if r["run"] is not None] or [0]) + 1

    def start_record(self, run, label, minutes):
        label = (label or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", label):
            raise ValueError("label ใช้ได้เฉพาะ a-z 0-9 _ - (ห้ามเว้นวรรค)")
        run, minutes = int(run), float(minutes)
        if run < 1 or not (0.1 <= minutes <= 240):
            raise ValueError("เลขรอบต้อง ≥ 1 และเวลา 0.1-240 นาที")
        if any(r["run"] == run and r["rows"] > 0 for r in self.runs()):
            raise ValueError(f"รอบที่ {run} มีข้อมูลแล้ว: แก้วน้ำใหม่ใช้เลขรอบใหม่")
        with self.lock:
            if self.rec:
                raise ValueError("กำลังเก็บข้อมูลอยู่แล้ว")
            if time.time() - self.last_rx > 5:
                raise ValueError("ยังไม่มีข้อมูลจากเซนเซอร์ ตรวจการเชื่อมต่อก่อน")
            path = DATA_DIR / f"run{run:03d}_{label}_{time.strftime('%Y%m%d-%H%M%S')}.csv"
            f = open(path, "w", newline="", encoding="utf-8")
            w = csv.writer(f)
            w.writerow(["pc_time", "ms", "run_id", "label", "temp_c", "tds_v", "turb_v", "ph_pin_v"])
            self.rec = {"run": run, "label": label, "minutes": minutes, "file": path.name, "fh": f, "w": w,
                        "rows": 0, "elapsed": 0.0, "last_ms": None, "started": time.time()}
            self._event("info", f"เริ่มเก็บข้อมูล รอบ {run} ({label}) {minutes:g} นาที")

    def _record(self, raw):
        r = self.rec
        if not r:
            return
        ms = raw.get("ms")
        if r["last_ms"] is not None and ms is not None:
            d = ms - r["last_ms"]
            r["elapsed"] += d / 1000 if 0 < d < 10000 else 1.0
        r["last_ms"] = ms
        r["w"].writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), ms, r["run"], r["label"],
                         *[("nan" if v is None or not math.isfinite(v) else round(v, 4))
                           for v in (raw.get("temp_c"), raw.get("tds_v"), raw.get("turb_v"), raw.get("ph_pin_v"))]])
        r["fh"].flush()
        r["rows"] += 1
        if r["elapsed"] >= r["minutes"] * 60:
            self._finish_record("ครบเวลา")

    def stop_record(self):
        with self.lock:
            if self.rec:
                self._finish_record("หยุดโดยผู้ใช้")

    def _finish_record(self, why):
        r = self.rec
        r["fh"].close()
        self.rec = None
        self.rec_last = {"run": r["run"], "label": r["label"], "rows": r["rows"], "file": r["file"], "why": why}
        self._event("info", f"เก็บรอบ {r['run']} ({r['label']}) เสร็จ: {r['rows']} แถว ({why})")

    # ------------------------------------------------------------ calibration
    def calibrate(self, action, value=None):
        with self.lock:
            recent = list(self.raw_recent)[-10:]
            if len(recent) < 3 or time.time() - self.last_rx > 5:
                raise ValueError("ยังไม่มีข้อมูลสดจากเซนเซอร์")

            def med(k):
                v = np.nanmedian([r.get(k, np.nan) for r in recent])
                if not np.isfinite(v):
                    raise ValueError(f"ไม่มีค่า {k} จากเซนเซอร์")
                return float(v)

            c = self.cfg["calib"]
            if action == "tds_air":
                c["tds_offset_v"] = round(med("tds_v"), 4)
            elif action == "tds_std":
                target = float(value)
                raw_ppm = float(tds_ppm(med("tds_v"), med("temp_c"), c["tds_offset_v"], 1.0))
                if raw_ppm < 5:
                    raise ValueError("ค่า TDS ต่ำเกินไป: จุ่มหัววัดในสารละลายมาตรฐานก่อน")
                c["tds_k"] = round(target / raw_ppm, 4)
            elif action == "ph_point":
                slot, ph = int(value["slot"]), float(value["ph"])
                po = med("ph_pin_v") * self.cfg["ph_divider"]
                c["ph_points"][slot] = [ph, round(po, 4)]
                if abs(c["ph_points"][0][1] - c["ph_points"][1][1]) < 0.02:
                    self._event("info", "คำเตือน: แรงดัน pH สองจุดใกล้กันมาก ตรวจว่าจุ่มคนละบัฟเฟอร์")
            else:
                raise ValueError("ไม่รู้จักคำสั่งคาลิเบรต")
            save_config(self.cfg)
            return copy.deepcopy(c)

    # ------------------------------------------------------------ config
    def public_config(self):
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
        for path in SECRET_KEYS:
            d = cfg
            for k in path[:-1]:
                d = d[k]
            if d.get(path[-1]):
                d[path[-1]] = MASK + str(d[path[-1]])[-2:]
        return cfg

    def update_config(self, patch):
        patch = known_only(DEFAULT_CONFIG, patch)
        for path in SECRET_KEYS:           # masked value sent back unchanged -> keep the stored secret
            d = patch
            for k in path[:-1]:
                d = d.get(k, {}) if isinstance(d, dict) else {}
            if isinstance(d, dict) and str(d.get(path[-1], "")).startswith(MASK):
                d.pop(path[-1])
        with self.lock:
            old_port, old_model = self.cfg["port"], self.cfg["active_model"]
            self.cfg = deep_merge(self.cfg, patch)
            save_config(self.cfg)
            if self.cfg["port"] != old_port:
                self.sim_forced = False
                self.gen += 1
                self.link = {"ok": False, "msg": "กำลังเปลี่ยนพอร์ต...", "port": None}
            if self.cfg["active_model"] != old_model and self.live:
                self.live.set_model(self.cfg["active_model"])
        return self.public_config()

    def test_alerts(self):
        a = self.cfg["alerts"]
        if not alerts.configured(a):
            raise ValueError("ยังไม่ได้ตั้งค่าช่องทางแจ้งเตือน (ntfy / Telegram / LINE)")
        return alerts.send_all(a, "🔔 Water Sentinel: ทดสอบ", "ถ้าเห็นข้อความนี้ แปลว่าแจ้งเตือนใช้งานได้", urgent=False)

    def sim_scenario(self, name):
        if self.port_pref() != "SIM":
            raise ValueError("ใช้ได้เฉพาะโหมดจำลอง")
        self.source.sim.set(name)
        with self.lock:
            self._event("info", f"จำลอง: {Simulator.SCENARIOS[name][0]}")

    # ------------------------------------------------------------ snapshot for the UI
    def model_summary(self):
        b = self.bundle
        if not b:
            return {"exists": False}
        m = b["meta"]
        return {"exists": True, "created": m["created"], "features": m["features"], "use_ph": m["use_ph"],
                "active": self.live.key if self.live else None,
                "active_name": self.live.name if self.live else None,
                "best": m["best"], "protocol": m["protocol"], "normal_runs": m["normal_runs"],
                "anomaly_runs": m["anomaly_runs"], "mismatch": m["use_ph"] != self.cfg["use_ph"],
                "names": MODEL_NAMES}

    def state(self, since=0.0):
        with self.lock:
            now = time.time()
            r = self.rec
            rec = None if not r else {"run": r["run"], "label": r["label"], "rows": r["rows"],
                                      "elapsed": round(r["elapsed"], 1), "minutes": r["minutes"], "file": r["file"]}
            return clean({
                "now": now,
                "link": {**self.link, "age": (now - self.last_rx) if self.last_rx else None},
                "sim": self.port_pref() == "SIM",
                "sim_scenario": self.source.sim.name,
                "latest": self.latest,
                "use_ph": self.cfg["use_ph"],
                "monitor_on": self.cfg["monitor_on"],
                "detector": self.det_out,
                "model": self.model_summary(),
                "training": self.training,
                "rec": rec,
                "rec_last": self.rec_last,
                "next_run": None,
                "events": list(self.events)[:40],
                "history": [h for h in self.history if h["t"] > since],
            })


__all__ = ["Hub", "MODEL_DIR"]
