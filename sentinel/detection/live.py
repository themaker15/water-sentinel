"""Scores one live sample at a time with exactly the training pipeline."""
from collections import deque

import numpy as np

from . import causes
from .drift import DriftTracker
from ..acquisition.features import FAULTS, SENSORS, derive_one, fault_one
from .models import MODEL_NAMES, timing
from .training import explain


class LiveMonitor:
    """median filter -> model score -> (EWMA) -> normalised ratio -> persistence/hysteresis.

    Window lengths are set in seconds and converted with the sample interval measured from the
    device clock, so old (2.3 s) and new (1 s) firmware alarm after the same time.
    """

    def __init__(self, bundle, key):
        self.bundle = bundle
        self.meta = bundle["meta"]
        self.feats = self.meta["features"]
        self.det = self.meta["detector"]
        self.mspc = bundle["models"]["mspc"]["model"]   # always used to explain which sensor moved
        self.default_dt = float(self.meta.get("interval_s", 1.0))
        d = self.meta.get("drift")
        self.drift = DriftTracker(d["k"], d["h"]) if d else None      # models from v1.0 have no drift channel
        # single-sensor alarm boundary of MSPC, used for "time until alarm" estimates
        self.edge = np.sqrt(max(bundle["models"]["mspc"]["thr"], 1.0)) * self.mspc.sd
        self.set_model(key)

    def set_model(self, key):
        if key not in self.bundle["models"]:
            key = "mspc"
        self.key = key
        bm = self.bundle["models"][key]
        self.model, self.thr, self.med = bm["model"], bm["thr"], bm["med"]
        self.reset()

    @property
    def name(self):
        return MODEL_NAMES.get(self.key, self.key)

    def reset(self):
        self.buf = deque(maxlen=200)
        self.deltas = deque(maxlen=15)
        self.last_ms = None
        self.dev_t = 0.0
        self.drifting = False
        if getattr(self, "drift", None):
            self.drift.reset()
        self.e = None
        self.state, self.above, self.below = False, 0, 0
        self.alarm_z = deque(maxlen=120)       # z-vectors since the alarm began (the "fingerprint" window)
        self.fault_in_alarm = False

    def interval(self):
        return float(np.median(self.deltas)) / 1000 if self.deltas else self.default_dt

    def update(self, raw):
        ms = raw.get("ms")
        if ms is not None and self.last_ms is not None and 0 < ms - self.last_ms < 10000:
            self.deltas.append(ms - self.last_ms)
            self.dev_t += (ms - self.last_ms) / 1000
        else:
            self.dev_t += self.interval()
        self.last_ms = ms
        tm = timing(self.det, self.interval())

        d = derive_one(raw, self.meta["calib"], self.meta["ph_divider"])
        code = fault_one(raw, self.meta["use_ph"], self.meta["limits"], self.feats)
        if code:
            self.buf.clear()   # restart the median after the probe is back in water
            self.fault_in_alarm = self.fault_in_alarm or self.state
            return {"status": "fault", "fault": code, "fault_text": FAULTS[code], "in_alarm": self.state}
        self.buf.append(np.array([d[f] for f in self.feats], float))
        if len(self.buf) < tm["median_win"]:
            left = tm["median_win"] - len(self.buf)
            return {"status": "starting", "left": left, "left_s": round(left * tm["dt"]), "in_alarm": self.state}

        x = np.median(np.array(list(self.buf)[-tm["median_win"]:]), axis=0)
        s = float(self.model.score(x[None, :])[0])
        lam = tm["lam"] if self.model.ewma else 1.0
        self.e = s if self.e is None else lam * s + (1 - lam) * self.e
        ratio = (self.e - self.med) / (self.thr - self.med)   # 0 = typical normal, 1 = threshold

        persist, release = tm["persist"], self.det.get("release", 0.8)
        if not self.state:
            self.above = self.above + 1 if ratio > 1.0 else 0
            if self.above >= persist:
                self.state, self.below = True, 0
        else:
            self.below = self.below + 1 if ratio < release else 0
            if self.below >= persist:
                self.state, self.above = False, 0

        z = (x - self.mspc.mu) / self.mspc.sd
        if self.state:
            self.alarm_z.append(z)
        else:
            self.alarm_z.clear()
            self.fault_in_alarm = False
        out = {"status": "anomaly" if self.state else "normal", "ratio": float(ratio),
               "cause": explain(z, self.feats), "z": dict(zip(self.feats, z.round(2).tolist())),
               "in_alarm": self.state, "interval_s": round(tm["dt"], 2)}
        if self.state and self.alarm_z:
            out["likely"] = causes.infer(np.median(np.array(self.alarm_z), axis=0), self.feats,
                                         self.meta.get("cause_model"), self.fault_in_alarm)
        if self.drift is not None:
            level = self.drift.update(self.dev_t, x, z, tm["dt"])
            slopes = self.drift.slopes()                          # units per second, robust 60 s trend
            self.drifting = bool((level > 1.0).any())
            trend = []
            for i, f in enumerate(self.feats):
                per_min = slopes[i] * 60
                if abs(per_min) * 5 < self.mspc.sd[i] and level[i] <= 0.5:
                    continue      # would move less than one "meaningful change" in 5 min: just noise
                edge = self.mspc.mu[i] + np.sign(per_min) * self.edge[i]
                eta = (edge - x[i]) / per_min if per_min else None
                info = SENSORS[f]
                trend.append({"sensor": f, "name": info["name"], "unit": info["unit"],
                              "rate_per_min": round(float(per_min), 4), "level": round(float(level[i]), 2),
                              "text": f"{info['name']} slowly {'rising' if per_min > 0 else 'falling'}",
                              "eta_min": round(float(eta), 1) if eta is not None and 0 < eta < 120 else None})
            out["drift"] = {"active": self.drifting, "level": round(float(level.max()), 2),
                            "trend": sorted(trend, key=lambda d: -d["level"])}
            if self.state:
                self.drift.clear()          # early warning is pointless during an alarm; start fresh after it
                self.drifting = False
                out["drift"]["active"] = False
            elif self.drifting:
                out["status"] = "drifting"
        return out
