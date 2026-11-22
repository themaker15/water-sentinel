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

from ..storage import explorer

from ..acquisition import turbidity
from ..config import DATA_DIR, DEFAULT_CONFIG, LOG_DIR, MODEL_DIR, deep_merge, load_config, save_config
from ..storage.datasets import (delete_dataset, load_datasets, normalize_selection, save_dataset, selection_from_cfg,
                       selection_use_ph)
from ..acquisition.features import derive_one, ph_value, tds_ppm
from ..detection.live import LiveMonitor
from ..detection.models import MODEL_NAMES
from ..storage.runs import UNLABELED, list_runs, load_meta, save_meta
from ..acquisition.sources import SourceThread, Simulator
from ..detection import tuning
from ..detection.training import (TrainError, check_selection, data_fingerprint, embedding, list_snapshots, load_bundle,
                       restore_snapshot, train)

SECRET_KEYS = {("alerts", "telegram_token"), ("alerts", "line_token"), ("remote", "pin"), ("ingest_token",)}
MASK = "••••"
_STORED = object()      # "train with the selection stored in config.json"


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
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    return o


RUN_HEADER = ["pc_time", "ms", "run_id", "label", "temp_c", "tds_v", "turb_v", "ph_pin_v"]


def _fmt_num(v):
    return "nan" if v is None or not isinstance(v, (int, float)) or not math.isfinite(v) else round(v, 4)


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
        self.link = {"ok": False, "msg": "Starting…", "port": None}
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
        self.prev_drift = False
        self.last_fault = None
        self.last_likely = None            # experimental cause guess of the latest alarm (causes.py)
        self.training = False
        self.raw_buf = deque(maxlen=4000)  # (device-seconds, raw) for snapshots, > 1 h at 1 s
        self.dev_t = 0.0                   # running device time from ms deltas (survives resets)
        self.pending_snap = None
        self.firmware = None
        self.link_lost = False
        self._stream = None
        self.bundle = None
        self.live = None
        self.load_model()
        self.source = SourceThread(self)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self.source.start()
        threading.Thread(target=self._watchdog, daemon=True, name="link-watchdog").start()

    def _watchdog(self):
        """Raise an event (and a push alert) when sensor data stops while monitoring."""
        while not self.source.stopping:
            time.sleep(2)
            with self.lock:
                limit = self.cfg.get("link_timeout_s", 60)
                silent = self.last_rx and time.time() - self.last_rx > limit
                if silent and self.cfg["monitor_on"] and not self.link_lost:
                    self.link_lost = True
                    self._event("link_lost", f"No data from the sensor for over {limit} s: {self.link['msg']}",
                                notify=True)

    def shutdown(self):
        self.source.stopping = True
        with self.lock:
            if self.rec:
                self._finish_record("App closed")

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
            if not self.link["ok"] or "Waiting for data" in self.link["msg"] or "No data" in self.link["msg"]:
                port = self.link.get("port")
                self.link = {"ok": True, "port": port, "msg": {"sim": "Simulation (no real board)",
                                                              "wifi": "Receiving data over Wi-Fi"}.get(source, f"Receiving data from {port}")}
            d = derive_one(raw, self.cfg["calib"], self.cfg["ph_divider"])
            self.latest = {**d, "time": now, "source": source,
                           "turb": turbidity.convert(d["turb_v"], d["temp_c"], self.cfg["turbidity"])}
            self.raw_recent.append(raw)
            if self.link_lost:
                self.link_lost = False
                self._event("link_restored", "Sensor data received again", notify=self.cfg["monitor_on"])
            fmt = raw.get("_fmt")
            if fmt and fmt != self.firmware:
                self.firmware = fmt
            self._advance_clock(raw)
            self.raw_buf.append((self.dev_t, raw))
            self._record(raw)
            if self.live and self.cfg["monitor_on"]:
                out = self.live.update(raw)
            else:
                out = {"status": "off" if self.live else "no_model"}
            self._transitions(out)
            self.det_out = out
            self._stream_write(now, raw, out)
            self._snapshot_tick()
            self.history.append({"t": round(now, 2), "temp_c": d["temp_c"], "tds_ppm": d["tds_ppm"],
                                 "turb_v": d["turb_v"], "ph": d["ph"] if self.cfg["use_ph"] else None,
                                 "ratio": out.get("ratio"), "status": out["status"]})

    def _transitions(self, out):
        a = self.cfg["alerts"]
        if out["status"] == "fault":
            if out["fault"] != self.last_fault:
                self._event("fault", f"Sensor problem: {out['fault_text']}", notify=a["notify_fault"])
            self.last_fault = out["fault"]
        else:
            self.last_fault = None
        drifting = out["status"] == "drifting"
        if drifting and not self.prev_drift:
            trend = (out.get("drift") or {}).get("trend") or []
            msg = ", ".join(t["text"] + (f" (reaches the alarm line in ~{t['eta_min']:g} min)" if t.get("eta_min") else "")
                            for t in trend[:2]) or "Values drifting slowly"
            self._event("drift", f"Slow drift started: {msg}", notify=a.get("notify_drift", False))
        if out["status"] in ("normal", "drifting", "anomaly"):
            self.prev_drift = drifting
        alarm = bool(out.get("in_alarm"))
        if out.get("likely"):
            self.last_likely = {**out["likely"], "time": time.time(), "active": True}
        if alarm and not self.prev_alarm:
            cause = ", ".join(c["text"] for c in out.get("cause") or []) or "Several sensors changed at once"
            lk = out.get("likely")
            if a.get("notify_cause") and lk and lk["verdict"] in ("match", "ambiguous"):
                cause += f" · (experimental) {lk['headline']}"
            self._event("anomaly", f"Abnormal water detected: {cause}", notify=True)
            if self.cfg.get("auto_snapshot", True) and not self.pending_snap:
                self.pending_snap = {"start": self.dev_t - 120, "end": self.dev_t + 300, "event_t": self.dev_t,
                                     "cause": cause}
        elif self.prev_alarm and not alarm and out["status"] in ("normal", "drifting"):
            self._event("recovered", "Water back to normal", notify=a["notify_recovery"])
            if self.last_likely:
                self.last_likely["active"] = False
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
                title = {"anomaly": "⚠️ Water Sentinel: abnormal water", "recovered": "✅ Water Sentinel: back to normal",
                         "fault": "🔧 Water Sentinel: sensor problem",
                         "link_lost": "🔌 Water Sentinel: connection lost",
                         "link_restored": "🔌 Water Sentinel: connected",
                         "drift": "📈 Water Sentinel: values drifting"}.get(kind, "Water Sentinel")
                urgent = kind in ("anomaly", "fault", "link_lost")
                threading.Thread(target=self._send, args=(ev, title, dict(a), urgent),
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
                raise ValueError("No model yet: record normal water and train first")
            self.cfg["monitor_on"] = bool(on)
            save_config(self.cfg)
            if self.live:
                self.live.reset()
            self.prev_alarm = False
            self.last_likely = None
            self.det_out = {"status": "starting" if on else "off", "left": None}
            self._event("info", "Monitoring on" if on else "Monitoring off")

    def set_active_model(self, key):
        if key not in MODEL_NAMES:
            raise ValueError("Unknown model")
        with self.lock:
            if self.bundle and key not in self.bundle["models"]:
                raise ValueError("This model was not part of the latest training: select it when training first")
            self.cfg["active_model"] = key
            save_config(self.cfg)
            if self.live:
                self.live.set_model(key)
                self.prev_alarm = False
            self._event("info", f"Using model {MODEL_NAMES[key]}")

    def train(self, use_ph, force=False, selection=_STORED):
        """selection: dict from the picker/API, None = every usable run (v1.0 behaviour), omitted = the stored one."""
        with self.lock:
            sel = selection_from_cfg(self.cfg) if selection is _STORED else normalize_selection(selection)
            use_ph = selection_use_ph(sel, bool(use_ph))
            if not force and self.bundle and \
                    self.bundle["meta"].get("data_hash") == data_fingerprint(self.cfg, use_ph, selection=sel):
                return {"ok": True, "unchanged": True, "log": ["Data and settings are the same as the last training, no need to retrain"],
                        "best": self.bundle["meta"]["best"]}
            if self.training:
                raise ValueError("Training in progress")
            if self.rec:
                raise ValueError("Recording in progress: wait for it to finish before training")
            self.training = True
            cfg = copy.deepcopy(self.cfg)
        logs = []
        try:
            report = train(cfg, use_ph=use_ph, log=logs.append, selection=sel)
            with self.lock:
                self.cfg["use_ph"] = "ph" in report["meta"]["features"]
                self.cfg["training"] = {"dataset": sel["dataset"] if sel else None, "files": sel["files"] if sel else None,
                                        "sensors": sel["sensors"] if sel else None, "models": sel["models"] if sel else None}
                self.cfg["active_model"] = report["meta"]["best"]
                save_config(self.cfg)
                self.load_model()
                self._event("info", f"Training done: using {MODEL_NAMES[report['meta']['best']]}"
                            + (f" (dataset {sel['dataset']})" if sel and sel["dataset"] else ""))
            return {"ok": True, "log": logs, "best": report["meta"]["best"]}
        except TrainError as e:
            return {"ok": False, "error": str(e), "log": logs}
        finally:
            with self.lock:
                self.training = False

    def snapshots(self):
        cur = self.bundle["meta"].get("created") if self.bundle else None
        return {"snapshots": list_snapshots(), "active_created": cur,
                "active_features": self.bundle["meta"]["features"] if self.bundle else None}

    def use_snapshot(self, sid):
        """Put an earlier training back as the live model."""
        with self.lock:
            if self.training:
                raise ValueError("Training in progress")
            restore_snapshot(sid)
            b = load_bundle()
            if b:
                self.cfg["use_ph"] = "ph" in b["meta"]["features"]
                self.cfg["active_model"] = b["meta"]["best"]
                save_config(self.cfg)
            self.load_model()
            if self.bundle:
                n = len(self.bundle["meta"]["features"])
                self._event("info", f"Switched to the model trained {self.bundle['meta'].get('created')} ({n} sensors)")
        return self.snapshots()

    def train_check(self, selection):
        """Guard rails for the training picker (nothing is fitted)."""
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
        sel = normalize_selection(selection)
        return check_selection(cfg, selection_use_ph(sel, cfg["use_ph"]), sel)

    # ------------------------------------------------------------ named datasets
    def datasets(self):
        with self.lock:
            cur = selection_from_cfg(self.cfg)
        return {"datasets": load_datasets(DATA_DIR), "current": cur}

    def dataset_save(self, name, selection, note=""):
        return save_dataset(name, selection, note, DATA_DIR)

    def dataset_delete(self, name):
        delete_dataset(name, DATA_DIR)

    def embedding(self, method):
        return embedding(method)

    # ------------------------------------------------------------ tuning
    def _model_use_ph(self):
        return self.bundle["meta"]["use_ph"] if self.bundle else self.cfg["use_ph"]

    def tune_preview(self, key, settings):
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
        return tuning.preview(cfg, self._model_use_ph(), key, settings or {})

    def tune_suggest(self):
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
        return tuning.suggest_floors(cfg, self._model_use_ph())

    def tune_apply(self, settings, retrain=True):
        settings = {k: v for k, v in (settings or {}).items() if k in ("detector", "floors", "model_params")}
        self.update_config(settings)
        with self.lock:
            self._event("info", "Detector settings updated" + (" retraining" if retrain else ""))
        return self.train(self._model_use_ph(), force=True) if retrain else {"ok": True}   # keeps the stored selection

    def tune_restore(self, index):
        hist = tuning.load_history(MODEL_DIR)
        if not -len(hist) <= int(index) < len(hist):
            raise ValueError("Settings history entry not found")
        h = hist[int(index)]
        return self.tune_apply({"detector": h["detector"], "floors": h["floors"],
                                "model_params": h.get("model_params") or {}}, retrain=True)

    # ------------------------------------------------------------ recording
    def runs(self):
        return list_runs(DATA_DIR, self.cfg.get("session_gap_min", 10))

    def _run_path(self, file):
        path = DATA_DIR / str(file)
        if path.parent != DATA_DIR or path.suffix != ".csv" or not path.exists():
            raise ValueError("Run file not found")
        return path

    def run_detail(self, file, with_score=True):
        path = self._run_path(file)
        with self.lock:
            cfg, bundle = copy.deepcopy(self.cfg), self.bundle
            key = self.live.key if self.live else "mspc"
        d = explorer.run_detail(path, cfg, bundle, key, with_score)
        d["session"] = {r["file"]: r["session"] for r in self.runs()}.get(file)
        return clean(d)

    def stats_csv(self, files):
        if not files or len(files) > 40:
            raise ValueError("Select 1–40 runs")
        return explorer.stats_csv([self.run_detail(f, with_score=False) for f in files])

    def archive_run(self, file):
        with self.lock:
            if self.rec and self.rec["file"] == file:
                raise ValueError("This run is being recorded")
            name = explorer.archive_run(DATA_DIR, file)
            self._event("info", f"Moved {file} to data/{explorer.ARCHIVE}/ (not deleted)")
        return name

    def restore_run(self, file):
        with self.lock:
            name = explorer.restore_run(DATA_DIR, file)
            self._event("info", f"Restored {name} from the archive")
        return name

    def archived(self):
        return explorer.list_archived(DATA_DIR)

    def next_run(self, runs=None):
        runs = self.runs() if runs is None else runs
        return max([r["run"] for r in runs if r["run"] is not None] or [0]) + 1

    def update_run(self, file, patch):
        path = DATA_DIR / file
        if path.parent != DATA_DIR or path.suffix != ".csv" or not path.exists():
            raise ValueError("Run file not found")
        allowed = {k: patch[k] for k in ("label", "note", "excluded", "new_water") if k in patch}
        meta = save_meta(path, allowed)
        with self.lock:
            self._event("info", f"Edited run {file}: " + ", ".join(f"{k}={v}" for k, v in allowed.items()))
        return meta

    def start_record(self, run, label, minutes, note="", new_water=False):
        label = (label or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", label) or label == UNLABELED:
            raise ValueError("Labels may only contain a-z 0-9 _ - (no spaces)")
        run, minutes = int(run), float(minutes)
        if run < 1 or not (0.1 <= minutes <= 240):
            raise ValueError("Run number must be ≥ 1 and duration 0.1–240 min")
        runs = self.runs()
        if any(r["run"] == run and r["rows"] > 0 for r in runs):
            raise ValueError(f"Run {run} already has data: use a new run number for a new cup")
        warnings = []
        with self.lock:
            if self.rec:
                raise ValueError("Already recording")
            if time.time() - self.last_rx > 5:
                raise ValueError("No sensor data yet: check the connection first")
            if label == "normal" and self.prev_alarm:
                warnings.append("The system is alarming on abnormal water but you are recording normal: check that the water really is normal")
            if runs and not new_water and label == "normal":
                last = runs[-1]
                end = time.mktime(time.strptime(last["start"], "%Y-%m-%dT%H:%M:%S")) + last["duration_s"]
                if (time.time() - end) / 60 <= self.cfg.get("session_gap_min", 10):
                    warnings.append(f"This run will count as the same water as run {last['run']} (session {last['session']}) "
                                    "If you changed the water, tick \"New water\"")
            path = DATA_DIR / f"run{run:03d}_{label}_{time.strftime('%Y%m%d-%H%M%S')}.csv"
            f = open(path, "w", newline="", encoding="utf-8")
            w = csv.writer(f)
            w.writerow(RUN_HEADER)
            meta = {"source": "record", "note": note or "", "new_water": bool(new_water),
                    "firmware": self.firmware, "recorded_during_alarm": bool(self.prev_alarm),
                    "calib": self._calib_snapshot()}
            save_meta(path, meta)
            self.rec = {"run": run, "label": label, "minutes": minutes, "file": path.name, "fh": f, "w": w,
                        "rows": 0, "elapsed": 0.0, "last_ms": None, "started": time.time()}
            self._event("info", f"Recording run {run} ({label}) for {minutes:g} min")
        return [w for w in warnings if w]

    def _calib_snapshot(self):
        c = self.cfg["calib"]
        return {"tds_offset_v": c["tds_offset_v"], "tds_k": c["tds_k"], "ph_divider": self.cfg["ph_divider"]}

    # ------------------------------------------------------------ device clock, stream log, snapshots
    def _advance_clock(self, raw):
        ms = raw.get("ms")
        last = getattr(self, "_last_ms", None)
        if last is not None and ms is not None:
            d = ms - last
            self.dev_t += d / 1000 if 0 < d < 10000 else 1.0
        self._last_ms = ms

    def _stream_write(self, now, raw, out):
        """Append every sample to logs/stream-YYYYMMDD.csv so nothing seen live is ever lost."""
        try:
            day = time.strftime("%Y%m%d", time.localtime(now))
            if not self._stream or self._stream[0] != day:
                if self._stream:
                    self._stream[1].close()
                path = LOG_DIR / f"stream-{day}.csv"
                new = not path.exists()
                fh = open(path, "a", newline="", encoding="utf-8")
                w = csv.writer(fh)
                if new:
                    w.writerow(["pc_time", "ms", "temp_c", "tds_v", "turb_v", "ph_pin_v", "status", "ratio"])
                self._stream = (day, fh, w, 0)
                self._prune_stream_logs()
            day, fh, w, n = self._stream
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), raw.get("ms"),
                        *[_fmt_num(raw.get(k)) for k in ("temp_c", "tds_v", "turb_v", "ph_pin_v")],
                        out.get("status"), _fmt_num(out.get("ratio"))])
            n += 1
            if n % 10 == 0:
                fh.flush()
            self._stream = (day, fh, w, n)
        except OSError:
            pass

    def _prune_stream_logs(self):
        keep = self.cfg.get("stream_log_days", 14)
        cutoff = time.time() - keep * 86400
        for f in LOG_DIR.glob("stream-*.csv"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    def _snapshot_tick(self):
        p = self.pending_snap
        if p and self.dev_t >= p["end"]:
            self.pending_snap = None
            rows = [(t, r) for t, r in self.raw_buf if p["start"] <= t <= p["end"]]
            path = self._write_snapshot(rows, UNLABELED, {
                "source": "auto", "note": f"Auto-saved alarm run: {p['cause']}",
                "markers": [{"t_s": round(p["event_t"] - rows[0][0], 1), "text": "system alarm"}] if rows else []})
            if path:
                self._event("info", f"Alarm period saved ({path.name}): label it on the Record tab")

    def _write_snapshot(self, rows, label, meta):
        if len(rows) < 5:
            return None
        run = self.next_run()
        began = time.time() - (self.dev_t - rows[0][0])     # file name carries the start time, like recordings
        path = DATA_DIR / f"run{run:03d}_{label}_{time.strftime('%Y%m%d-%H%M%S', time.localtime(began))}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(RUN_HEADER)
            for _, raw in rows:
                w.writerow(["", raw.get("ms"), run, label,
                            *[_fmt_num(raw.get(k)) for k in ("temp_c", "tds_v", "turb_v", "ph_pin_v")]])
        save_meta(path, {**meta, "firmware": self.firmware, "calib": self._calib_snapshot()})
        return path

    def save_last(self, minutes, label):
        """Save the last N minutes seen live as a run (e.g. after testing in monitor mode)."""
        label = (label or UNLABELED).strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", label):
            raise ValueError("Labels may only contain a-z 0-9 _ - (no spaces)")
        minutes = float(minutes)
        if not 0.2 <= minutes <= 60:
            raise ValueError("Choose 0.2–60 min")
        with self.lock:
            rows = [(t, r) for t, r in self.raw_buf if t >= self.dev_t - minutes * 60]
            path = self._write_snapshot(rows, label, {"source": "snapshot", "note": f"Saved last {minutes:g} min"})
            if not path:
                raise ValueError("Not enough live data yet")
            self._event("info", f"Saved last {minutes:g} min as {path.name}")
            return path.name

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
                         *[_fmt_num(raw.get(k)) for k in ("temp_c", "tds_v", "turb_v", "ph_pin_v")]])
        r["fh"].flush()
        r["rows"] += 1
        if r["elapsed"] >= r["minutes"] * 60:
            self._finish_record("time up")

    def mark(self, text, kind="note", dose=None):
        """Event marker during a recording (e.g. "added 1 mL stock"), stored in the run's sidecar."""
        text = (text or "").strip()[:120]
        if not text:
            raise ValueError("Enter event text first")
        if kind not in ("note", "dose", "stir", "probe"):
            raise ValueError("Invalid event type")
        if dose not in (None, ""):
            dose = float(dose)
            kind = "dose"
        else:
            dose = None
        with self.lock:
            r = self.rec
            if not r:
                raise ValueError("Events can only be marked while recording")
            path = DATA_DIR / r["file"]
            markers = load_meta(path).get("markers", [])
            m = {"t_s": round(r["elapsed"], 1), "text": text, "kind": kind}
            if dose is not None:
                m["dose"] = dose
            markers.append(m)
            save_meta(path, {"markers": markers})
            self._event("info", f"Event at {m['t_s']:.0f} s of run {r['run']}: {text}")
            return m

    def stop_record(self):
        with self.lock:
            if self.rec:
                self._finish_record("stopped by user")

    def _finish_record(self, why):
        r = self.rec
        r["fh"].close()
        self.rec = None
        self.rec_last = {"run": r["run"], "label": r["label"], "rows": r["rows"], "file": r["file"], "why": why}
        self._event("info", f"Run {r['run']} ({r['label']}) done: {r['rows']} rows ({why})")

    # ------------------------------------------------------------ turbidity (NTU display)
    def turb_info(self):
        with self.lock:
            return clean(turbidity.info(copy.deepcopy(self.cfg["turbidity"]), self.latest))

    def _recent_raw(self, seconds, minimum):
        rows = [r for t, r in self.raw_buf if t >= self.dev_t - seconds]
        if not self.last_rx or time.time() - self.last_rx > 5 or len(rows) < minimum:
            raise ValueError("Not enough live data yet (at least %d consecutive sensor readings needed)" % minimum)
        return rows

    def _turb_median(self, seconds, minimum):
        rows = self._recent_raw(seconds, minimum)
        v = np.array([r.get("turb_v", np.nan) for r in rows], float)
        t = np.array([r.get("temp_c", np.nan) for r in rows], float)
        lim = self.cfg["limits"]
        ok = np.isfinite(v) & (v > lim["min_signal_v"]) & (v < lim["adc_max_v"])
        if ok.sum() < minimum:
            raise ValueError("Turbidity signal is abnormal (unreadable or full scale): check the sensor cable first")
        med = float(np.median(v[ok]))
        mad = float(1.4826 * np.median(np.abs(v[ok] - med)))
        tt = float(np.nanmedian(t)) if np.isfinite(t).any() else None
        return med, tt, mad

    def turb_set_clear(self, seconds=30):
        """Clear-water reference = median of the last 30 s. Sets v_clear, t_clear and the noise estimate."""
        with self.lock:
            med, tt, mad = self._turb_median(seconds, 8)
            c = self.cfg["turbidity"]
            c.update(v_clear=round(med, 4), t_clear=None if tt is None else round(tt, 2), noise_v=round(max(mad, 0.002), 4))
            save_config(self.cfg)
            self._event("info", f"Turbidity clear-water reference set: {med:.3f} V" + (f" at {tt:.1f} °C" if tt is not None else ""))
        return self.turb_info()

    def turb_add_point(self, ntu):
        """A user standard: the NTU the person reads from a reference meter, with the volts measured right now."""
        ntu = float(ntu)
        if not 0 <= ntu <= 5000:
            raise ValueError("NTU must be between 0 and 5000")
        with self.lock:
            c = self.cfg["turbidity"]
            if not c.get("v_clear"):
                raise ValueError("Set the clear-water reference first (dip the probe in clear water and press \"Set as clear water\")")
            med, tt, _ = self._turb_median(10, 4)
            pts = [p for p in c["points"] if abs(p[0] - ntu) > 1e-9] + [[ntu, round(med, 4), None if tt is None else round(tt, 2)]]
            if len(pts) > 12:
                raise ValueError("At most 12 standard points")
            trial = {**c, "points": pts, "curve": "user_points"}
            turbidity.curve(trial)                       # raises ValueError for an impossible set of points
            c.update(points=pts, curve="user_points")
            save_config(self.cfg)
            self._event("info", f"Added turbidity standard {ntu:g} NTU = {med:.3f} V")
        return self.turb_info()

    def turb_delete_point(self, index):
        with self.lock:
            c = self.cfg["turbidity"]
            if not 0 <= int(index) < len(c["points"]):
                raise ValueError("Point not found")
            c["points"].pop(int(index))
            if not c["points"] and c["curve"] == "user_points":
                c["curve"] = "generic_vendor"
            save_config(self.cfg)
        return self.turb_info()

    def turb_tempco(self):
        with self.lock:
            gap, warm = self.cfg.get("session_gap_min", 10), self.cfg["detector"]["warmup_s"]
        return turbidity.fit_temp_coeff(DATA_DIR, warm, session_gap_min=gap)

    # ------------------------------------------------------------ calibration
    def calibrate(self, action, value=None):
        with self.lock:
            recent = list(self.raw_recent)[-10:]
            if len(recent) < 3 or time.time() - self.last_rx > 5:
                raise ValueError("No live sensor data yet")

            def med(k):
                v = np.nanmedian([r.get(k, np.nan) for r in recent])
                if not np.isfinite(v):
                    raise ValueError(f"No {k} value from the sensor")
                return float(v)

            c = self.cfg["calib"]
            if action == "tds_air":
                c["tds_offset_v"] = round(med("tds_v"), 4)
            elif action == "tds_std":
                target = float(value)
                raw_ppm = float(tds_ppm(med("tds_v"), med("temp_c"), c["tds_offset_v"], 1.0))
                if raw_ppm < 5:
                    raise ValueError("TDS reading too low: dip the probe in the standard solution first")
                c["tds_k"] = round(target / raw_ppm, 4)
            elif action == "ph_multi_offset":
                # all probes in water whose true pH is `value` (checked with the pH probe alone): store the shift
                if med("tds_v") <= c.get("ph_wet_tds_v", 0.2):
                    raise ValueError("TDS probe is not in the water: dip all probes in first")
                raw_ph = float(ph_value(med("ph_pin_v"), self.cfg["ph_divider"], c["ph_points"]))
                c["ph_multi_offset"] = round(raw_ph - float(value), 3)
            elif action == "ph_point":
                slot, ph = int(value["slot"]), float(value["ph"])
                po = med("ph_pin_v") * self.cfg["ph_divider"]
                c["ph_points"][slot] = [ph, round(po, 4)]
                if abs(c["ph_points"][0][1] - c["ph_points"][1][1]) < 0.02:
                    self._event("info", "Warning: the two pH voltages are very close; check you used different buffers")
            else:
                raise ValueError("Unknown calibration command")
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
        patch.pop("training", None)                  # only a real training run sets this
        tuning.validate(patch)
        turbidity.validate(patch)
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
                self.link = {"ok": False, "msg": "Switching port…", "port": None}
            if self.cfg["active_model"] != old_model and self.live:
                self.live.set_model(self.cfg["active_model"])
        return self.public_config()

    def test_alerts(self):
        a = self.cfg["alerts"]
        if not alerts.configured(a):
            raise ValueError("No alert channel set up yet (ntfy / Telegram / LINE)")
        return alerts.send_all(a, "🔔 Water Sentinel: test", "If you see this message, alerts are working", urgent=False)

    def sim_scenario(self, name):
        if self.port_pref() != "SIM":
            raise ValueError("Only available in simulation mode")
        self.source.sim.set(name)
        with self.lock:
            self._event("info", f"Simulating: {Simulator.SCENARIOS[name][0]}")

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
                "n_sessions": m.get("n_sessions"), "interval_s": m.get("interval_s"),
                "dataset": m.get("dataset"), "n_files": len(m.get("files", [])), "test_runs": m.get("test_runs", []),
                "models": m.get("models"),
                "new_runs": self._new_runs(m), "names": MODEL_NAMES}

    def _new_runs(self, meta):
        """Usable runs recorded after this model was trained (None for models from v1.0)."""
        if "files" not in meta:
            return None
        used = set(meta.get("seen") or meta["files"])      # runs deliberately left out of the selection are not "new"
        return sum(1 for r in self.runs() if r["rows"] > 0 and not r["excluded"]
                   and r["label"] != UNLABELED and r["file"] not in used)

    def state(self, since=0.0):
        with self.lock:
            now = time.time()
            r = self.rec
            rec = None if not r else {"run": r["run"], "label": r["label"], "rows": r["rows"],
                                      "elapsed": round(r["elapsed"], 1), "minutes": r["minutes"], "file": r["file"],
                                      "markers": load_meta(DATA_DIR / r["file"]).get("markers", [])}
            return clean({
                "now": now,
                "link": {**self.link, "age": (now - self.last_rx) if self.last_rx else None},
                "sim": self.port_pref() == "SIM",
                "firmware": self.firmware, "link_lost": self.link_lost,
                "snapshot_pending": bool(self.pending_snap),
                "sim_scenario": self.source.sim.name,
                "latest": self.latest,
                "likely": self.last_likely,
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
