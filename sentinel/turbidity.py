"""Turbidity shown in NTU. Display only: models, thresholds and training keep reading volts.

Why a ratio: this TSW-30 module reads about 1.85 V in clear water at 3.3 V, while the published curves
(DFRobot SEN0189 at 5 V, TSW-30 vendor chart) start near 4.2-4.5 V. Absolute voltage formulas do not
transfer, but the *shape* does, so everything works on r = V / V_clear after temperature correction.

Curves (all in r, NTU falls as r falls; the clear-water reference is 0 NTU by definition):
  generic_dfrobot  NTU = -1120.4 V^2 + 5742.3 V - 4352.9 with V = 4.2 r  (valid 2.6-4.2 V -> r 0.62-1)
  generic_vendor   TSW-30 chart: 4.5 V at 0 NTU, 3.9 V at 1000, 2.6 V at 3000 (about 10 degC)
  user_points      the user's own standards [[ntu, volts, temp_c], ...], PCHIP, no extrapolation

Honesty rules: with only a clear-water reference the value is an uncalibrated *estimate*; anything below
3 x sensor noise (converted through the curve) is shown as "< X NTU" instead of a number, because tap
water (< 4 NTU) is below what this sensor can resolve.
"""
import math
import pathlib

import numpy as np

CURVES = {
    "generic_dfrobot": "ประมาณจากกราฟ DFRobot SEN0189 (ยังไม่ได้คาลิเบรตกับเครื่องวัดจริง)",
    "generic_vendor": "ประมาณจากกราฟของผู้ขาย TSW-30 (ยังไม่ได้คาลิเบรตกับเครื่องวัดจริง)",
    "user_points": "คาลิเบรตด้วยสารมาตรฐานของคุณเอง",
}
DEFAULTS = {"ntu_display": True, "v_clear": None, "t_clear": None, "noise_v": 0.003,
            "temp_coeff": -0.0022, "curve": "generic_vendor", "points": []}


def _generic_points(kind):
    """[(ntu, r)] with r ascending."""
    if kind == "generic_dfrobot":
        v = np.linspace(2.6, 4.2, 10)
        ntu = -1120.4 * v ** 2 + 5742.3 * v - 4352.9
        pts = [(max(float(n), 0.0), float(x) / 4.2) for n, x in zip(ntu, v)]
        pts[-1] = (0.0, 1.0)                 # the formula gives ~1 NTU at 4.2 V; the reference is 0 by definition
        return sorted(pts, key=lambda p: p[1])
    return [(3000.0, 2.6 / 4.5), (1000.0, 3.9 / 4.5), (0.0, 1.0)]


def _norm(v, t, v_clear, t_clear, coeff):
    """Volts at temperature t -> r relative to the clear-water reference at t_clear."""
    if t is not None and t_clear is not None and math.isfinite(t):
        f = 1.0 + coeff * (t - t_clear)
        v = v / f if f > 0.2 else v
    return v / v_clear


_cache = {}


def curve(cal):
    """Cached _build_curve: the live loop converts every sample, the curve only changes with the settings."""
    key = (cal.get("curve"), cal.get("v_clear"), cal.get("t_clear"), cal.get("temp_coeff"),
           tuple(tuple(p) for p in cal.get("points", [])))
    hit = _cache.get(key)
    if hit is None:
        if len(_cache) > 20:
            _cache.clear()
        hit = _cache[key] = _build_curve(cal)
    return hit


def _build_curve(cal):
    """-> (PchipInterpolator over r, r_min, r_max, kind). Raises ValueError when the points are unusable."""
    from scipy.interpolate import PchipInterpolator

    kind = cal.get("curve", "generic_vendor")
    if kind == "user_points":
        vc = cal.get("v_clear")
        if not vc:
            raise ValueError("ต้องตั้งค่าน้ำใสก่อนจึงใช้จุดมาตรฐานได้")
        pts = [(float(n), _norm(float(v), t, vc, cal.get("t_clear"), cal["temp_coeff"]))
               for n, v, t in ((p + [None])[:3] for p in cal.get("points", []))]
        if not any(abs(n) < 1e-9 for n, _ in pts):
            pts.append((0.0, 1.0))
        pts.sort(key=lambda p: p[1])
        r = [p[1] for p in pts]
        ntu = [p[0] for p in pts]
        if len(pts) < 2:
            raise ValueError("ต้องมีจุดมาตรฐานอย่างน้อย 1 จุด")
        if any(b - a < 1e-6 for a, b in zip(r, r[1:])):
            raise ValueError("มีจุดมาตรฐานสองจุดที่แรงดันเท่ากัน")
        if any(b > a + 1e-9 for a, b in zip(ntu, ntu[1:])):
            raise ValueError("จุดมาตรฐานไม่เรียงตามความขุ่น: น้ำขุ่นมากกว่าควรให้แรงดันต่ำกว่า")
    else:
        pts = _generic_points(kind if kind in CURVES else "generic_vendor")
        r, ntu = [p[1] for p in pts], [p[0] for p in pts]
    return PchipInterpolator(r, ntu, extrapolate=False), r[0], r[-1], kind


