"""Early warning for slow changes: per-sensor CUSUM, trend slope and time-to-alarm.

MSPC (the level channel) answers "is the water different now?". This module answers "is it moving
away, slowly, in one direction?". It never moves the baseline: an adaptive baseline would quietly
absorb a slow contamination, which is exactly what we want to catch.

CUSUM works on z = (x - mu) / sd from the MSPC model and is integrated over time (z * seconds), so the
same threshold `h` means the same thing at 1 s and 2.3 s sampling.
    S+ = max(0, S+ + (z - k) * dt)      S- = max(0, S- + (-z - k) * dt)
"""
from collections import deque

import numpy as np

K_DEFAULT = 0.5      # ignore steady deviations smaller than half a "meaningful change" (floor)
H_MIN = 8.0          # z*s; e.g. a 3-floor step signals in ~3 s, a 1-floor step in ~16 s


def cusum(z, dt, k):
    """z: (n, p) z-scores in time order. Returns S+ and S- arrays of shape (n, p)."""
    z = np.atleast_2d(np.asarray(z, float))
    sp, sn = np.zeros_like(z), np.zeros_like(z)
    a = np.zeros(z.shape[1]); b = np.zeros(z.shape[1])
    for i, row in enumerate(z):
        a = np.maximum(0.0, a + (row - k) * dt)
        b = np.maximum(0.0, b + (-row - k) * dt)
        sp[i], sn[i] = a, b
    return sp, sn


def calibrate_h(z_runs, k, margin=1.5, h_min=H_MIN):
    """Per-sensor h: the largest CUSUM seen on normal water times a margin (never below h_min).

    z_runs: list of (z array, dt) from normal runs, each scored against a model that did not see it
    when possible, so session-to-session offsets raise h instead of causing false drift warnings.
    """
    p = z_runs[0][0].shape[1]
    peak = np.zeros(p)
    for z, dt in z_runs:
        sp, sn = cusum(z, dt, k)
        peak = np.maximum(peak, np.maximum(sp.max(axis=0), sn.max(axis=0)))
    return np.maximum(peak * margin, h_min)


def drift_flags(z, dt, k, h):
    """Boolean per row: any sensor's CUSUM above its h; plus which sensor/direction drives it."""
    sp, sn = cusum(z, dt, k)
    over = np.maximum(sp / h, sn / h)
    flag = (over > 1.0).any(axis=1)
    return flag, sp, sn


def theil_sen(t, y):
    """Robust slope (units per second): median of pairwise slopes, on at most 40 points."""
    t, y = np.asarray(t, float), np.asarray(y, float)
    if len(t) > 40:
        idx = np.linspace(0, len(t) - 1, 40).astype(int)
        t, y = t[idx], y[idx]
    if len(t) < 5 or t[-1] - t[0] < 10:
        return 0.0
    i, j = np.triu_indices(len(t), 1)
    dtt = t[j] - t[i]
    ok = dtt > 0
    return float(np.median((y[j] - y[i])[ok] / dtt[ok])) if ok.any() else 0.0


def change_points(y, max_cp=3, min_seg=8):
    """Offline binary segmentation on a score series (mean shifts). Returns sorted indices."""
    y = np.asarray(y, float)
    n = len(y)
    if n < 2 * min_seg:
        return []
    var = np.var(np.diff(y)) / 2 or 1e-12             # noise level robust to the shifts themselves
    penalty = 2.0 * np.log(n) * var * 4

    def best_split(a, b):
        seg = y[a:b]
        cs = np.cumsum(seg); tot = cs[-1]; m = len(seg)
        best, at = 0.0, None
        for s in range(min_seg, m - min_seg):
            left, right = cs[s - 1], tot - cs[s - 1]
            gain = left ** 2 / s + right ** 2 / (m - s) - tot ** 2 / m
            if gain > best:
                best, at = gain, a + s
        return best, at

    cps, segs = [], [(0, n)]
    while segs and len(cps) < max_cp:
        cands = [(best_split(a, b), (a, b)) for a, b in segs]
        (gain, at), (a, b) = max(cands, key=lambda c: c[0][0])
        if at is None or gain < penalty:
            break
        cps.append(at)
        segs.remove((a, b))
        segs += [(a, at), (at, b)]
    return sorted(cps)


class DriftTracker:
    """Live CUSUM + 60 s trend per sensor."""

    def __init__(self, k, h, window_s=60):
        self.k, self.h = float(k), np.asarray(h, float)
        self.window_s = window_s
        self.reset()

    def reset(self):
        self.clear()
        self.hist = deque()     # (t, x)

    def clear(self):
        """Forget accumulated evidence (used while the level alarm is on: its job is already done)."""
        self.sp = np.zeros(len(self.h)); self.sn = np.zeros(len(self.h))

    def update(self, t, x, z, dt):
        # capped at 2h so the warning clears within seconds once the water is back to normal
        self.sp = np.minimum(np.maximum(0.0, self.sp + (z - self.k) * dt), 2 * self.h)
        self.sn = np.minimum(np.maximum(0.0, self.sn + (-z - self.k) * dt), 2 * self.h)
        self.hist.append((t, x))
        while self.hist and t - self.hist[0][0] > self.window_s:
            self.hist.popleft()
        level = np.maximum(self.sp, self.sn) / self.h
        return level

    def slopes(self):
        if len(self.hist) < 5:
            return np.zeros(len(self.h))
        t = np.array([a for a, _ in self.hist]); X = np.array([b for _, b in self.hist])
        return np.array([theil_sen(t, X[:, i]) for i in range(X.shape[1])])
