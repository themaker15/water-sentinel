# Architecture

```
 ┌──────────────────── laptop next to the water ─────────────────────┐
 │                                                                   │
 │  ESP32 ──USB──> acquisition.sources.SourceThread ──┐              │
 │  (simulator) ──> acquisition.sources.Simulator ────┼─> app.hub.Hub ──> app.server (FastAPI) ──> frontend/app (browser)
 │  ESP32 Wi-Fi ──HTTP POST /api/ingest ─────────────┘     │    │                  └──> phone via Tailscale (PIN)
 │                                                         │    └─> app.alerts: ntfy / Telegram / LINE ──> phone push
 │                     detection.live.LiveMonitor <────────┘
 │                     runtime/model/bundle.joblib <── detection.training.train() <── runtime/data/*.csv
 └───────────────────────────────────────────────────────────────────┘

 ESP32 (board-only mode): edge MSPC ──Wi-Fi──> ThingSpeak ──> frontend/dashboard (public, read-only)
```

## Packages

| Package | Responsibility | Depends on |
|---|---|---|
| `sentinel.config` | defaults, folder paths (`ROOT`, `HOME` = `runtime/`), load/save `config.json` | nothing |
| `sentinel.acquisition` | turning raw lines into readings: serial/simulator sources, derived features, faults, turbidity in NTU | config |
| `sentinel.storage` | files on disk: runs + sidecar metadata, named datasets, explorer statistics | acquisition |
| `sentinel.detection` | models, training and evaluation, tuning, drift (CUSUM), likely cause, live scoring | acquisition, storage |
| `sentinel.edge` | exporting MSPC for the ESP32 and simulating the board | detection |
| `sentinel.app` | the running program: `Hub` (state, threads, events), FastAPI server, alerts, public link | everything above |

Dependencies point one way (down the table). `app` is the only package that knows about threads and HTTP.

## Design rules (keep these when changing things)

1. **One pipeline for training and live scoring.** `acquisition.features.prepare_run` (batch) and `detection.live.LiveMonitor`
   (one sample) apply the same fault check, median filter, model score, smoothing and threshold.
   Change one, change both, and run `pytest`.
2. **Calibration lives on the PC, not in the firmware.** The ESP32 sends pin voltages. The model stores
   a snapshot of the calibration it was trained with, so recalibrating never silently breaks a model.
3. **`Hub` does not know about HTTP.** The web layer only calls Hub methods. Swapping FastAPI for
   something else, or adding MQTT, does not touch detection logic.
4. **Every model plugs in the same way.** A model class needs `key`, `name`, `ewma` (bool),
   `fit(X, floors)` and `score(X)` (higher = less normal). Add it to `detection.models.MODELS` and it appears
   in training, comparison, charts and the live selector automatically.
5. **Faults are not anomalies.** Anything that is the sensor's fault (dry probe, saturated ADC,
   missing sensor) is reported separately and never trains or triggers the water alarm.
6. **The app writes only to `runtime/`.** Code folders stay read-only at run time, so the repository can be
   updated with `git pull` without touching data, models or keys.

## Data contract

CSV per run in `runtime/data/`: `pc_time, ms, run_id, label, temp_c, tds_v, turb_v, ph_pin_v`
(older 3-sensor files with `tds_ppm` and no `ph_pin_v` are still read).
One file = one cup of water = one `run_id`. `label` = `normal` or the contaminant name.
Metadata (note, session, role, markers) lives in a sidecar `runNNN_....json` so CSVs are never edited.

Model bundles are pickles that record each class's module path. `sentinel/__init__.py` maps the
pre-2026-11-21 flat module names (`sentinel.models`, ...) to their new packages so old bundles still load.
If you move a model class again, add its old path there.

## Frontend

Plain HTML, CSS and JavaScript with no build step, so it runs offline at the bench and the dashboard folder
can be dropped onto Netlify as is.

| File | Role |
|---|---|
| `frontend/app/tokens.css` | every colour, type size and radius (light + dark). Charts read these at draw time |
| `frontend/app/fonts.css` | self-hosted fonts (`vendor/fonts/`, SIL OFL) |
| `frontend/app/style.css` | components; uses only tokens |
| `frontend/app/app.js` | state polling, overview, record, model, visualization, settings |
| `explorer.js`, `training.js`, `turbidity.js`, `share.js` | one feature each, loaded before `app.js` |
| `frontend/dashboard/` | standalone public page; `tokens.css` must stay identical to the app's (a test checks this) |

Colours in JavaScript go through `C.<token>`, `MODEL_COLORS.<key>`, `SENSOR_COLORS.<key>` or `seriesColor(i)`;
never hard-code hex values. Visual rules are in [DESIGN.md](DESIGN.md).

## Extension points

| Want | Where |
|---|---|
| New sensor (e.g. DO, ORP) | firmware DATA line → `acquisition.sources.parse_data_line` → `acquisition.features` (`RAW_COLS`, `derive`, `feature_list`) → floors in `config` → `--sensor-<key>` in `tokens.css` and an entry in `SENSORS` in `app.js` |
| New model | class in `detection/models.py` + add to `MODELS` + `--model-<key>` colour in `tokens.css` |
| New page or tab | a `<section class="view" id="v-name">` in `index.html`, a tab button, and a feature file loaded before `app.js` |
| New API endpoint | a Hub method in `app/hub.py` first, then a thin route in `app/server.py`; viewers are read-only by default (every non-GET is refused) |
| Another UI language | UI text is English; move strings into a dictionary per language and look them up through one `t(key)` helper |
| Laptop-free deployment | ESP32 joins Wi-Fi and POSTs JSON to `/api/ingest` with header `X-Token` (set `ingest_token`) to an always-on PC or Raspberry Pi running this app |
| Cloud dashboard | already done for board-only mode (ThingSpeak + `frontend/dashboard`); for the full app, publish `hub.state()` snapshots to MQTT from a thread in `app/hub.py` |

## Security

Localhost needs no login. Other devices need `remote.enabled` + a PIN (cookie after login).
POSTs must be JSON (blocks cross-site requests), the Host header is checked (blocks DNS rebinding),
secrets are masked in `/api/config`, and `runtime/` is git-ignored. The public dashboard contains only the
ThingSpeak **read** key; the write key stays in `runtime/edge_secrets.json` and the firmware's `secrets.h`.
