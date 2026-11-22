"""Hyperparameter tuning: fast what-if previews, presets, suggested floors and validation.

A preview evaluates one model with candidate settings exactly like training does, without saving.
It is split in two stages so sliders feel instant:
  stage 1 (slow, cached): load runs, median filter, fit fold models, raw scores
          depends on floors, median_s, warmup_s, model hyperparameters
  stage 2 (fast): EWMA, threshold quantile/margin, persistence, release, CUSUM -> metrics
          depends on ewma_lambda, quantile, margin, min_sigma, persist_s, release, cusum_*
Tuning on the same runs used for evaluation is optimistic: the UI says so.
"""
import copy
import hashlib
import json
import pathlib
import threading

import numpy as np

from ..config import DATA_DIR, DEFAULT_CONFIG, deep_merge
from ..storage.datasets import selection_from_cfg, selection_use_ph
from .drift import calibrate_h, drift_flags
from ..acquisition.features import feature_list
from .models import MODELS, MSPC, alarms, ewma, timing
from .training import TrainError, _selected_files, detection_limits, load_runs, rising_edges, split_runs

CLASSES = {m.key: m for m in MODELS}
STAGE1_DETECTOR = ("median_s", "warmup_s")
_cache, _lock = {}, threading.Lock()

PRESETS = {
    "sensitive": {"name": "Sensitive (detects fast, more false alarms)",
                  "detector": {"margin": 1.1, "persist_s": 2, "quantile": 0.99, "min_sigma": 2.5, "cusum_h_min": 5}},
    "balanced": {"name": "Balanced (default)",
                 "detector": {k: DEFAULT_CONFIG["detector"][k] for k in
                              ("margin", "persist_s", "quantile", "min_sigma", "cusum_h_min")}},
    "conservative": {"name": "Conservative (few false alarms, slower detection)",
                     "detector": {"margin": 1.6, "persist_s": 5, "quantile": 0.999, "min_sigma": 4, "cusum_h_min": 15}},
}

RANGES = {
    ("detector", "margin"): (1.0, 5.0), ("detector", "persist_s"): (0.5, 60), ("detector", "release"): (0.3, 0.99),
    ("detector", "ewma_lambda"): (0.01, 1.0), ("detector", "quantile"): (0.9, 0.9999),
    ("detector", "min_sigma"): (0.5, 10), ("detector", "median_s"): (0, 60), ("detector", "warmup_s"): (0, 600),
    ("detector", "cusum_k"): (0.05, 5), ("detector", "cusum_h_min"): (0.5, 200), ("detector", "cusum_margin"): (1.0, 5),
    ("floors", "temp_c"): (0.05, 20), ("floors", "tds_ppm"): (0.5, 500), ("floors", "turb_v"): (0.001, 1),
    ("floors", "ph"): (0.01, 3),
}
PARAM_RANGES = {("pca", "var_target"): (0.5, 0.999), ("iforest", "n_estimators"): (10, 1000),
                ("ocsvm", "nu"): (0.001, 0.5), ("lof", "n_neighbors"): (2, 100)}
RESOLUTION = {"temp_c": 0.0625, "tds_ppm": 1.0, "turb_v": 0.002, "ph": 0.01}


def validate(patch):
    """Raise ValueError (Thai) when a tunable value is out of range. Returns the patch unchanged."""
    for (grp, key), (lo, hi) in RANGES.items():
        v = (patch.get(grp) or {}).get(key)
        if v is None:
            continue
        if not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{grp}.{key} must be between {lo} and {hi}")
    for (model, key), (lo, hi) in PARAM_RANGES.items():
        v = ((patch.get("model_params") or {}).get(model) or {}).get(key)
        if v is None:
            continue
        if not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{model}.{key} must be between {lo} and {hi}")
    g = ((patch.get("model_params") or {}).get("ocsvm") or {}).get("gamma")
    if g is not None and g not in ("scale", "auto") and not (isinstance(g, (int, float)) and g > 0):
        raise ValueError('ocsvm.gamma must be "scale", "auto" or a positive number')
    return patch


