"""Training, model comparison (evaluation) and 2-D embeddings for visualization."""
import json
import pathlib
import time

import numpy as np
import pandas as pd

from .config import DATA_DIR, MODEL_DIR
from .features import SENSORS, feature_list, prepare_run, read_run
from .models import MODELS, MODEL_NAMES, alarm_states, fit_calibrated, normalized


class TrainError(Exception):
    pass


def load_runs(cfg, use_ph, data_dir=DATA_DIR):
    runs, skipped = [], []
    for p in sorted(pathlib.Path(data_dir).glob("*.csv")):
        try:
            raw = read_run(p)
        except Exception as e:  # unreadable file
            skipped.append({"file": p.name, "reason": f"อ่านไฟล์ไม่ได้: {e}"})
            continue
        if raw.empty:
            skipped.append({"file": p.name, "reason": "ไฟล์ว่าง (มีแต่หัวตาราง)"})
            continue
        label = str(raw["label"].iloc[0])
        if use_ph and ("ph_pin_v" not in raw or not np.isfinite(pd.to_numeric(raw["ph_pin_v"], errors="coerce")).any()):
            skipped.append({"file": p.name, "reason": "ไม่มีข้อมูล pH (เก็บก่อนเปิดใช้ pH)"})
            continue
        clean, faults = prepare_run(raw, cfg, use_ph, warmup=(label == "normal"))
        if len(clean) < 10:
            why = "เหลือข้อมูลน้อยกว่า 10 แถวหลังตัดช่วงเริ่มต้น/ค่าเซนเซอร์ผิดพลาด"
            if faults:
                why += f" (พบ {', '.join(faults)})"
            skipped.append({"file": p.name, "reason": why})
            continue
        runs.append({"run_id": int(raw["run_id"].iloc[0]), "label": label, "file": p.name,
                     "df": clean, "faults": int(sum(faults.values())), "fault_types": faults})
    ids = [r["run_id"] for r in runs]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        files = [r["file"] for r in runs if r["run_id"] in dup]
        raise TrainError(f"เลขรอบ {dup} ซ้ำกันหลายไฟล์: {files} ย้ายไฟล์ที่ไม่ใช้ออกจากโฟลเดอร์ data")
    return runs, skipped


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


