"""Anomaly detectors and the shared scoring pipeline (smoothing, threshold, persistence)."""
import math
import time

import numpy as np


class Standardizer:
    """Robust centre (median) and scale (MAD), never smaller than the sensor floor."""

    def fit(self, X, floors):
        self.c = np.median(X, axis=0)
        mad = np.median(np.abs(X - self.c), axis=0) * 1.4826
        self.s = np.maximum(mad, floors)
        return self

    def transform(self, X):
        return (X - self.c) / self.s


def fit_gaussian(X, floors):
    """Trimmed mean/covariance with a per-sensor variance floor (always invertible)."""
    z = Standardizer().fit(X, floors).transform(X)
    keep = (np.abs(z) < 5).all(axis=1)
    Xk = X[keep] if keep.sum() >= max(10, X.shape[1] + 2) else X
    mu = Xk.mean(axis=0)
    cov = np.atleast_2d(np.cov(Xk, rowvar=False))
    cov = cov + np.diag(np.maximum(0.0, floors ** 2 - np.diag(cov)))
    return mu, cov


class Params:
    """Hyperparameters from config.json -> model_params.<key> (unknown keys are ignored)."""

    def __init__(self, **params):
        self.params = params

    def p(self, name, default):
        return self.params.get(name, default) if hasattr(self, "params") else default


class MSPC(Params):
    key, name, ewma = "mspc", "MSPC (Robust Mahalanobis + EWMA)", True

    def fit(self, X, floors):
        self.mu, self.cov = fit_gaussian(X, floors)
        self.P = np.linalg.inv(self.cov)
        self.sd = np.sqrt(np.diag(self.cov))
        return self

    def score(self, X):
        d = X - self.mu
        return np.einsum("ij,jk,ik->i", d, self.P, d)


class PCAMonitor(Params):
    key, name, ewma = "pca", "PCA (Hotelling T² + Q)", False

    def fit(self, X, floors):
        from sklearn.decomposition import PCA
        self.st = Standardizer().fit(X, floors)
        Z = self.st.transform(X)
        p = X.shape[1]
        ratio = PCA().fit(Z).explained_variance_ratio_
        k = int(np.clip(np.searchsorted(np.cumsum(ratio), float(self.p("var_target", 0.90))) + 1, 1, p - 1))
        self.pca = PCA(n_components=k).fit(Z)
        self.eig = np.maximum(self.pca.explained_variance_, 1e-9)
        t2, q = self._tq(Z)
        self.t2u = max(float(np.quantile(t2, 0.99)), 1e-9)
        self.qu = max(float(np.quantile(q, 0.99)), 1e-9)
        return self

    def _tq(self, Z):
        S = self.pca.transform(Z)
        t2 = (S ** 2 / self.eig).sum(axis=1)
        R = Z - self.pca.inverse_transform(S)
        return t2, (R ** 2).sum(axis=1)

    def score(self, X):
        t2, q = self._tq(self.st.transform(X))
        return np.maximum(t2 / self.t2u, q / self.qu)


class SklearnNovelty(Params):
    ewma = False

    def fit(self, X, floors):
        self.st = Standardizer().fit(X, floors)
        Z = self.st.transform(X)
        if len(Z) > 3000:
            Z = Z[np.random.default_rng(0).choice(len(Z), 3000, replace=False)]
        self.est = self.make(len(Z)).fit(Z)
        return self

    def score(self, X):
        return -self.raw(self.st.transform(X))


class IForest(SklearnNovelty):
    key, name = "iforest", "Isolation Forest"

    def make(self, n):
        from sklearn.ensemble import IsolationForest
        return IsolationForest(n_estimators=int(self.p("n_estimators", 200)), random_state=0)

    def raw(self, Z):
        return self.est.score_samples(Z)


class OCSVM(SklearnNovelty):
    key, name = "ocsvm", "One-Class SVM (RBF)"

    def make(self, n):
        from sklearn.svm import OneClassSVM
        g = self.p("gamma", "scale")
        return OneClassSVM(nu=float(self.p("nu", 0.01)), gamma=g if g in ("scale", "auto") else float(g))

    def raw(self, Z):
        return self.est.decision_function(Z)


