"""Training, model comparison (evaluation) and 2-D embeddings for visualization."""
import hashlib
import json
import pathlib
import time

import numpy as np
import pandas as pd

from . import causes
from ..config import DATA_DIR, MODEL_DIR
from ..storage.datasets import normalize_selection, role_of, selection_use_ph
from ..acquisition.features import SENSORS, feature_list, prepare_run, read_run
from .drift import H_MIN, K_DEFAULT, calibrate_h, change_points, drift_flags
from .models import MODELS, MODEL_NAMES, MSPC, alarms, fit_calibrated, normalized
from ..storage.runs import UNLABELED, list_runs


class TrainError(Exception):
    pass


def _selected_files(data_dir, selection):
    """CSV files that take part in training under a selection (all of them for the default)."""
    files = sorted(pathlib.Path(data_dir).glob("*.csv"))
    if selection and selection.get("files") is not None:
        files = [p for p in files if selection["files"].get(p.name, "exclude") != "exclude"]
    return files


def data_fingerprint(cfg, use_ph, data_dir=DATA_DIR, selection=None):
    """Hash of every input that changes training: data files, sidecars, roles and the relevant settings."""
    selection = normalize_selection(selection)
    h = hashlib.sha1()
    for p in _selected_files(data_dir, selection):
        st = p.stat()
        h.update(f"{p.name}|{st.st_size}|{st.st_mtime_ns}".encode())
        j = p.with_suffix(".json")
        if j.exists():
            h.update(j.read_bytes())
    keys = {k: cfg[k] for k in ("calib", "ph_divider", "limits", "floors", "detector", "session_gap_min")}
    keys["model_params"] = cfg.get("model_params", {})
    h.update(json.dumps({"use_ph": bool(use_ph), **keys, "selection": selection}, sort_keys=True).encode())
    return h.hexdigest()[:16]


def content_hash(files, data_dir=DATA_DIR):
    """Hash of the *contents* of the trained files (not timestamps): proves which data a result came from."""
    h = hashlib.sha1()
    for name in sorted(files):
        p = pathlib.Path(data_dir) / name
        h.update(name.encode())
        h.update(p.read_bytes())
        j = p.with_suffix(".json")
        if j.exists():
            h.update(j.read_bytes())
    return h.hexdigest()[:16]


def load_runs(cfg, use_ph, data_dir=DATA_DIR, selection=None):
    selection = normalize_selection(selection)
    sensors = selection["sensors"] if selection else None
    feats = feature_list(use_ph, sensors)
    ph_in = "ph" in feats
    runs, skipped = [], []
    info = {r["file"]: r for r in list_runs(data_dir, cfg.get("session_gap_min", 10))}
    for p in sorted(pathlib.Path(data_dir).glob("*.csv")):
        meta = info.get(p.name, {})
        if meta.get("excluded"):
            skipped.append({"file": p.name, "reason": "Set as not used for training"})
            continue
        if selection and selection["files"] is not None and selection["files"].get(p.name, "exclude") == "exclude":
            skipped.append({"file": p.name, "reason": "Not selected in this dataset", "quiet": True})
            continue
        try:
            raw = read_run(p)
        except Exception as e:  # unreadable file
            skipped.append({"file": p.name, "reason": f"Cannot read file: {e}"})
            continue
        if raw.empty:
            skipped.append({"file": p.name, "reason": "Empty file (header only)"})
            continue
        label = str(raw["label"].iloc[0])
        if label == UNLABELED:
            skipped.append({"file": p.name, "reason": "No condition label yet (unlabeled)"})
            continue
        role = role_of(selection, p.name, label)
        if (label == "normal") != (role in ("train", "test")):
            skipped.append({"file": p.name, "reason": f"role '{role}' Does not match the condition filter '{label}'"})
            continue
        if ph_in and ("ph_pin_v" not in raw or not np.isfinite(pd.to_numeric(raw["ph_pin_v"], errors="coerce")).any()):
            skipped.append({"file": p.name, "reason": "No pH data (recorded before pH was enabled)"})
            continue
        clean, faults, dt = prepare_run(raw, cfg, ph_in, warmup=(label == "normal"), sensors=sensors)
        if len(clean) < 10:
            why = "Fewer than 10 rows left after trimming the start / sensor errors"
            if faults:
                why += f" (found {', '.join(faults)})"
            skipped.append({"file": p.name, "reason": why})
            continue
        nfault = int(sum(faults.values()))
        runs.append({"run_id": int(raw["run_id"].iloc[0]), "label": label, "file": p.name, "role": role,
                     "session": meta.get("session", p.stem), "dt": dt, "markers": meta.get("markers", []),
                     "df": clean, "faults": nfault, "fault_types": faults,
                     "fault_frac": nfault / max(1, nfault + len(clean)), "meta_calib": meta.get("calib")})
    ids = [r["run_id"] for r in runs]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        files = [r["file"] for r in runs if r["run_id"] in dup]
        raise TrainError(f"Run number {dup} is used by several files: {files}. Move the unused files out of the data folder")
    return runs, skipped


