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
    "remote": {"enabled": False, "pin": "", "allowed_hosts": []},
    # Token an ESP32 on WiFi must send (header X-Token) to POST readings to /api/ingest.
    "ingest_token": "",
    "calib": {
        "tds_offset_v": 0.142,   # TDS pin voltage with the probe dry in air
        "tds_k": 1.0,            # known ppm / measured ppm in a standard solution
        "ph_points": [[6.86, 2.50], [4.01, 3.03]],  # [buffer pH, Po voltage]; defaults are uncalibrated
    },
    "limits": {"tds_dry_v": 0.16, "adc_max_v": 3.05, "min_signal_v": 0.02},
    # smallest change per sensor that counts as meaningful (stops tiny steady noise looking huge)
    "floors": {"temp_c": 1.5, "tds_ppm": 5.0, "turb_v": 0.02, "ph": 0.1},
    "detector": {
        "warmup_s": 60, "median_win": 5, "ewma_lambda": 0.3, "persist": 3,
        "release": 0.8, "quantile": 0.995, "margin": 1.25, "min_sigma": 3.0,
    },
    "alerts": {
        "sound": True, "browser": True, "notify_recovery": True, "notify_fault": False,
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
