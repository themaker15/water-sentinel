# Architecture

```
 ┌──────────── edge (laptop next to the water) ────────────┐
 │                                                         │
 │  ESP32 ──USB──> sources.SourceThread ──┐                 │
 │  (simulator) ──> sources.Simulator ────┼─> hub.Hub ──> server (FastAPI) ──> browser on laptop
 │  ESP32 WiFi ──HTTP POST /api/ingest ──┘     │    │                    └──> phone via Tailscale (PIN)
 │                                             │    └─> alerts: ntfy / Telegram / LINE  ──> phone push
 │                     live.LiveMonitor <──────┘
 │                     runtime/model/bundle.joblib <── training.train() <── runtime/data/*.csv
 └─────────────────────────────────────────────────────────┘
```

## Design rules (keep these when changing things)

1. **One pipeline for training and live scoring.** `features.prepare_run` (batch) and `live.LiveMonitor`
   (one sample) apply the same fault check, median filter, model score, smoothing and threshold.
   Change one, change both, and run `pytest`.
2. **Calibration lives on the PC, not in the firmware.** The ESP32 sends pin voltages. The model stores
   a snapshot of the calibration it was trained with, so recalibrating never silently breaks a model.
3. **`Hub` does not know about HTTP.** The web layer only calls Hub methods. Swapping FastAPI for
   something else, or adding MQTT, does not touch detection logic.
4. **Every model plugs in the same way.** A model class needs `key`, `name`, `ewma` (bool),
   `fit(X, floors)` and `score(X)` (higher = less normal). Add it to `models.MODELS` and it appears
   in training, comparison, charts and the live selector automatically.
5. **Faults are not anomalies.** Anything that is the sensor's fault (dry probe, saturated ADC,
   missing sensor) is reported separately and never trains or triggers the water alarm.

## Data contract

CSV per run in `runtime/data/`: `pc_time, ms, run_id, label, temp_c, tds_v, turb_v, ph_pin_v`
(older 3-sensor files with `tds_ppm` and no `ph_pin_v` are still read).
One file = one cup of water = one `run_id`. `label` = `normal` or the contaminant name.

## Extension points

| Want | Where |
|---|---|
| New sensor (e.g. DO, ORP) | firmware DATA line → `sources.parse_data_line` → `features.RAW_COLS`/`derive`/`feature_list` → `config` floors |
| New model | class in `models.py` + add to `MODELS` |
| Laptop-free deployment | ESP32 joins WiFi and POSTs JSON to `/api/ingest` with header `X-Token` (set `ingest_token`) to a small always-on PC / Raspberry Pi running this app |
| Cloud dashboard | publish `hub.state()` snapshots to MQTT (e.g. HiveMQ Cloud) from a thread in `sentinel/app/hub.py`; the web UI already only needs `/api/state` shaped data |
| Model on the ESP32 itself | MSPC is just `mu`, `cov` and a threshold (see `report.json → meta.mspc`): a few lines of C on the ESP32 |

## Security

Localhost needs no login. Other devices need `remote.enabled` + a PIN (cookie after login).
POSTs must be JSON (blocks cross-site requests), the Host header is checked (blocks DNS rebinding),
secrets are masked in `/api/config`, and `runtime/config.json` is git-ignored.
