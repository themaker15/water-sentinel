"""Anomaly detectors and the shared scoring pipeline (smoothing, threshold, persistence)."""
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


class MSPC:
    key, name, ewma = "mspc", "MSPC (Robust Mahalanobis + EWMA)", True

    def fit(self, X, floors):
        self.mu, self.cov = fit_gaussian(X, floors)
        self.P = np.linalg.inv(self.cov)
        self.sd = np.sqrt(np.diag(self.cov))
        return self

    def score(self, X):
        d = X - self.mu
        return np.einsum("ij,jk,ik->i", d, self.P, d)


class PCAMonitor:
    key, name, ewma = "pca", "PCA (Hotelling T² + Q)", False

    def fit(self, X, floors):
        from sklearn.decomposition import PCA
        self.st = Standardizer().fit(X, floors)
        Z = self.st.transform(X)
        p = X.shape[1]
        ratio = PCA().fit(Z).explained_variance_ratio_
        k = int(np.clip(np.searchsorted(np.cumsum(ratio), 0.90) + 1, 1, p - 1))
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


class SklearnNovelty:
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
        return IsolationForest(n_estimators=200, random_state=0)

    def raw(self, Z):
        return self.est.score_samples(Z)


class OCSVM(SklearnNovelty):
    key, name = "ocsvm", "One-Class SVM (RBF)"

    def make(self, n):
        from sklearn.svm import OneClassSVM
        return OneClassSVM(nu=0.01, gamma="scale")

    def raw(self, Z):
        return self.est.decision_function(Z)


class LOF(SklearnNovelty):
    key, name = "lof", "Local Outlier Factor"

    def make(self, n):
        from sklearn.neighbors import LocalOutlierFactor
        return LocalOutlierFactor(n_neighbors=max(2, min(20, n - 1)), novelty=True)

    def raw(self, Z):
        return self.est.score_samples(Z)


MODELS = [MSPC, PCAMonitor, IForest, OCSVM, LOF]


MODEL_NAMES = {m.key: m.name for m in MODELS}


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


def smoothed_score(model, X, det):
    s = model.score(X)
    return ewma(s, det["ewma_lambda"]) if model.ewma else s


def fit_calibrated(cls, parts, floors, det):
    """Fit on all normal runs in `parts`; set the threshold from scores on runs the model did NOT see.

    With 3+ runs each run is scored by a model trained on the others (cross-validated threshold),
    so the threshold includes cup-to-cup variation. With fewer runs it falls back to in-sample.
    The threshold is placed `margin` times beyond the reference quantile, measured from the median,
    which works for any score scale (positive, negative or bounded).
    """
    t0 = time.perf_counter()
    m = cls().fit(np.vstack(parts), floors)
    fit_ms = (time.perf_counter() - t0) * 1000
    if len(parts) >= 3:
        ref = []
        for j in range(len(parts)):
            mj = cls().fit(np.vstack([p for i, p in enumerate(parts) if i != j]), floors)
            ref.append(smoothed_score(mj, parts[j], det))
        ref = np.concatenate(ref)
    else:
        ref = smoothed_score(m, np.vstack(parts), det)
    med = float(np.median(ref))
    qv = float(np.quantile(ref, det["quantile"]))
    thr = med + det["margin"] * (qv - med)
    if cls is MSPC:
        thr = max(thr, det["min_sigma"] ** 2)
    if thr - med < 1e-9:
        thr = med + 1e-6
    return m, thr, med, fit_ms


def normalized(model, X, thr, med, det):
    """Score rescaled so 0 = typical normal and 1 = alarm threshold, comparable across models."""
    return (smoothed_score(model, X, det) - med) / (thr - med)