class LOF(SklearnNovelty):
    key, name = "lof", "Local Outlier Factor"

    def make(self, n):
        from sklearn.neighbors import LocalOutlierFactor
        return LocalOutlierFactor(n_neighbors=max(2, min(int(self.p("n_neighbors", 20)), n - 1)), novelty=True)

    def raw(self, Z):
        return self.est.score_samples(Z)


class Threshold(Params):
    """Baseline: the classic one-sensor-at-a-time limit (largest robust |z| over the sensors)."""
    key, name, ewma = "threshold", "Simple threshold (baseline)", False

    def fit(self, X, floors):
        self.st = Standardizer().fit(X, floors)
        return self

    def score(self, X):
        return np.abs(self.st.transform(X)).max(axis=1)


MODELS = [MSPC, PCAMonitor, IForest, OCSVM, LOF, Threshold]


MODEL_NAMES = {m.key: m.name for m in MODELS}


def timing(det, dt):
    """Turn time-based detector settings into per-sample values for a sample interval `dt` seconds.

    median_s / persist_s are seconds and ewma_lambda is defined for 1-second sampling, so the
    detector behaves the same whether the firmware sends a sample every 1 s or every 2.3 s.
    """
    dt = min(max(float(dt or 1.0), 0.05), 30.0)
    lam1 = float(det.get("ewma_lambda", 0.3))
    win = max(1, int(round(det.get("median_s", 5) / dt)))
    if win >= 2 and win % 2 == 0:
        win += 1          # an even median window averages the step it should reject
    persist = max(1, math.ceil(det.get("persist_s", 3) / dt - 1e-9))   # never shorter than asked
    return {"dt": dt, "median_win": win, "persist": persist, "lam": 1.0 - (1.0 - lam1) ** dt}


def ewma(x, lam):
    out = np.empty(len(x))
    e = None
    for i, v in enumerate(x):
        e = v if e is None else lam * v + (1 - lam) * e
        out[i] = e
    return out


def alarm_states(e, thr, persist, release):
    out = np.zeros(len(e), bool)
    state, above, below = False, 0, 0
    for i, v in enumerate(e):
        if not state:
            above = above + 1 if v > thr else 0
            if above >= persist:
                state, below = True, 0
        else:
            below = below + 1 if v < thr * release else 0
            if below >= persist:
                state, above = False, 0
        out[i] = state
    return out


def alarms(ratio, det, dt):
    """Persistence + hysteresis on a normalised score (threshold = 1)."""
    return alarm_states(ratio, 1.0, timing(det, dt)["persist"], det.get("release", 0.8))


def smoothed_score(model, X, det, dt=1.0):
    s = model.score(X)
    return ewma(s, timing(det, dt)["lam"]) if model.ewma else s


def fit_calibrated(cls, groups, floors, det, params=None):
    """Fit on every normal run; set the threshold from scores on data the model did NOT see.

    `groups` is a list of groups, each a list of (X, dt) runs. A group is one session (one batch of
    water) when sessions are known, otherwise one run. With 3+ groups each group is scored by a model
    trained on the others (cross-validated threshold), so the threshold includes cup-to-cup variation.
    With fewer groups it falls back to in-sample. The threshold is placed `margin` times beyond the
    reference quantile, measured from the median, which works for any score scale.
    """
    t0 = time.perf_counter()
    params = params or {}
    m = cls(**params).fit(np.vstack([X for g in groups for X, _ in g]), floors)
    fit_ms = (time.perf_counter() - t0) * 1000
    ref = []
    if len(groups) >= 3:
        for j in range(len(groups)):
            mj = cls(**params).fit(np.vstack([X for i, g in enumerate(groups) if i != j for X, _ in g]), floors)
            ref += [smoothed_score(mj, X, det, d) for X, d in groups[j]]
    else:
        ref = [smoothed_score(m, X, det, d) for g in groups for X, d in g]
    ref = np.concatenate(ref)
    med = float(np.median(ref))
    qv = float(np.quantile(ref, det["quantile"]))
    thr = med + det["margin"] * (qv - med)
    if cls is MSPC:
        thr = max(thr, det["min_sigma"] ** 2)
    if thr - med < 1e-9:
        thr = med + 1e-6
    return m, thr, med, fit_ms


def normalized(model, X, thr, med, det, dt=1.0):
    """Score rescaled so 0 = typical normal and 1 = alarm threshold, comparable across models."""
    return (smoothed_score(model, X, det, dt) - med) / (thr - med)