def _stage1_key(cfg, use_ph, key, data_dir):
    h = hashlib.sha1()
    sel = selection_from_cfg(cfg)               # tune the data the model was trained on, not "everything"
    for p in _selected_files(data_dir, sel):
        st = p.stat()
        h.update(f"{p.name}|{st.st_size}|{st.st_mtime_ns}".encode())
        j = p.with_suffix(".json")
        if j.exists():
            h.update(j.read_bytes())
    sel = {"selection": sel, "use_ph": use_ph, "key": key, "floors": cfg["floors"], "calib": cfg["calib"],
           "ph_divider": cfg["ph_divider"], "limits": cfg["limits"], "gap": cfg.get("session_gap_min"),
           "det": {k: cfg["detector"].get(k) for k in STAGE1_DETECTOR},
           "params": cfg.get("model_params", {}).get(key, {})}
    h.update(json.dumps(sel, sort_keys=True, default=str).encode())
    return h.hexdigest()


def _stage1(cfg, use_ph, key, data_dir):
    ck = _stage1_key(cfg, use_ph, key, data_dir)
    with _lock:
        if ck in _cache:
            return _cache[ck]
    cls = CLASSES[key]
    params = cfg.get("model_params", {}).get(key, {})
    sel = selection_from_cfg(cfg)
    use_ph = selection_use_ph(sel, use_ph)
    feats = feature_list(use_ph, sel["sensors"] if sel else None)
    floors = np.array([cfg["floors"][f] for f in feats], float)
    runs, _ = load_runs(cfg, use_ph, data_dir, sel)
    sp = split_runs(runs, feats)
    if not sp["normal"]:
        raise TrainError("No usable normal runs yet")

    def fit(groups):
        return cls(**params).fit(np.vstack([X for g in groups for X, _ in g]), floors)

    def raw_ref(groups):
        """Raw scores of each normal run from a model that did not see its group (or in-sample)."""
        out, zs = [], []
        if len(groups) >= 3:
            for j in range(len(groups)):
                mj = fit([g for i, g in enumerate(groups) if i != j])
                out += [(mj.score(X), d) for X, d in groups[j]]
                if cls is MSPC:
                    zs += [((X - mj.mu) / mj.sd, d) for X, d in groups[j]]
        else:
            m = fit(groups)
            out = [(m.score(X), d) for g in groups for X, d in g]
            if cls is MSPC:
                zs = [((X - m.mu) / m.sd, d) for g in groups for X, d in g]
        return out, zs

    folds = []
    for train_groups, held in sp["folds"]:
        m = fit(train_groups)
        ref, zref = raw_ref(train_groups)
        f = {"ref": ref, "zref": zref,
             "held": [(m.score(X), d, ((X - m.mu) / m.sd) if cls is MSPC else None) for X, d in held],
             "anom": []}
        for r in sp["anomaly"]:
            rid = r["run_id"]
            X, d = sp["X"][rid], sp["DT"][rid]
            f["anom"].append((m.score(X), d, sp["T"][rid], sp["TD"][rid],
                              ((X - m.mu) / m.sd) if cls is MSPC else None))
        folds.append(f)
    all_groups = [sp["groups"][g] for g in sp["gkeys"]]
    final = fit(all_groups)
    ref_final, _ = raw_ref(all_groups)
    center = MSPC().fit(np.vstack([X for g in all_groups for X, _ in g]), floors).mu
    st = {"key": key, "feats": feats, "floors": floors, "folds": folds, "final": final,
          "ref_final": ref_final, "center": center, "protocol": sp["protocol"],
          "n_normal": len(sp["normal"]), "n_anomaly": len(sp["anomaly"]), "n_sessions": len(sp["sessions"])}
    with _lock:
        if len(_cache) > 12:
            _cache.clear()
        _cache[ck] = st
    return st


def _threshold(ref, det, is_mspc, margin=None):
    lam = lambda d: timing(det, d)["lam"]
    smooth = np.concatenate([ewma(s, lam(d)) if is_mspc else s for s, d in ref])
    med = float(np.median(smooth))
    qv = float(np.quantile(smooth, det["quantile"]))
    thr = med + (det["margin"] if margin is None else margin) * (qv - med)
    if is_mspc:
        thr = max(thr, det["min_sigma"] ** 2)
    return (thr if thr - med > 1e-9 else med + 1e-6), med