def train(cfg, use_ph=None, data_dir=DATA_DIR, out_dir=MODEL_DIR, log=print):
    import joblib

    use_ph = bool(cfg["use_ph"] if use_ph is None else use_ph)
    det = cfg["detector"]
    feats = feature_list(use_ph)
    floors = np.array([cfg["floors"][f] for f in feats], float)

    runs, skipped = load_runs(cfg, use_ph, data_dir)
    for s in skipped:
        log(f"ข้าม {s['file']}: {s['reason']}")
    normal = [r for r in runs if r["label"] == "normal"]
    anomaly = [r for r in runs if r["label"] != "normal"]
    if not normal:
        raise TrainError("ยังไม่มีรอบ normal ที่ใช้ได้ เก็บน้ำปกติอย่างน้อย 1 รอบ (แนะนำ 5 รอบ)")
    log(f"ใช้ {len(normal)} รอบ normal, {len(anomaly)} รอบ anomaly, เซนเซอร์: {feats}")

    X = {r["run_id"]: r["df"][feats].to_numpy(float) for r in runs}

    # folds: leave one normal run out, or a 70/30 time split when there is only one
    if len(normal) >= 2:
        protocol = "leave-one-run-out (เกณฑ์ตั้งด้วย cross-validation ภายในชุดเทรน)" if len(normal) >= 4 \
            else "leave-one-run-out (รอบ normal ยังน้อย เกณฑ์ตั้งจากข้อมูลเทรน)"
        folds = [([X[o["run_id"]] for o in normal if o is not r], X[r["run_id"]], r["run_id"]) for r in normal]
    else:
        protocol = "time-split 70/30 (มี normal รอบเดียว ผลยังเชื่อถือได้น้อย)"
        x0 = X[normal[0]["run_id"]]
        cut = int(len(x0) * 0.7)
        folds = [([x0[:cut]], x0[cut:], normal[0]["run_id"])]

    results = {}
    P, R = det["persist"], det["release"]
    for cls in MODELS:
        neg_ratio, pos_ratio = [], []
        fa_hits = fa_n = det_hits = det_n = 0
        fit_times = []
        for train_parts, held, _rid in folds:
            m, thr, med, fit_ms = fit_calibrated(cls, train_parts, floors, det)
            fit_times.append(fit_ms)
            sh = normalized(m, held, thr, med, det)
            ah = alarm_states(sh, 1.0, P, R)
            fa_hits += int(ah.sum()); fa_n += len(ah)
            neg_ratio.append(sh)
            for r in anomaly:
                sa = normalized(m, X[r["run_id"]], thr, med, det)
                aa = alarm_states(sa, 1.0, P, R)
                det_hits += int(aa.sum()); det_n += len(aa)
                pos_ratio.append(sa)
        neg = np.concatenate(neg_ratio) if neg_ratio else np.array([])
        pos = np.concatenate(pos_ratio) if pos_ratio else np.array([])
        roc, auc = roc_points(neg, pos)
        far = fa_hits / fa_n if fa_n else None
        dr = det_hits / det_n if det_n else None
        results[cls.key] = {"key": cls.key, "name": cls.name, "false_alarm": far, "detection": dr,
                            "balanced_acc": (None if dr is None or far is None else (1 - far + dr) / 2),
                            "auc": auc, "roc": roc, "fit_ms": float(np.mean(fit_times))}
        log(f"  {cls.name:34s} false alarm {_pct(far)} | detection {_pct(dr)} | AUC {auc if auc is None else round(auc, 3)}")

    # final models on all normal data (these are what the live monitor uses)
    Xn = np.vstack([X[r["run_id"]] for r in normal])
    bundle_models = {}
    for cls in MODELS:
        m, thr, med, _ = fit_calibrated(cls, [X[r["run_id"]] for r in normal], floors, det)
        t0 = time.perf_counter()
        for row in Xn[:50]:
            m.score(row[None, :])          # live scoring is one sample at a time
        results[cls.key]["score_us"] = (time.perf_counter() - t0) / min(50, len(Xn)) * 1e6
        bundle_models[cls.key] = {"model": m, "thr": thr, "med": med}

    # per-run table + timelines with the final models
    mspc = bundle_models["mspc"]["model"]
    run_table, timelines = [], {k: [] for k in bundle_models}
    for r in runs:
        xr = X[r["run_id"]]
        t = r["df"]["t_s"].to_numpy()
        row = {"run_id": r["run_id"], "label": r["label"], "file": r["file"], "rows": len(xr),
               "faults": r["faults"], "models": {}}
        for k, bm in bundle_models.items():
            s = normalized(bm["model"], xr, bm["thr"], bm["med"], det)
            a = alarm_states(s, 1.0, P, R)
            first = float(t[np.argmax(a)]) if a.any() else None
            row["models"][k] = {"alarm_rate": float(a.mean()), "first_alarm_s": first}
            timelines[k].append({"run_id": r["run_id"], "label": r["label"],
                                 "t": downsample(t).round(1).tolist(),
                                 "ratio": downsample(np.clip(s, -5, 1e4)).round(4).tolist()})
        z = (xr - mspc.mu) / mspc.sd
        row["cause"] = explain(np.median(z, axis=0), feats) if r["label"] != "normal" else []
        row["role"] = "normal" if r["label"] == "normal" else "anomaly"
        run_table.append(row)

    # detection delay per model (median over anomaly runs that were detected)
    for k in results:
        delays = [rw["models"][k]["first_alarm_s"] for rw in run_table
                  if rw["role"] == "anomaly" and rw["models"][k]["first_alarm_s"] is not None]
        caught = sum(1 for rw in run_table if rw["role"] == "anomaly" and rw["models"][k]["alarm_rate"] > 0)
        results[k]["delay_s"] = float(np.median(delays)) if delays else None
        results[k]["runs_caught"] = caught

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
        a = alarm_states(normalized(mspc, xr, bm["thr"], bm["med"], det), 1.0, P, R)
        for i in idx:
            pts.append({"run": r["run_id"], "label": r["label"], "x": xr[i].round(4).tolist(),
                        "z": ((xr[i] - mspc.mu) / mspc.sd).round(4).tolist(), "alarm": bool(a[i])})

    meta = {
        "created": time.strftime("%Y-%m-%d %H:%M"), "features": feats, "use_ph": use_ph,
        "calib": cfg["calib"], "ph_divider": cfg["ph_divider"], "limits": cfg["limits"],
        "detector": det, "floors": cfg["floors"], "protocol": protocol,
        "normal_runs": [r["run_id"] for r in normal], "anomaly_runs": [r["run_id"] for r in anomaly],
        "n_train_rows": int(len(Xn)), "best": best,
        "thresholds": {k: v["thr"] for k, v in bundle_models.items()},
        "mspc": {"mu": mspc.mu.tolist(), "cov": mspc.cov.tolist()},
    }
    report = {"meta": meta, "models": results, "runs": run_table, "timelines": timelines,
              "skipped": skipped, "points": pts, "model_names": MODEL_NAMES}

    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump({"meta": meta, "models": bundle_models}, out_dir / "bundle.joblib")
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    for f in out_dir.glob("embed_*.json"):
        f.unlink()   # embeddings belong to the previous training
    log(f"โมเดลที่แนะนำ: {MODEL_NAMES[best]}  | บันทึกไว้ที่ {out_dir}")
    return report


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
        raise TrainError("ข้อมูลน้อยเกินไปสำหรับทำ embedding")
    Z = np.array([p["z"] for p in pts], float)
    Z = np.clip(Z, -50, 50)   # keep extreme anomalies from squashing everything else
    t0 = time.perf_counter()
    if method == "pca":
        from sklearn.decomposition import PCA
        p = PCA(n_components=2).fit(Z)
        Y = p.transform(Z)
        note = f"อธิบายความแปรปรวนได้ {p.explained_variance_ratio_.sum() * 100:.0f}%"
    elif method == "tsne":
        from sklearn.manifold import TSNE
        Y = TSNE(n_components=2, perplexity=min(30, max(5, len(Z) // 4)), init="pca",
                 random_state=0).fit_transform(Z)
        note = "t-SNE รักษาเพื่อนบ้านใกล้ ระยะห่างระหว่างกลุ่มตีความไม่ได้"
    elif method == "umap":
        try:
            import umap
        except ImportError:
            raise TrainError("ยังไม่ได้ติดตั้ง UMAP: รัน  py -m pip install umap-learn  แล้วเปิดโปรแกรมใหม่")
        Y = umap.UMAP(n_neighbors=min(15, len(Z) - 1), min_dist=0.1, random_state=0).fit_transform(Z)
        note = "UMAP รักษาโครงสร้างทั้งใกล้และไกลได้ดีกว่า t-SNE แต่ยังเป็นภาพเชิงคุณภาพ"
    else:
        raise TrainError(f"ไม่รู้จักวิธี {method}")
    out = {"method": method, "note": note, "seconds": round(time.perf_counter() - t0, 1),
           "xy": np.asarray(Y).round(4).tolist(),
           "label": [p["label"] for p in pts], "run": [p["run"] for p in pts],
           "alarm": [p["alarm"] for p in pts]}
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out
