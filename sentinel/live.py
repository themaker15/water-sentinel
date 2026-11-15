"""Scores one live sample at a time with exactly the training pipeline."""
from collections import deque

import numpy as np

from .features import FAULTS, derive_one, fault_one
from .models import MODEL_NAMES
from .training import explain


class LiveMonitor:
    """median filter -> model score -> (EWMA) -> normalised ratio -> persistence/hysteresis."""

    def __init__(self, bundle, key):
        self.bundle = bundle
        self.meta = bundle["meta"]
        self.feats = self.meta["features"]
        self.det = self.meta["detector"]
        self.mspc = bundle["models"]["mspc"]["model"]   # always used to explain which sensor moved
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
        self.buf = deque(maxlen=self.det["median_win"])
        self.e = None
        self.state, self.above, self.below = False, 0, 0

    def update(self, raw):
        d = derive_one(raw, self.meta["calib"], self.meta["ph_divider"])
        code = fault_one(raw, self.meta["use_ph"], self.meta["limits"])
        if code:
            self.buf.clear()   # restart the median after the probe is back in water
            return {"status": "fault", "fault": code, "fault_text": FAULTS[code], "in_alarm": self.state}
        self.buf.append(np.array([d[f] for f in self.feats], float))
        if len(self.buf) < self.buf.maxlen:
            return {"status": "starting", "left": self.buf.maxlen - len(self.buf), "in_alarm": self.state}

        x = np.median(np.array(self.buf), axis=0)
        s = float(self.model.score(x[None, :])[0])
        lam = self.det["ewma_lambda"] if self.model.ewma else 1.0
        self.e = s if self.e is None else lam * s + (1 - lam) * self.e
        ratio = (self.e - self.med) / (self.thr - self.med)   # 0 = typical normal, 1 = threshold

        persist, release = self.det["persist"], self.det["release"]
        if not self.state:
            self.above = self.above + 1 if ratio > 1.0 else 0
            if self.above >= persist:
                self.state, self.below = True, 0
        else:
            self.below = self.below + 1 if ratio < release else 0
            if self.below >= persist:
                self.state, self.above = False, 0

        z = (x - self.mspc.mu) / self.mspc.sd
        return {"status": "anomaly" if self.state else "normal", "ratio": float(ratio),
                "cause": explain(z, self.feats), "z": dict(zip(self.feats, z.round(2).tolist())),
                "in_alarm": self.state}