def _stage2(st, det, margin=None, with_drift=True):
    is_mspc = st["key"] == "mspc"
    lam = lambda d: timing(det, d)["lam"]
    sm = lambda s, d: ewma(s, lam(d)) if is_mspc else s
    fa_hits = fa_n = det_hits = det_n = 0
    delays, caught = [], 0
    drift_edges, drift_hours, leads = 0, 0.0, []
    for f in st["folds"]:
        thr, med = _threshold(f["ref"], det, is_mspc, margin)
        h = None
        if is_mspc and with_drift and f["zref"]:
            h = calibrate_h(f["zref"], det.get("cusum_k", 0.5), det.get("cusum_margin", 1.5), det.get("cusum_h_min", 8.0))
        for s, d, z in f["held"]:
            a = alarms((sm(s, d) - med) / (thr - med), det, d)
            fa_hits += int(a.sum()); fa_n += len(a)
            if h is not None:
                fl, _, _ = drift_flags(z, d, det.get("cusum_k", 0.5), h)
                drift_edges += rising_edges(fl); drift_hours += len(s) * d / 3600
        for s, d, t, td, z in f["anom"]:
            a = alarms((sm(s, d) - med) / (thr - med), det, d)
            post = np.ones(len(a), bool) if td is None else t >= td
            fa_hits += int(a[~post].sum()); fa_n += int((~post).sum())
            det_hits += int(a[post].sum()); det_n += int(post.sum())
            hit = a & post
            t0 = td or 0.0
            if hit.any():
                caught += 1
                delays.append(float(t[np.argmax(hit)] - t0))
                if h is not None:
                    fl, _, _ = drift_flags(z, d, det.get("cusum_k", 0.5), h)
                    dh = fl & post
                    if dh.any():
                        leads.append(float(t[np.argmax(hit)] - t[np.argmax(dh)]))
    nf = max(1, len(st["folds"]))
    far = fa_hits / fa_n if fa_n else None
    dr = det_hits / det_n if det_n else None
    return {"false_alarm": far, "detection": dr,
            "balanced_acc": None if far is None or dr is None else (1 - far + dr) / 2,
            "delay_s": float(np.median(delays)) if delays else None,
            "runs_caught": round(caught / nf, 2), "n_anomaly": st["n_anomaly"],
            "drift_false_per_hour": (drift_edges / drift_hours) if drift_hours else None,
            "drift_lead_s": float(np.median(leads)) if leads else None}


def preview(cfg, use_ph, key, settings, data_dir=DATA_DIR):
    """Metrics for the current config and for `settings` merged on top, plus a margin sweep."""
    if key not in CLASSES:
        raise ValueError("Unknown model")
    validate(settings)
    cand_cfg = deep_merge(cfg, {k: v for k, v in settings.items() if k in ("detector", "floors", "model_params")})
    out = {"model": key}
    for name, c in (("current", cfg), ("candidate", cand_cfg)):
        st = _stage1(c, use_ph, key, data_dir)
        det = c["detector"]
        res = _stage2(st, det)
        thr, _ = _threshold(st["ref_final"], det, key == "mspc")
        res["limits"] = detection_limits(st["final"], thr, st["center"], st["floors"], st["feats"])
        out[name] = res
        if name == "candidate":
            margins = np.round(np.linspace(1.0, 3.0, 11), 2)
            out["sweep"] = [{"margin": float(m), **{k: v for k, v in _stage2(st, det, m, with_drift=False).items()
                                                     if k in ("false_alarm", "detection")}} for m in margins]
            out["protocol"] = st["protocol"]
            out["features"] = st["feats"]
            out["n_sessions"] = st["n_sessions"]
    return out


def suggest_floors(cfg, use_ph, data_dir=DATA_DIR):
    """floor = max(sensor resolution, 3 x sd of the per-session medians of normal water)."""
    sel = selection_from_cfg(cfg)
    use_ph = selection_use_ph(sel, use_ph)
    feats = feature_list(use_ph, sel["sensors"] if sel else None)
    c = copy.deepcopy(cfg)
    runs, _ = load_runs(c, use_ph, data_dir, sel)
    normal = [r for r in runs if r["label"] == "normal"]
    sessions = sorted({r["session"] for r in normal})
    if len(sessions) < 3:
        raise ValueError(f"At least 3 normal-water sessions are needed (currently {len(sessions)})")
    med = []
    for s in sessions:
        X = np.vstack([r["df"][feats].to_numpy(float) for r in normal if r["session"] == s])
        med.append(np.median(X, axis=0))
    spread = np.std(np.array(med), axis=0, ddof=1)
    out = {}
    for i, f in enumerate(feats):
        v = max(RESOLUTION[f], 3 * float(spread[i]))
        out[f] = float(f"{v:.3g}")
    return {"floors": out, "current": {f: cfg["floors"][f] for f in feats}, "n_sessions": len(sessions),
            "session_spread": {f: float(f"{spread[i]:.3g}") for i, f in enumerate(feats)}}


def load_history(out_dir):
    try:
        return json.loads((pathlib.Path(out_dir) / "history.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
