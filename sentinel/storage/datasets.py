"""Choosing which data trains the model (WP4b): roles per run, sensors, models and named datasets.

A *selection* is {"dataset": name|None, "files": {file: role}|None, "sensors": [..]|None, "models": [..]|None}.
None means "default": every usable run (normal -> train, others -> anomaly), the 3-or-4 sensors from
use_ph, and all five models. Roles:
    train      normal water the model is fitted on (and cross-validated on)
    test       normal water held out: never fitted, only used for an honest false-alarm check
    anomaly    contaminated water used for evaluation (and for the cause centroids)
    exclude    not used
Named datasets live in data/datasets.json so a result can be traced back to its exact data.
"""
import json
import pathlib
import re

from ..config import DATA_DIR
from ..acquisition.features import ALL_FEATURES

ROLES = ("train", "test", "anomaly", "exclude")
MODEL_KEYS = ("mspc", "pca", "iforest", "ocsvm", "lof", "threshold")
NAME_OK = re.compile(r"[^\x00-\x1f\\/]{1,40}")


def default_role(label):
    return "train" if label == "normal" else "anomaly"


def normalize_selection(sel):
    """Validate a selection from the API/UI. Returns None for the default, else a clean dict."""
    if not sel:
        return None
    files, sensors, models = sel.get("files"), sel.get("sensors"), sel.get("models")
    if files is None and sensors is None and models is None:
        return None
    if files is not None:
        if not isinstance(files, dict):
            raise ValueError("files ต้องเป็น {ชื่อไฟล์: role}")
        for f, r in files.items():
            if not isinstance(f, str) or "/" in f or "\\" in f or not f.endswith(".csv"):
                raise ValueError(f"ชื่อไฟล์ไม่ถูกต้อง: {f}")
            if r not in ROLES:
                raise ValueError(f"role ของ {f} ต้องเป็น {', '.join(ROLES)}")
        files = dict(sorted(files.items()))
    if sensors is not None:
        sensors = [f for f in ALL_FEATURES if f in set(sensors)]
        if len(sensors) < 2:
            raise ValueError("เลือกเซนเซอร์อย่างน้อย 2 ตัว")
    if models is not None:
        bad = [m for m in models if m not in MODEL_KEYS]
        if bad:
            raise ValueError(f"ไม่รู้จักโมเดล {bad}")
        models = [m for m in MODEL_KEYS if m == "mspc" or m in set(models)]   # MSPC explains causes and drift: always trained
    name = sel.get("dataset")
    return {"dataset": str(name) if name else None, "files": files, "sensors": sensors, "models": models}


def selection_from_cfg(cfg):
    """The selection the current model was trained with (stored in config.json -> training)."""
    t = cfg.get("training") or {}
    return normalize_selection(t)


def selection_use_ph(sel, use_ph):
    return ("ph" in sel["sensors"]) if sel and sel.get("sensors") is not None else bool(use_ph)


def role_of(sel, file, label):
    """Role for one run under a selection (None selection = default)."""
    if sel and sel.get("files") is not None:
        return sel["files"].get(file, "exclude")
    return default_role(label)


# ------------------------------------------------------------------ named datasets
def _path(data_dir):
    return pathlib.Path(data_dir) / "datasets.json"


def load_datasets(data_dir=DATA_DIR):
    try:
        return json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_dataset(name, sel, note="", data_dir=DATA_DIR):
    name = (name or "").strip()
    if not NAME_OK.fullmatch(name):
        raise ValueError("ตั้งชื่อชุดข้อมูลไม่เกิน 40 ตัวอักษร (ห้ามใช้ / \\)")
    sel = normalize_selection(sel)
    if not sel or sel["files"] is None:
        raise ValueError("เลือกรอบข้อมูลก่อนบันทึกเป็นชุดข้อมูล")
    import time
    ds = load_datasets(data_dir)
    ds[name] = {"files": sel["files"], "sensors": sel["sensors"], "models": sel["models"],
                "note": str(note)[:200], "saved": time.strftime("%Y-%m-%d %H:%M")}
    _path(data_dir).write_text(json.dumps(ds, ensure_ascii=False, indent=1), encoding="utf-8")
    return ds[name]


def delete_dataset(name, data_dir=DATA_DIR):
    """Removes only the saved definition, never any data file."""
    ds = load_datasets(data_dir)
    if name not in ds:
        raise ValueError("ไม่พบชุดข้อมูลนี้")
    del ds[name]
    _path(data_dir).write_text(json.dumps(ds, ensure_ascii=False, indent=1), encoding="utf-8")


def dataset_selection(name, data_dir=DATA_DIR):
    ds = load_datasets(data_dir)
    if name not in ds:
        raise ValueError(f"ไม่พบชุดข้อมูล '{name}'")
    d = ds[name]
    return normalize_selection({"dataset": name, "files": d["files"], "sensors": d.get("sensors"),
                                "models": d.get("models")})