def _nice(x):
    """Two significant digits, so a limit reads "< 36 NTU" and not "< 36.4817 NTU"."""
    if x <= 0:
        return 0.0
    d = 1 - int(math.floor(math.log10(x)))
    return round(x, max(d, 0)) if d > 0 else float(round(x, d))


def detection_limit(cal, fn=None):
    """Smallest NTU the sensor can tell from clear water: 3 x noise through the curve slope near r = 1."""
    vc = cal.get("v_clear")
    if not vc:
        return None
    fn = fn or curve(cal)[0]
    slope = abs(float(fn.derivative()(1.0 - 1e-4)))
    return _nice(3 * cal["noise_v"] / vc * slope)


def convert(turb_v, temp_c, cal):
    """Volts (+ temperature) -> {"ntu", "text", "quality", "limit", "estimate", "r", "v"}.

    quality: no_reference | below_limit | ok | over_range | off | error
    ntu is None unless quality == "ok": below the limit and over range there is no honest number.
    """
    out = {"v": turb_v, "ntu": None, "text": None, "quality": "no_reference", "limit": None, "r": None,
           "estimate": cal.get("curve") != "user_points"}
    if not cal.get("ntu_display", True):
        out["quality"] = "off"
        return out
    vc = cal.get("v_clear")
    if not vc or turb_v is None or not isinstance(turb_v, (int, float)) or not math.isfinite(turb_v):
        return out
    try:
        fn, r_min, r_max, _ = curve(cal)
    except ValueError:
        out["quality"] = "error"
        return out
    r = _norm(turb_v, temp_c, vc, cal.get("t_clear"), cal["temp_coeff"])
    limit = detection_limit(cal, fn)
    out.update(r=round(r, 4), limit=limit)
    if r < r_min - 1e-9:
        out.update(quality="over_range", text=f"> {fn(r_min):.0f} NTU")
        return out
    ntu = float(fn(min(r, r_max)))
    if ntu < limit:
        out.update(quality="below_limit", text=f"< {limit:g} NTU")
        return out
    out.update(quality="ok", ntu=round(ntu, 1 if ntu < 100 else 0), text=None)
    out["text"] = f"{out['ntu']:g} NTU"
    return out


def convert_array(turb_v, temp_c, cal):
    """Vectorised convert for recorded runs. ntu is clipped at the detection limit (never below it) and
    `below` marks the samples that are really "< limit"; over-range samples are NaN."""
    v = np.asarray(turb_v, float)
    t = np.asarray(temp_c, float)
    fn, r_min, r_max, _ = curve(cal)
    limit = detection_limit(cal, fn)
    f = 1.0 + cal["temp_coeff"] * (t - (cal.get("t_clear") if cal.get("t_clear") is not None else 0.0))
    use_t = np.isfinite(t) & (cal.get("t_clear") is not None) & (f > 0.2)
    r = np.where(use_t, v / np.where(use_t, f, 1.0), v) / cal["v_clear"]
    with np.errstate(invalid="ignore"):
        ntu = np.asarray(fn(np.clip(r, r_min, r_max)), float)
        ntu = np.where(r < r_min - 1e-9, np.nan, ntu)
        ntu = np.where(np.isfinite(v), ntu, np.nan)
        below = np.isfinite(ntu) & (ntu < limit)
        out = np.where(below, limit, ntu)
    return {"ntu": out, "below": below, "limit": limit, "estimate": cal.get("curve") != "user_points"}


def curve_preview(cal, n=40):
    """Points for the settings chart: x in volts when the reference is known, else r."""
    fn, r_min, r_max, kind = curve(cal)
    vc = cal.get("v_clear")
    rs = np.linspace(r_min, r_max, n)
    return {"x": [round(float(r * (vc or 1.0)), 4) for r in rs], "ntu": [round(float(v), 2) for v in fn(rs)],
            "x_unit": "V" if vc else "V/V_clear", "kind": kind}