def check_selection(cfg, use_ph, selection=None, data_dir=DATA_DIR):
    """Guard rails before training: blockers stop the button, warnings are shown but allowed."""
    selection = normalize_selection(selection)
    use_ph = selection_use_ph(selection, use_ph)
    blockers, warnings = [], []
    try:
        runs, skipped = load_runs(cfg, use_ph, data_dir, selection)
    except TrainError as e:
        return {"ok": False, "blockers": [str(e)], "warnings": [], "counts": {}}
    train_ = [r for r in runs if r["role"] == "train"]
    test_ = [r for r in runs if r["role"] == "test"]
    anom = [r for r in runs if r["role"] == "anomaly"]
    sess = sorted({r["session"] for r in train_})
    if not train_:
        blockers.append("No normal run selected as train: tick at least 1 normal-water run")
    elif len(sess) < 3:
        warnings.append(f"Train runs come from {len(sess)} sessions ({len(sess)} batches of water): false-alarm results will look too good. At least 3 sessions recommended")
    if train_ and not anom:
        warnings.append("No anomaly runs selected: models can only be compared on false alarms")
    ph_bad = [s["file"] for s in skipped if "No pH data" in s["reason"]]
    if ph_bad:
        warnings.append(f"pH is selected but {len(ph_bad)} runs have no pH data, so they are skipped: {', '.join(ph_bad[:4])}{' ...' if len(ph_bad) > 4 else ''}")
    for r in runs:
        if r["fault_frac"] > 0.10:
            warnings.append(f"Run {r['run_id']} ({r['file']}) has sensor errors in {r['fault_frac'] * 100:.0f}% of rows")
    cal = {tuple(sorted((k, str(v)) for k, v in (r["meta_calib"] or {}).items())) for r in runs if r["meta_calib"]}
    if len(cal) > 1:
        warnings.append("The selected runs were recorded with different calibrations: values are recomputed from raw voltages with the current calibration. "
                        "If the probe or voltage divider changed between runs, select only runs recorded with the same hardware")
    if train_ and any(r["meta_calib"] and r["meta_calib"].get("ph_divider") != cfg["ph_divider"] for r in runs) and "ph" in feature_list(use_ph, selection["sensors"] if selection else None):
        warnings.append("The pH divider ratio at recording time differs from the current one: check Settings")
    return {"ok": not blockers, "blockers": blockers, "warnings": warnings,
            "counts": {"train": len(train_), "test": len(test_), "anomaly": len(anom), "sessions": len(sess),
                       "skipped": len([s for s in skipped if not s.get("quiet")])}}


