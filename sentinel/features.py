"""Raw sensor values -> features, sensor-fault detection, CSV loading."""
import re

import numpy as np
import pandas as pd


NAME_RE = re.compile(r"run(\d+)_(.+?)_(\d{8}-\d{4,6})\.csv$")


RAW_COLS = ["temp_c", "tds_v", "turb_v", "ph_pin_v"]


SENSORS = {
    "temp_c": {"name": "อุณหภูมิ", "unit": "°C", "up": "อุณหภูมิสูงขึ้น", "down": "อุณหภูมิลดลง"},
    "tds_ppm": {"name": "TDS", "unit": "ppm", "up": "TDS สูงขึ้น (สารละลาย/เกลือเพิ่ม)", "down": "TDS ลดลง"},
    "turb_v": {"name": "ความขุ่น", "unit": "V", "up": "น้ำใสขึ้น", "down": "น้ำขุ่นขึ้น"},
    "ph": {"name": "pH", "unit": "", "up": "pH สูงขึ้น (เป็นด่างขึ้น)", "down": "pH ลดลง (เป็นกรดขึ้น)"},
}


FAULTS = {
    "temp_missing": "ไม่พบเซนเซอร์อุณหภูมิ (DS18B20)",
    "tds_dry": "หัววัด TDS ไม่อยู่ในน้ำ หรือสายหลวม",
    "tds_saturated": "TDS อ่านค่าเต็มสเกล (ADC อิ่มตัว)",
    "turb_saturated": "ความขุ่นอ่านค่าเต็มสเกล (ADC อิ่มตัว)",
    "turb_missing": "ไม่มีสัญญาณจากเซนเซอร์ความขุ่น",
    "ph_saturated": "pH อ่านค่าเต็มสเกล: ต้องใช้ตัวแบ่งแรงดัน 10k/10k",
    "ph_missing": "ไม่มีสัญญาณจากเซนเซอร์ pH",
}


def tds_ppm(tds_v, temp_c, offset_v, k):
    tds_v = np.asarray(tds_v, float)
    t = np.asarray(temp_c, float)
    t = np.where(np.isfinite(t), t, 25.0)
    v = np.clip(tds_v - offset_v, 0, None)
    vc = v / (1.0 + 0.02 * (t - 25.0))
    return (133.42 * vc ** 3 - 255.86 * vc ** 2 + 857.39 * vc) * 0.5 * k


def ph_value(ph_pin_v, divider, points):
    (p1, v1), (p2, v2) = points
    po = np.asarray(ph_pin_v, float) * divider
    if abs(v2 - v1) < 1e-6:
        return np.full(po.shape, np.nan)
    return p1 + (p2 - p1) / (v2 - v1) * (po - v1)


def feature_list(use_ph):
    return ["temp_c", "tds_ppm", "turb_v"] + (["ph"] if use_ph else [])


def derive(df, calib, divider):
    df = df.copy()
    for c in RAW_COLS:
        if c not in df:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["tds_ppm"] = tds_ppm(df["tds_v"], df["temp_c"], calib["tds_offset_v"], calib["tds_k"])
    df["ph"] = ph_value(df["ph_pin_v"], divider, calib["ph_points"])
    return df


def fault_codes(df, use_ph, lim):
    t, tds, turb, ph = (df[c].to_numpy(float) for c in RAW_COLS)
    with np.errstate(invalid="ignore"):
        conds = [~np.isfinite(t) | (t < -50) | (t == 85.0),
                 ~np.isfinite(tds) | (tds <= lim["tds_dry_v"]),
                 tds >= lim["adc_max_v"],
                 turb >= lim["adc_max_v"],
                 ~np.isfinite(turb) | (turb <= lim["min_signal_v"])]
        codes = ["temp_missing", "tds_dry", "tds_saturated", "turb_saturated", "turb_missing"]
        if use_ph:
            conds += [ph >= lim["adc_max_v"], ~np.isfinite(ph) | (ph <= lim["min_signal_v"])]
            codes += ["ph_saturated", "ph_missing"]
    return np.select(conds, codes, default="")


def derive_one(raw, calib, divider):
    df = derive(pd.DataFrame([raw]), calib, divider)
    return {k: float(df[k].iloc[0]) for k in RAW_COLS + ["tds_ppm", "ph"]}


def fault_one(raw, use_ph, lim):
    df = pd.DataFrame([{c: raw.get(c, np.nan) for c in RAW_COLS}])
    return str(fault_codes(df, use_ph, lim)[0])


def read_run(path):
    df = pd.read_csv(path)
    m = NAME_RE.match(path.name)
    if "run_id" not in df or df["run_id"].isna().all():
        df["run_id"] = int(m.group(1)) if m else -1
    if "label" not in df:
        df["label"] = m.group(2) if m else "unknown"
    return df


def prepare_run(raw, cfg, use_ph, calib=None, divider=None, warmup=True):
    """Raw CSV rows -> clean, median-filtered feature rows (+ fault counts)."""
    det = cfg["detector"]
    feats = feature_list(use_ph)
    df = derive(raw, calib or cfg["calib"], cfg["ph_divider"] if divider is None else divider)
    df["ms"] = pd.to_numeric(df.get("ms"), errors="coerce")
    df = df.dropna(subset=["ms"])
    if df.empty:
        return df, {}
    df["t_s"] = (df["ms"] - df["ms"].min()) / 1000.0
    if warmup:
        df = df[df["t_s"] >= det["warmup_s"]]
    df["fault"] = fault_codes(df, use_ph, cfg["limits"])
    faults = df["fault"][df["fault"] != ""].value_counts().to_dict()
    ok = (df["fault"] == "") & np.isfinite(df[feats].to_numpy(float)).all(axis=1)
    clean = df.loc[ok].copy()
    win = det["median_win"]
    clean[feats] = clean[feats].rolling(win, min_periods=win).median()
    clean = clean.dropna(subset=feats)
    return clean, faults
