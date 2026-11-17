"""Paths and settings. Every tunable value lives in config.json (created on first save)."""
import json
import os
import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent   # repository root (code)
WEB_DIR = ROOT / "web"
# where data/, model/, logs/ and config.json live; tests point this at a temp folder
HOME = pathlib.Path(os.environ.get("SENTINEL_HOME", ROOT))
DATA_DIR = HOME / "data"
MODEL_DIR = HOME / "model"
LOG_DIR = HOME / "logs"
CONFIG_PATH = HOME / "config.json"


DEFAULT_CONFIG = {
    "port": "auto",
    "baud": 115200,
    "use_ph": False,
    "ph_divider": 2.0,           # Po -> 10k/10k divider -> ESP32 pin. Set 1.0 if no divider.
    "monitor_on": False,
    "active_model": "mspc",
    "http_port": 8000,
    # Remote access (phone). Needs a PIN; restart the app after changing "enabled".
    "remote": {"enabled": False, "pin": "", "allowed_hosts": [],
               # viewer link for presentations: tunnelled visitors may look without a PIN but change nothing
               "public_view": False, "public_url": ""},
    # Token an ESP32 on WiFi must send (header X-Token) to POST readings to /api/ingest.
    "ingest_token": "",
    "session_gap_min": 10,       # runs further apart than this count as a new batch of water
    "auto_snapshot": True,       # save 2 min before to 5 min after every alarm as an unlabeled run
    "link_timeout_s": 60,        # alert when no sensor data arrives for this long while monitoring
    "stream_log_days": 14,       # keep logs/stream-YYYYMMDD.csv for this many days
    "calib": {
        "tds_offset_v": 0.142,   # TDS pin voltage with the probe dry in air
        "tds_k": 1.0,            # known ppm / measured ppm in a standard solution
        "ph_points": [[6.86, 2.50], [4.01, 3.03]],  # [buffer pH, Po voltage]; defaults are uncalibrated
        # with the other probes in the same water the pH reading shifts by a constant amount (ground path
        # through the water). This is subtracted only while the TDS probe is wet (tds_v above ph_wet_tds_v),
        # so a pH-only check (other probes out of the water) still uses the plain buffer calibration.
        "ph_multi_offset": 0.0,
        "ph_wet_tds_v": 0.2,
    },
    "limits": {"tds_dry_v": 0.16, "adc_max_v": 3.05, "min_signal_v": 0.02},
    # smallest change per sensor that counts as meaningful (stops tiny steady noise looking huge)
    "floors": {"temp_c": 1.5, "tds_ppm": 5.0, "turb_v": 0.02, "ph": 0.1},
    "detector": {
        # all times in seconds, so 1 s and 2.3 s firmware behave the same; ewma_lambda is per 1 s
        "warmup_s": 60, "median_s": 5, "ewma_lambda": 0.3, "persist_s": 3,
        "release": 0.8, "quantile": 0.995, "margin": 1.25, "min_sigma": 3.0,
        # drift early warning (CUSUM per sensor, in z*seconds; see drift.py)
        "cusum_k": 0.5, "cusum_h_min": 8.0, "cusum_margin": 1.5,
    },
    # per-model hyperparameters (tuning tab); see models.py
    "model_params": {"pca": {"var_target": 0.90}, "iforest": {"n_estimators": 200},
                     "ocsvm": {"nu": 0.01, "gamma": "scale"}, "lof": {"n_neighbors": 20}},
    # turbidity shown in NTU (display only, models stay on volts); see turbidity.py
    "turbidity": {"ntu_display": True, "v_clear": None, "t_clear": None, "noise_v": 0.003,
                  "temp_coeff": -0.0022, "curve": "generic_vendor", "points": []},
    # what the last training used (None = every usable run, sensors from use_ph, all models); see datasets.py
    "training": {"dataset": None, "files": None, "sensors": None, "models": None},
    "alerts": {
        "sound": True, "browser": True, "notify_recovery": True, "notify_fault": False, "notify_drift": False,
        "notify_cause": False,
        "cooldown_s": 60, "ntfy_server": "https://ntfy.sh", "ntfy_topic": "",
        "telegram_token": "", "telegram_chat_id": "", "line_token": "", "line_user_id": "",
    },
}


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = deep_merge(base[k], v)
        else:
            out[k] = v
    return out


def load_config():
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        user = {}
    return deep_merge(DEFAULT_CONFIG, user)


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