def roc_points(neg, pos, n=80):
    if len(neg) == 0 or len(pos) == 0:
        return None, None
    allv = np.concatenate([neg, pos])
    ths = np.unique(np.quantile(allv, np.linspace(0, 1, n)))[::-1]
    fpr = [0.0] + [float((neg >= t).mean()) for t in ths] + [1.0]
    tpr = [0.0] + [float((pos >= t).mean()) for t in ths] + [1.0]
    # AUC by rank comparison (Mann-Whitney), exact and threshold-free
    order = np.argsort(allv, kind="mergesort")
    ranks = np.empty(len(allv))
    ranks[order] = np.arange(1, len(allv) + 1)
    _, inv, counts = np.unique(allv, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=ranks)
    ranks = (sums / counts)[inv]
    auc = (ranks[len(neg):].sum() - len(pos) * (len(pos) + 1) / 2) / (len(neg) * len(pos))
    return {"fpr": fpr, "tpr": tpr}, float(auc)


def explain(z, feats, top=2):
    order = np.argsort(-np.abs(z))[:top]
    out = []
    for i in order:
        f = feats[i]
        if abs(z[i]) < 1.5:
            continue
        info = SENSORS[f]
        out.append({"sensor": f, "name": info["name"], "z": round(float(z[i]), 2),
                    "text": info["up"] if z[i] > 0 else info["down"]})
    return out


def downsample(arr, n=300):
    arr = np.asarray(arr)
    if len(arr) <= n:
        return arr
    idx = np.linspace(0, len(arr) - 1, n).astype(int)
    return arr[idx]


def dose_time(markers):
    """Time (s from run start) of the first marker that added something, or None."""
    ts = [m["t_s"] for m in markers or [] if m.get("kind") == "dose" or m.get("dose") is not None]
    return min(ts) if ts else None


def dose_at(markers, t):
    """Cumulative numeric dose added up to time t (None when no marker has a number)."""
    doses = [(m["t_s"], m["dose"]) for m in markers or [] if isinstance(m.get("dose"), (int, float))]
    if not doses:
        return None
    return float(sum(d for ts, d in doses if ts <= t))


def drift_h(groups, floors, det):
    """CUSUM thresholds from normal water the model did not see (leave one group out when possible)."""
    k = det.get("cusum_k", K_DEFAULT)
    z_runs = []
    if len(groups) >= 3:
        for j in range(len(groups)):
            m = MSPC().fit(np.vstack([X for i, g in enumerate(groups) if i != j for X, _ in g]), floors)
            z_runs += [((X - m.mu) / m.sd, d) for X, d in groups[j]]
    else:
        m = MSPC().fit(np.vstack([X for g in groups for X, _ in g]), floors)
        z_runs = [((X - m.mu) / m.sd, d) for g in groups for X, d in g]
    return calibrate_h(z_runs, k, det.get("cusum_margin", 1.5), det.get("cusum_h_min", H_MIN))


def rising_edges(flag):
    flag = np.asarray(flag, bool)
    return int(flag[0]) + int(np.sum(flag[1:] & ~flag[:-1])) if len(flag) else 0


def split_runs(runs, feats):
    """Arrays per run plus evaluation folds. Groups = independent batches of water: leave one session
    out when there are 3+ sessions, otherwise one run, and with a single run a 70/30 time split."""
    normal = [r for r in runs if r["label"] == "normal" and r.get("role", "train") == "train"]
    test = [r for r in runs if r["label"] == "normal" and r.get("role") == "test"]   # held out, never fitted
    anomaly = [r for r in runs if r["label"] != "normal"]
    X = {r["run_id"]: r["df"][feats].to_numpy(float) for r in runs}
    DT = {r["run_id"]: r["dt"] for r in runs}
    T = {r["run_id"]: r["df"]["t_s"].to_numpy() for r in runs}
    # runs with a "dose" marker: rows before the first dose are normal water, rows after are the anomaly
    TD = {r["run_id"]: (dose_time(r["markers"]) if r["label"] != "normal" else None) for r in runs}
    sessions = sorted({r["session"] for r in normal})
    if len(sessions) >= 3:
        group_of = {r["run_id"]: r["session"] for r in normal}
        protocol = f"leave-one-session-out ({len(sessions)} water sessions, {len(normal)} runs)"
    else:
        group_of = {r["run_id"]: r["run_id"] for r in normal}
        protocol = (f"leave-one-run-out ({len(normal)} runs but only {len(sessions)} sessions: "
                    "runs in the same water make results look better than they are)")
    gkeys = sorted(set(group_of.values()), key=str)
    groups = {g: [(X[r["run_id"]], DT[r["run_id"]]) for r in normal if group_of[r["run_id"]] == g] for g in gkeys}
    if len(gkeys) >= 2:
        folds = [([groups[o] for o in gkeys if o != g], groups[g]) for g in gkeys]
    elif normal:
        protocol = "time split 70/30 (only one normal run, results are not yet reliable)"
        x0, d0 = X[normal[0]["run_id"]], DT[normal[0]["run_id"]]
        cut = int(len(x0) * 0.7)
        folds = [([[(x0[:cut], d0)]], [(x0[cut:], d0)])]
    else:
        folds = []
    return {"normal": normal, "test": test, "anomaly": anomaly, "X": X, "DT": DT, "T": T, "TD": TD, "sessions": sessions,
            "gkeys": gkeys, "groups": groups, "folds": folds, "protocol": protocol}


