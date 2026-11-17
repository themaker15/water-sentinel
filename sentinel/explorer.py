"""Recorded-run explorer (WP2): one run -> downsampled series, per-sensor characteristics, faults, score replay.

Nothing here writes to a CSV. Run management only moves files into data/_excluded/ (never deletes).
"""
import csv
import io
import pathlib
import shutil

import numpy as np
import pandas as pd

from . import turbidity
from .drift import change_points, theil_sen
from .features import FAULTS, SENSORS, derive, fault_codes, prepare_run, read_run, sample_interval
from .models import alarms, normalized, timing
from .runs import load_meta

MAX_POINTS = 1200
ARCHIVE = "_excluded"
FLOOR_KEY = {"temp_c": "temp_c", "tds_ppm": "tds_ppm", "turb_v": "turb_v", "ph": "ph"}


def _idx(n, cap):
    return np.arange(n) if n <= cap else np.unique(np.linspace(0, n - 1, cap).astype(int))


def noise_of(y):
    """Standard deviation of the white-noise part: MAD of first differences / sqrt(2) (ignores slow drift)."""
    d = np.diff(y)
    if len(d) < 3:
        return None
    return float(1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2))


def settle_time(t, y, floor, dt, win_s=10.0):
    """Seconds from the start until the rolling mean stays within one floor of the final value; None = never."""
    if len(y) < 8:
        return None
    win = max(1, int(round(win_s / max(dt, 1e-6))))
    m = pd.Series(y).rolling(win, min_periods=1).mean().to_numpy()
    final = float(np.median(y[-max(5, len(y) // 5):]))
    out = np.nonzero(np.abs(m - final) > floor)[0]
    if not len(out):
        return 0.0
    return None if out[-1] >= len(y) - 1 else float(t[out[-1] + 1] - t[0])


def sensor_stats(t, y, floor, dt):
    ok = np.isfinite(y)
    t, y = t[ok], y[ok]
    if len(y) < 3:
        return None
    r = lambda v, d=4: None if v is None else round(float(v), d)
    return {"n": int(len(y)), "mean": r(y.mean()), "sd": r(y.std(ddof=1)), "min": r(y.min()), "max": r(y.max()),
            "slope_per_min": r(theil_sen(t, y) * 60), "noise": r(noise_of(y), 5),
            "settle_s": r(settle_time(t, y, floor, dt), 1)}


def _spans(t, codes):
    out, i, n = [], 0, len(codes)
    while i < n:
        if codes[i]:
            j = i
            while j + 1 < n and codes[j + 1] == codes[i]:
                j += 1
            out.append({"t0": round(float(t[i]), 1), "t1": round(float(t[j]), 1), "code": codes[i],
                        "text": FAULTS.get(codes[i], codes[i])})
            i = j + 1
        else:
            i += 1
    return out


def replay(raw, bundle, key):
    """Score a run with a trained model (its own settings and calibration). None when it does not fit the model."""
    if not bundle:
        return None
    meta = bundle["meta"]
    feats = meta["features"]
    if key not in bundle["models"]:
        key = "mspc"
    if "ph" in feats and not np.isfinite(pd.to_numeric(raw.get("ph_pin_v"), errors="coerce")).any():
        return None
    like = {"detector": meta["detector"], "calib": meta["calib"], "ph_divider": meta["ph_divider"], "limits": meta["limits"]}
    clean, _, dt = prepare_run(raw, like, meta["use_ph"], meta["calib"], meta["ph_divider"], warmup=False, sensors=feats)
    if len(clean) < 10:
        return None
    bm = bundle["models"][key]
    s = normalized(bm["model"], clean[feats].to_numpy(float), bm["thr"], bm["med"], meta["detector"], dt)
    a = alarms(s, meta["detector"], dt)
    t = clean["t_s"].to_numpy()
    cps = change_points(np.clip(s, -5, 50))
    ix = _idx(len(t), MAX_POINTS)
    return {"model": key, "t": t[ix].round(1).tolist(), "ratio": np.clip(s[ix], -5, 1e4).round(4).tolist(),
            "alarm": a[ix].tolist(), "change_points": [round(float(t[i]), 1) for i in cps],
            "first_alarm_s": float(t[np.argmax(a)]) if a.any() else None,
            "alarm_rate": float(a.mean())}


def run_detail(path, cfg, bundle=None, key="mspc", with_score=True):
    path = pathlib.Path(path)
    raw = read_run(path)
    meta = load_meta(path)
    df = derive(raw, cfg["calib"], cfg["ph_divider"])
    df["ms"] = pd.to_numeric(df.get("ms"), errors="coerce")
    df = df.dropna(subset=["ms"]).reset_index(drop=True)
    if df.empty:
        raise ValueError("รอบนี้ไม่มีข้อมูล")
    dt = sample_interval(df["ms"])
    t = ((df["ms"] - df["ms"].min()) / 1000.0).to_numpy()
    label = str(raw["label"].iloc[0])
    det, lim = cfg["detector"], cfg["limits"]
    warm = float(det["warmup_s"]) if label == "normal" and t[-1] > det["warmup_s"] + 30 else 0.0
    has_ph = bool(np.isfinite(df["ph_pin_v"].to_numpy(float)).any())
    sensors = ["temp_c", "tds_ppm", "turb_v"] + (["ph"] if has_ph else [])
    win = timing(det, dt)["median_win"]
    ix = _idx(len(t), MAX_POINTS)
    series, filt, stats = {}, {}, {}
    for f in sensors:
        valid = fault_codes(df, True, lim, [f]) == ""     # a fault on one sensor never hides the others
        y = df[f].to_numpy(float).copy()
        y[~valid] = np.nan
        fy = pd.Series(y).rolling(win, min_periods=1).median().to_numpy()
        series[f], filt[f] = y[ix].round(4), fy[ix].round(4)
        m = t >= warm
        stats[f] = sensor_stats(t[m], y[m], cfg["floors"].get(f, 0.0), dt)
    ntu = None
    cal = cfg.get("turbidity") or {}
    if cal.get("v_clear") and cal.get("ntu_display", True):
        try:
            conv = turbidity.convert_array(df["turb_v"].to_numpy(float), df["temp_c"].to_numpy(float), cal)
            ntu = {"limit": conv["limit"], "estimate": conv["estimate"], "label": turbidity.CURVES.get(cal["curve"], ""),
                   "raw": conv["ntu"][ix].round(1), "filt": pd.Series(conv["ntu"]).rolling(win, min_periods=1).median()
                   .to_numpy()[ix].round(1)}
            stats["turb_ntu"] = sensor_stats(t[t >= warm], conv["ntu"][t >= warm], 0.0, dt)
            if stats["turb_ntu"]:
                stats["turb_ntu"]["below_limit_frac"] = round(float(np.mean(conv["below"][t >= warm])), 3)
        except ValueError:
            ntu = None
    codes = fault_codes(df, has_ph, lim, sensors)
    faults = _spans(t, codes)
    detail = {
        "file": path.name, "label": label, "note": meta.get("note", ""), "excluded": bool(meta.get("excluded", False)),
        "session": None, "source": meta.get("source", "record"), "firmware": meta.get("firmware"),
        "markers": meta.get("markers", []), "rows": int(len(df)), "duration_s": round(float(t[-1]), 1),
        "interval_s": round(dt, 3), "warmup_s": warm, "median_s": det["median_s"], "has_ph": has_ph,
        "t": t[ix].round(2), "raw": series, "filt": filt, "ntu": ntu, "faults": faults,
        "fault_rows": int((codes != "").sum()), "stats": stats,
        "score": replay(raw, bundle, key) if with_score else None,
    }
    if detail["score"] and bundle:
        detail["score"]["in_training"] = path.name in set(bundle["meta"].get("files", [])) and \
            bundle["meta"].get("roles", {}).get(path.name, "train") == "train"
    return detail


def stats_csv(details):
    """One row per run x sensor, for the report."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["file", "label", "session", "rows", "duration_s", "interval_s", "fault_rows", "sensor", "unit", "n",
                "mean", "sd", "min", "max", "slope_per_min", "noise", "settle_s"])
    for d in details:
        for sensor, st in d["stats"].items():
            if not st:
                continue
            unit = "NTU" if sensor == "turb_ntu" else SENSORS[sensor]["unit"]
            w.writerow([d["file"], d["label"], d.get("session") or "", d["rows"], d["duration_s"], d["interval_s"],
                        d["fault_rows"], sensor, unit, st["n"], st["mean"], st["sd"], st["min"], st["max"],
                        st["slope_per_min"], st["noise"], st["settle_s"]])
    return buf.getvalue()


# ------------------------------------------------------------------ run management (never deletes)
def _safe(data_dir, file):
    p = pathlib.Path(data_dir) / file
    if pathlib.Path(file).name != file or p.suffix != ".csv":
        raise ValueError("ชื่อไฟล์ไม่ถูกต้อง")
    return p


def _move(src, dst_dir):
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    k = 1
    while dst.exists():
        dst = dst_dir / f"{src.stem}-{k}{src.suffix}"
        k += 1
    shutil.move(str(src), str(dst))
    side = src.with_suffix(".json")
    if side.exists():
        shutil.move(str(side), str(dst.with_suffix(".json")))
    return dst


def archive_run(data_dir, file):
    """Move a run (CSV + sidecar) to data/_excluded/. It stays on disk and can be restored."""
    src = _safe(data_dir, file)
    if not src.exists():
        raise ValueError("ไม่พบไฟล์รอบนี้")
    return _move(src, pathlib.Path(data_dir) / ARCHIVE).name


def restore_run(data_dir, file):
    src = _safe(pathlib.Path(data_dir) / ARCHIVE, file)
    if not src.exists():
        raise ValueError("ไม่พบไฟล์ในที่เก็บ")
    if (pathlib.Path(data_dir) / file).exists():
        raise ValueError("มีไฟล์ชื่อเดียวกันอยู่ในโฟลเดอร์ข้อมูลแล้ว")
    return _move(src, pathlib.Path(data_dir)).name


def list_archived(data_dir):
    d = pathlib.Path(data_dir) / ARCHIVE
    return sorted(p.name for p in d.glob("*.csv")) if d.exists() else []