def info(cal, latest=None):
    out = {"cal": cal, "curves": CURVES, "label": CURVES.get(cal.get("curve"), ""),
           "limit": detection_limit(cal) if cal.get("v_clear") else None}
    try:
        out["preview"] = curve_preview(cal)
    except ValueError as e:
        out["preview"] = None
        out["error"] = str(e)
    if latest:
        out["live"] = convert(latest.get("turb_v"), latest.get("temp_c"), cal)
    return out


def validate(patch):
    """Server-side range checks for the turbidity settings (Thai messages)."""
    t = patch.get("turbidity")
    if not t:
        return patch
    rules = {"v_clear": (0.05, 3.3), "noise_v": (0.0005, 0.2), "temp_coeff": (-0.02, 0.0), "t_clear": (-5, 80)}
    for k, (lo, hi) in rules.items():
        v = t.get(k)
        if v is not None and not (isinstance(v, (int, float)) and lo <= v <= hi):
            raise ValueError(f"turbidity.{k} ต้องอยู่ระหว่าง {lo} ถึง {hi}")
    if "curve" in t and t["curve"] not in CURVES:
        raise ValueError("ไม่รู้จักกราฟแปลงค่าความขุ่นนี้")
    pts = t.get("points")
    if pts is not None:
        if not isinstance(pts, list) or len(pts) > 12:
            raise ValueError("จุดมาตรฐานมีได้ไม่เกิน 12 จุด")
        for p in pts:
            if not (isinstance(p, list) and 2 <= len(p) <= 3 and 0 <= p[0] <= 5000 and 0.02 <= p[1] <= 3.3):
                raise ValueError("จุดมาตรฐานต้องเป็น [NTU 0-5000, แรงดัน V, อุณหภูมิ °C]")
    return patch


def fit_temp_coeff(data_dir, warmup_s=60, min_sessions=3, min_span_c=1.0, session_gap_min=10):
    """Suggest a temperature coefficient from the user's own normal sessions (never applied silently).

    One (median temperature, median turbidity volts) point per session; ln V = a + c * T by least squares.
    Cup position and probe placement also move V between sessions, so a small span or poor fit is reported
    and the vendor default is kept.
    """
    import pandas as pd
    from .features import read_run
    from .runs import list_runs

    per = {}
    for r in list_runs(data_dir, session_gap_min):
        if r["label"] != "normal" or r["excluded"] or r["rows"] < 20:
            continue
        df = read_run(pathlib.Path(data_dir) / r["file"])
        ms = pd.to_numeric(df.get("ms"), errors="coerce")
        keep = (ms - ms.min()) / 1000 >= warmup_s
        v = pd.to_numeric(df.get("turb_v"), errors="coerce")[keep]
        t = pd.to_numeric(df.get("temp_c"), errors="coerce")[keep]
        ok = np.isfinite(v) & np.isfinite(t) & (v > 0.05)
        if ok.sum() >= 10:
            per.setdefault(r["session"], []).append((float(np.median(t[ok])), float(np.median(v[ok]))))
    pts = [tuple(np.mean(a, axis=0)) for a in per.values()]
    if len(pts) < min_sessions:
        raise ValueError(f"ต้องมีน้ำปกติอย่างน้อย {min_sessions} session (ตอนนี้มี {len(pts)})")
    T = np.array([p[0] for p in pts]); V = np.log([p[1] for p in pts])
    span = float(T.max() - T.min())
    if span < min_span_c:
        raise ValueError(f"อุณหภูมิของแต่ละ session ต่างกันแค่ {span:.1f} °C (ต้องอย่างน้อย {min_span_c:g}) "
                         "จึงยังประเมินสัมประสิทธิ์ไม่ได้")
    c, a = np.polyfit(T, V, 1)
    resid = V - (a + c * T)
    r2 = 1 - float(np.sum(resid ** 2) / max(np.sum((V - V.mean()) ** 2), 1e-12))
    return {"coeff": float(np.clip(c, -0.02, 0.0)), "raw_coeff": float(c), "n_sessions": len(pts),
            "t_span": round(span, 2), "r2": round(r2, 2),
            "note": "ตำแหน่งหัววัด/แก้วที่ต่างกันก็ทำให้แรงดันเปลี่ยนได้ ใช้ค่านี้เมื่อ R² สูงและอุณหภูมิต่างกันหลายองศา"}
