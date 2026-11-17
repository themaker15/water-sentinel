"""Recorded runs: file listing, sidecar metadata and sessions.

Each run is `data/runNNN_<label>_<stamp>.csv` (never rewritten by the app) plus an optional sidecar
`data/runNNN_<label>_<stamp>.json` holding everything the user can change later:
    {"label": "...", "note": "...", "excluded": false, "new_water": true, "session": "...",
     "markers": [{"t_s": 12.0, "text": "added 1 mL"}], "source": "record|snapshot|auto"}

A *session* is one batch of water. Runs recorded back-to-back without changing the water are not
independent, so evaluation leaves out whole sessions when there are enough of them.
"""
import datetime as dt
import json
import pathlib
import re

import numpy as np

from .config import DATA_DIR
from .features import NAME_RE

LABEL_RE = re.compile(r"[A-Za-z0-9_\-]+")
UNLABELED = "unlabeled"
_cache = {}


def sidecar_path(path):
    return pathlib.Path(path).with_suffix(".json")


def load_meta(path):
    p = sidecar_path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_meta(path, patch):
    meta = load_meta(path)
    for k, v in patch.items():
        if k == "label":
            v = str(v).strip()
            if not LABEL_RE.fullmatch(v):
                raise ValueError("label ใช้ได้เฉพาะ a-z 0-9 _ - (ห้ามเว้นวรรค)")
        meta[k] = v
    sidecar_path(path).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def _scan(path):
    """rows, sample interval (s), duration (s), has pH column: cached by mtime/size."""
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path.name)
    if hit and hit[0] == key:
        return hit[1]
    rows, ms, has_ph = 0, [], False
    with open(path, encoding="utf-8", errors="replace") as f:
        header = f.readline().strip().split(",")
        has_ph = "ph_pin_v" in header
        i_ms = header.index("ms") if "ms" in header else None
        for line in f:
            rows += 1
            if i_ms is not None:
                try:
                    ms.append(float(line.split(",")[i_ms]))
                except (ValueError, IndexError):
                    pass
    if len(ms) > 1:
        d = np.diff(ms)
        d = d[(d > 0) & (d < 10000)]
        interval = float(np.median(d)) / 1000 if len(d) else 1.0
        duration = float(np.sum(d)) / 1000 if len(d) else 0.0
    else:
        interval, duration = 1.0, 0.0
    info = {"rows": rows, "interval_s": round(interval, 3), "duration_s": round(duration, 1), "has_ph": has_ph}
    _cache[path.name] = (key, info)
    return info


def list_runs(data_dir=DATA_DIR, session_gap_min=10):
    """All runs with metadata and session ids, oldest first."""
    out = []
    for p in sorted(pathlib.Path(data_dir).glob("*.csv")):
        m = NAME_RE.match(p.name)
        meta = load_meta(p)
        scan = _scan(p)
        start = None
        if m:
            stamp = m.group(3)
            fmt = "%Y%m%d-%H%M%S" if len(stamp) == 15 else "%Y%m%d-%H%M"
            try:
                start = dt.datetime.strptime(stamp, fmt)
            except ValueError:
                start = None
        if start is None:
            start = dt.datetime.fromtimestamp(p.stat().st_mtime) - dt.timedelta(seconds=scan["duration_s"])
        out.append({"file": p.name, "run": int(m.group(1)) if m else None,
                    "label": meta.get("label") or (m.group(2) if m else "unknown"),
                    "note": meta.get("note", ""), "excluded": bool(meta.get("excluded", False)),
                    "source": meta.get("source", "record"), "markers": meta.get("markers", []),
                    "calib": meta.get("calib"), "firmware": meta.get("firmware"), "start": start, **scan, "_meta": meta})
    out.sort(key=lambda r: r["start"])
    # sessions: explicit id wins; otherwise a new session starts when the user said "new water"
    # or when the gap since the previous run ended is longer than session_gap_min
    sid, prev_end = None, None
    for r in out:
        meta = r.pop("_meta")
        gap = None if prev_end is None else (r["start"] - prev_end).total_seconds() / 60
        if meta.get("session"):
            sid = meta["session"]
        elif sid is None or meta.get("new_water") or gap is None or gap > session_gap_min:
            sid = "S" + r["start"].strftime("%m%d-%H%M") + (f"-r{r['run']}" if r["run"] is not None else "")
        r["session"] = sid
        r["gap_min"] = None if gap is None else round(gap, 1)
        prev_end = r["start"] + dt.timedelta(seconds=r["duration_s"])
        r["time"] = r["start"].strftime("%Y-%m-%d %H:%M")
        r["start"] = r["start"].isoformat(timespec="seconds")
    return out


def run_lookup(data_dir=DATA_DIR, session_gap_min=10):
    return {r["file"]: r for r in list_runs(data_dir, session_gap_min)}