def detection_limits(model, thr, center, floors, feats):
    """Smallest steady single-sensor change (real units, each direction) that crosses the threshold.

    At a steady value the EWMA converges to the raw score, so "alarm eventually" = score > thr.
    Returns {sensor: [down, up]} with None where the model never alarms within 50 floors."""
    out = {}
    steps = np.geomspace(0.05, 50, 80)
    for i, f in enumerate(feats):
        pair = []
        for sign in (-1, 1):
            X = np.repeat(center[None, :], len(steps), axis=0)
            X[:, i] += sign * steps * floors[i]
            hit = np.nonzero(model.score(X) > thr)[0]
            pair.append(round(float(steps[hit[0]] * floors[i]), 4) if len(hit) else None)
        out[f] = pair
    return out


def train(cfg, use_ph=None, data_dir=DATA_DIR, out_dir=MODEL_DIR, log=print, selection=None):
    """selection=None trains on every usable run (as in v1.0); see datasets.py for chosen subsets."""
    import joblib

    selection = normalize_selection(selection)
    use_ph = selection_use_ph(selection, bool(cfg["use_ph"] if use_ph is None else use_ph))
    det = cfg["detector"]
    feats = feature_list(use_ph, selection["sensors"] if selection else None)
    use_ph = "ph" in feats
    floors = np.array([cfg["floors"][f] for f in feats], float)
    chosen = [cls for cls in MODELS if not selection or selection["models"] is None
              or cls.key in selection["models"] or cls is MSPC]

    runs, skipped = load_runs(cfg, use_ph, data_dir, selection)
    for s in skipped:
        if not s.get("quiet"):
            log(f"Skipped {s['file']}: {s['reason']}")
    sp = split_runs(runs, feats)
    if not sp["normal"]:
        raise TrainError("No normal run selected as train. Record at least 1 normal-water run (5 recommended)")
    normal, anomaly, test = sp["normal"], sp["anomaly"], sp["test"]
    X, DT, T, TD = sp["X"], sp["DT"], sp["T"], sp["TD"]
    sessions, gkeys, groups, folds, protocol = sp["sessions"], sp["gkeys"], sp["groups"], sp["folds"], sp["protocol"]
    k_cusum = det.get("cusum_k", K_DEFAULT)
    mparams = cfg.get("model_params", {})
    log(f"Using {len(normal)} normal runs (train), {len(test)} test-only runs, {len(anomaly)} anomaly runs, sensors: {feats}")
    log(f"Evaluation: {protocol}")

    results = {}
    for cls in chosen:
        neg_ratio, pos_ratio = [], []
        fa_hits = fa_n = det_hits = det_n = 0
        fit_times = []
        drift_edges, drift_hours = 0, 0.0
        for train_groups, held in folds:
            m, thr, med, fit_ms = fit_calibrated(cls, train_groups, floors, det, mparams.get(cls.key))
            fit_times.append(fit_ms)
            if cls is MSPC:
                h_fold = drift_h(train_groups, floors, det)
            for xh, dh in held:
                sh = normalized(m, xh, thr, med, det, dh)
                ah = alarms(sh, det, dh)
                fa_hits += int(ah.sum()); fa_n += len(ah)
                neg_ratio.append(sh)
                if cls is MSPC:
                    fl, _, _ = drift_flags((xh - m.mu) / m.sd, dh, k_cusum, h_fold)
                    drift_edges += rising_edges(fl); drift_hours += len(xh) * dh / 3600
            for r in anomaly:
                rid = r["run_id"]
                sa = normalized(m, X[rid], thr, med, det, DT[rid])
                aa = alarms(sa, det, DT[rid])
                post = np.ones(len(sa), bool) if TD[rid] is None else T[rid] >= TD[rid]
                fa_hits += int(aa[~post].sum()); fa_n += int((~post).sum())
                det_hits += int(aa[post].sum()); det_n += int(post.sum())
                if (~post).any():
                    neg_ratio.append(sa[~post])
                pos_ratio.append(sa[post])
        neg = np.concatenate(neg_ratio) if neg_ratio else np.array([])
        pos = np.concatenate(pos_ratio) if pos_ratio else np.array([])
        roc, auc = roc_points(neg, pos)
        far = fa_hits / fa_n if fa_n else None
        dr = det_hits / det_n if det_n else None
        results[cls.key] = {"key": cls.key, "name": cls.name, "false_alarm": far, "detection": dr,
                            "balanced_acc": (None if dr is None or far is None else (1 - far + dr) / 2),
                            "auc": auc, "roc": roc, "fit_ms": float(np.mean(fit_times))}
        if cls is MSPC:
            results[cls.key]["drift_false_per_hour"] = (drift_edges / drift_hours) if drift_hours else None
        log(f"  {cls.name:34s} false alarm {_pct(far)} | detection {_pct(dr)} | AUC {auc if auc is None else round(auc, 3)}")

    # final models on all normal data (these are what the live monitor uses)
    Xn = np.vstack([X[r["run_id"]] for r in normal])
    bundle_models = {}
    for cls in chosen:
        m, thr, med, _ = fit_calibrated(cls, [groups[g] for g in gkeys], floors, det, mparams.get(cls.key))
        t0 = time.perf_counter()
        for row in Xn[:50]:
            m.score(row[None, :])          # live scoring is one sample at a time
        results[cls.key]["score_us"] = (time.perf_counter() - t0) / min(50, len(Xn)) * 1e6
        bundle_models[cls.key] = {"model": m, "thr": thr, "med": med}

    # held-out normal runs: scored by the final models, which never saw them
    for k, bm in bundle_models.items():
        hits = rows = 0
        for r in test:
            a = alarms(normalized(bm["model"], X[r["run_id"]], bm["thr"], bm["med"], det, DT[r["run_id"]]), det, DT[r["run_id"]])
            hits += int(a.sum()); rows += len(a)
        results[k]["test_false_alarm"] = hits / rows if rows else None
        results[k]["test_rows"] = rows

    # per-run table + timelines with the final models
    mspc = bundle_models["mspc"]["model"]
    h_final = drift_h([groups[g] for g in gkeys], floors, det)
    from sklearn.decomposition import PCA
    Zn = (Xn - mspc.mu) / mspc.sd
    pca2 = PCA(n_components=min(2, Zn.shape[1])).fit(Zn)
    run_table, timelines, series, fp_items = [], {k: [] for k in bundle_models}, {}, []
    for r in runs:
        rid = r["run_id"]
        xr, t, td = X[rid], T[rid], TD[rid]
        t0 = td if td is not None else 0.0          # timings are measured from the dose when there is one
        post = np.ones(len(xr), bool) if td is None else t >= td
        row = {"run_id": rid, "label": r["label"], "file": r["file"], "rows": len(xr),
               "faults": r["faults"], "session": r["session"], "dt": round(r["dt"], 2), "models": {},
               "markers": r["markers"], "t_dose": td, "pre_rows": int((~post).sum())}
        for k, bm in bundle_models.items():
            s = normalized(bm["model"], xr, bm["thr"], bm["med"], det, r["dt"])
            a = alarms(s, det, r["dt"])
            hit = a & post
            first = float(t[np.argmax(hit)] - t0) if hit.any() else None
            row["models"][k] = {"alarm_rate": float(a[post].mean()) if post.any() else 0.0, "first_alarm_s": first,
                                "pre_alarm_rate": float(a[~post].mean()) if (~post).any() else None}
            if k == "mspc":
                mspc_hit = hit
                if hit.any():
                    row["min_dose"] = dose_at(r["markers"], float(t[np.argmax(hit)]))
            timelines[k].append({"run_id": rid, "label": r["label"],
                                 "t": downsample(t).round(1).tolist(),
                                 "ratio": downsample(np.clip(s, -5, 1e4)).round(4).tolist()})
            if k == "mspc":
                cps = change_points(np.clip(s, -5, 50))
                row["change_points_s"] = [round(float(t[i]), 1) for i in cps]
        z = (xr - mspc.mu) / mspc.sd
        fl, _, _ = drift_flags(z, r["dt"], k_cusum, h_final)
        dh = fl & post
        row["drift_first_s"] = float(t[np.argmax(dh)] - t0) if dh.any() else None
        a_first = row["models"]["mspc"]["first_alarm_s"]
        row["lead_s"] = (a_first - row["drift_first_s"]) if (a_first is not None and row["drift_first_s"] is not None) else None
        row["cause"] = explain(np.median(z[post], axis=0), feats) if r["label"] != "normal" and post.any() else []
        row["role"] = {"train": "normal", "test": "test"}.get(r["role"], "anomaly")
        if r["label"] != "normal" and post.any():
            vec = causes.fingerprint(z[mspc_hit] if mspc_hit.sum() >= 5 else z[post])
            fp_items.append({"run_id": rid, "family": causes.family(r["label"]), "label": r["label"], "vec": vec})
            lk = causes.infer(vec, feats)              # rule layer only: the learned layer would grade its own homework
            row["likely"] = {"verdict": lk["verdict"], "headline": lk["headline"],
                             "family": lk["candidates"][0]["family"] if lk["candidates"] else None}
        run_table.append(row)
        idx = np.linspace(0, len(xr) - 1, min(200, len(xr))).astype(int)
        series[str(rid)] = {"t": t[idx].round(1).tolist(), "z": np.clip(z[idx], -99, 999).round(3).tolist(),
                            "pc": pca2.transform(z[idx]).round(3).tolist(), "drift": fl[idx].tolist()}

    # detection delay per model (median over anomaly runs that were detected)
    for k in results:
        delays = [rw["models"][k]["first_alarm_s"] for rw in run_table
                  if rw["role"] == "anomaly" and rw["models"][k]["first_alarm_s"] is not None]
        caught = sum(1 for rw in run_table if rw["role"] == "anomaly" and rw["models"][k]["alarm_rate"] > 0)
        results[k]["delay_s"] = float(np.median(delays)) if delays else None
        results[k]["runs_caught"] = caught
    leads = [rw["lead_s"] for rw in run_table if rw["role"] == "anomaly" and rw["lead_s"] is not None]
    results["mspc"]["drift_lead_s"] = float(np.median(leads)) if leads else None

    ranked = [r for r in results.values() if r["balanced_acc"] is not None]
    if ranked:
        ok = [r for r in ranked if r["false_alarm"] <= 0.02] or ranked
        best = max(ok, key=lambda r: (round(r["balanced_acc"], 3), r["key"] == "mspc"))["key"]
    else:
        best = "mspc"

    # points for scatter / embeddings (z-scores against the normal centre)
    rng = np.random.default_rng(0)
    pts = []
    per_run = max(40, 1500 // max(1, len(runs)))
    for r in runs:
        xr = X[r["run_id"]]
        idx = np.sort(rng.choice(len(xr), min(per_run, len(xr)), replace=False))
        bm = bundle_models["mspc"]
        a = alarms(normalized(mspc, xr, bm["thr"], bm["med"], det, r["dt"]), det, r["dt"])
        for i in idx:
            pts.append({"run": r["run_id"], "label": r["label"], "x": xr[i].round(4).tolist(),
                        "z": ((xr[i] - mspc.mu) / mspc.sd).round(4).tolist(), "alarm": bool(a[i])})

    cause_model = causes.build_model(fp_items, feats) if fp_items else None
    meta = {
        "created": time.strftime("%Y-%m-%d %H:%M"), "features": feats, "use_ph": use_ph,
        "calib": cfg["calib"], "ph_divider": cfg["ph_divider"], "limits": cfg["limits"],
        "detector": det, "floors": cfg["floors"], "protocol": protocol,
        "normal_runs": [r["run_id"] for r in normal], "anomaly_runs": [r["run_id"] for r in anomaly],
        "n_train_rows": int(len(Xn)), "best": best,
        "sessions": sessions, "n_sessions": len(sessions),
        "interval_s": float(np.median([r["dt"] for r in runs if r["role"] != "test"])),
        "files": [r["file"] for r in runs], "data_hash": data_fingerprint(cfg, use_ph, data_dir, selection),
        # what exactly this model was trained on (WP4b): dataset name, role per file, content hash
        "dataset": selection["dataset"] if selection else None, "selection": selection,
        "roles": {r["file"]: r["role"] for r in runs}, "sensors": feats, "models": list(results),
        "files_hash": content_hash([r["file"] for r in runs], data_dir), "test_runs": [r["run_id"] for r in test],
        "seen": sorted(p.name for p in pathlib.Path(data_dir).glob("*.csv")),
        "cause_model": cause_model,
        "thresholds": {k: v["thr"] for k, v in bundle_models.items()},
        "mspc": {"mu": mspc.mu.tolist(), "cov": mspc.cov.tolist()},
        "drift": {"k": k_cusum, "h": h_final.tolist()},
        "model_params": mparams,
        "detect_limits": {k: detection_limits(bm["model"], bm["thr"], mspc.mu, floors, feats) for k, bm in bundle_models.items()},
        "pca2": {"mean": pca2.mean_.tolist(), "components": pca2.components_.tolist(),
                 "normal_cov": np.cov(pca2.transform(Zn), rowvar=False).tolist()},
    }
    report = {"meta": meta, "models": results, "runs": run_table, "timelines": timelines, "series": series,
              "skipped": skipped, "points": pts, "model_names": MODEL_NAMES}

    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump({"meta": meta, "models": bundle_models}, out_dir / "bundle.joblib")
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    for f in out_dir.glob("embed_*.json"):
        f.unlink()   # embeddings belong to the previous training
    snap = save_snapshot(out_dir)
    append_history(out_dir, meta, results, snapshot=snap)
    log(f"Recommended model: {MODEL_NAMES[best]}  | saved to {out_dir}")
    return report


SNAP_DIR = "saved"


def save_snapshot(out_dir, keep=15):
    """Keep a copy of every trained model so an earlier one (e.g. 3 vs 4 sensors) can be put back in one click."""
    import shutil
    out_dir = pathlib.Path(out_dir)
    sid = time.strftime("%Y%m%d-%H%M%S")
    d = out_dir / SNAP_DIR / sid
    n = 1
    while d.exists():
        n += 1
        d = out_dir / SNAP_DIR / f"{sid}-{n}"
    d.mkdir(parents=True)
    for f in ("bundle.joblib", "report.json"):
        shutil.copy2(out_dir / f, d / f)
    snaps = sorted(p for p in (out_dir / SNAP_DIR).iterdir() if p.is_dir())
    for old in snaps[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    return d.name


def list_snapshots(out_dir=MODEL_DIR):
    """Saved trainings, newest first, with what the UI needs to tell them apart."""
    out_dir = pathlib.Path(out_dir)
    try:
        hist = {h.get("snapshot"): h for h in json.loads((out_dir / "history.json").read_text(encoding="utf-8")) if h.get("snapshot")}
    except (FileNotFoundError, json.JSONDecodeError):
        hist = {}
    out = []
    root = out_dir / SNAP_DIR
    for d in sorted((root.iterdir() if root.exists() else []), reverse=True):
        if not (d / "bundle.joblib").exists():
            continue
        h = hist.get(d.name, {})
        out.append({"id": d.name, "created": h.get("created"), "features": h.get("sensors"), "use_ph": h.get("use_ph"),
                    "best": h.get("best"), "n_sessions": h.get("n_sessions"), "n_files": h.get("n_files"),
                    "metrics": h.get("metrics", {})})
    return out


def restore_snapshot(sid, out_dir=MODEL_DIR):
    import shutil
    out_dir = pathlib.Path(out_dir)
    d = out_dir / SNAP_DIR / str(sid)
    if "/" in str(sid) or "\\" in str(sid) or ".." in str(sid) or not (d / "bundle.joblib").exists():
        raise TrainError("Saved model not found")
    for f in ("bundle.joblib", "report.json"):
        shutil.copy2(d / f, out_dir / f)
    for f in out_dir.glob("embed_*.json"):
        f.unlink()


def append_history(out_dir, meta, results, keep=20, snapshot=None):
    """Settings + headline numbers of every training, so a good configuration can be restored."""
    path = pathlib.Path(out_dir) / "history.json"
    try:
        hist = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        hist = []
    hist.append({"created": meta["created"], "data_hash": meta["data_hash"], "use_ph": meta["use_ph"],
                 "detector": meta["detector"], "floors": meta["floors"], "model_params": meta["model_params"],
                 "best": meta["best"], "n_sessions": meta["n_sessions"], "snapshot": snapshot,
                 "dataset": meta.get("dataset"), "n_files": len(meta["files"]), "sensors": meta.get("sensors"),
                 "models": meta.get("models"), "files_hash": meta.get("files_hash"), "roles": meta.get("roles"),
                 "metrics": {k: {m: r.get(m) for m in ("false_alarm", "detection", "auc", "delay_s", "test_false_alarm")}
                             for k, r in results.items()}})
    path.write_text(json.dumps(hist[-keep:], ensure_ascii=False, indent=1), encoding="utf-8")


def _pct(v):
    return "  –  " if v is None else f"{v * 100:5.1f}%"


def load_bundle(out_dir=MODEL_DIR):
    import joblib
    p = pathlib.Path(out_dir) / "bundle.joblib"
    return joblib.load(p) if p.exists() else None


def embedding(method, out_dir=MODEL_DIR):
    out_dir = pathlib.Path(out_dir)
    cache = out_dir / f"embed_{method}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    pts = report["points"]
    if len(pts) < 5:
        raise TrainError("Too little data for an embedding")
    Z = np.array([p["z"] for p in pts], float)
    Z = np.clip(Z, -50, 50)   # keep extreme anomalies from squashing everything else
    t0 = time.perf_counter()
    if method == "pca":
        from sklearn.decomposition import PCA
        p = PCA(n_components=2).fit(Z)
        Y = p.transform(Z)
        note = f"Explains {p.explained_variance_ratio_.sum() * 100:.0f}% of the variance"
    elif method == "tsne":
        from sklearn.manifold import TSNE
        Y = TSNE(n_components=2, perplexity=min(30, max(5, len(Z) // 4)), init="pca",
                 random_state=0).fit_transform(Z)
        note = "t-SNE preserves near neighbours; distances between groups cannot be interpreted"
    elif method == "umap":
        try:
            import umap
        except ImportError:
            raise TrainError("UMAP is not installed: run  py -m pip install umap-learn  then restart the app")
        Y = umap.UMAP(n_neighbors=min(15, len(Z) - 1), min_dist=0.1, random_state=0).fit_transform(Z)
        note = "UMAP preserves both near and far structure better than t-SNE, but is still qualitative"
    else:
        raise TrainError(f"Unknown method {method}")
    out = {"method": method, "note": note, "seconds": round(time.perf_counter() - t0, 1),
           "xy": np.asarray(Y).round(4).tolist(),
           "label": [p["label"] for p in pts], "run": [p["run"] for p in pts],
           "alarm": [p["alarm"] for p in pts]}
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out
