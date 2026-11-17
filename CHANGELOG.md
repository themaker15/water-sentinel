# Changelog

- edge-sim: fake water is now centred on the trained model (`--raw` keeps the old fixed values), so "normal" no longer scores as an anomaly when the model was trained on real water.

## 1.1.0 (in progress)

### Edge (ESP32) MSPC, simulated
- `edge.py`: MSPC export (JSON + C header) and a plain-maths `EdgeDetector` that the firmware will copy; a test
  proves it gives the same score and alarms as the app's live monitor.
- `edge_sim.py` (`py -m sentinel edge-sim`): simulated board -> on-device MSPC -> ThingSpeak (8 fields, every
  15 s, worst status and peak score of the window) + ntfy on alarm/recovery. Keys in `edge_secrets.json` (ignored by git).

### Viewer link for presentations
- **Security fix:** a tunnel (Cloudflare, Tailscale Funnel) connects from 127.0.0.1, which used to count as "this
  computer" with no login. Requests with proxy headers or a non-local Host are now always treated as remote.
- New read-only **viewer** role (`remote.public_view`): no PIN needed to look; every POST and the API docs, port
  list and compute-heavy endpoints are refused server-side; secrets are removed from `/api/config`; viewers only
  get embeddings that are already cached.
- Share card: start/stop a Cloudflare quick tunnel from the app (`share.py`), or paste a Tailscale Funnel address;
  full-screen QR code (vendored qrcode-generator, MIT). Starting a tunnel works only from this computer.
- PIN login: async delay (no longer blocks the server), lock-out after 8 wrong PINs in 15 min, secure cookie over
  https, PIN must be 6+ characters before the viewer link can be turned on.

### WP5: turbidity in NTU (display only; models stay on volts)
- `turbidity.py`: ratio method r = V (temperature-corrected) / V_clear, because this module reads ~1.85 V in clear
  water at 3.3 V, so absolute 5 V datasheet curves do not transfer. Curves: DFRobot-style, vendor-style
  (both labelled "ประมาณ / estimate"), and the user's own standards (PCHIP, no extrapolation).
- Detection limit from the measured noise: below it the tile says `< N NTU` instead of a made-up number.
  States: no reference, below limit, ok, over range, off, error.
- Settings card: set clear water, choose curve, add standards (reads the volts now), delete points, temperature
  coefficient suggestion fitted from the user's own sessions (never applied silently), live curve chart.
- Tiles and the explorer show NTU (volts on hover / toggle); the sparkline, fingerprint and models are untouched.

### WP2: recorded-data explorer (Overview -> "ข้อมูลที่บันทึกไว้")
- Pick up to 8 runs, overlay aligned at t = 0 or concatenated, raw / median / both, NTU or volts, optional score
  replay with the active model (flagged when the run was in the training set).
- Per-run characteristics: mean, SD, min, max, robust slope per minute (Theil-Sen), white-noise estimate (MAD of
  differences), settling time. Warm-up and sensor-fault spans and dose markers are drawn on the charts.
- Exports: PNG per chart, CSV of the characteristics (`/api/runs/export.csv`).
- Run management never deletes: "ย้ายเก็บ" moves CSV + sidecar to `data/_excluded/` and can be restored.

### WP4b: choose what trains the model
- Per-run roles **train / test-only / anomaly / exclude**. Test-only normal runs are never fitted; every model is
  scored on them (`test_false_alarm`), the honest number.
- Sensor subset (>= 2), model subset (MSPC is always trained: it explains causes and drives drift), named datasets in
  `data/datasets.json` (definitions only, never data), `py -m sentinel train --dataset NAME`.
- Guard rails before training (blockers: no train run; warnings: < 3 sessions, no anomaly, runs without pH, many
  faulty rows, calibration changed between runs).
- Fingerprint covers the selection, so "nothing changed" stays exact; history stores dataset, roles, sensors,
  models; the model tab compares any two trainings and says what differed.

### WP6: experimental likely cause
- `causes.py`: fingerprint = median z-vector of the alarm window as a unit vector. Rule prototypes (salts,
  alkaline salt, acid, suspended solids, warmer, cooler, probe disturbed) work with zero labelled anomalies; the
  user's own labelled anomaly runs add nearest-centroid candidates (one per label family: `salt_lvl2` and
  `salt_ramp` are both `salt`).
- Open set: best cosine < 0.80 -> "unknown"; top candidates within 0.05 -> "ambiguous" and the headline names every
  tied candidate. Wording is always "consistent with", never an identification, never a safety statement.
- Leave-one-anomaly-run-out confusion matrix on the model tab; live card on the Overview;
  `alerts.notify_cause` (off by default) appends the guess to push messages.
- Known limit, shown in the UI: with three sensors plain salt and baking soda both only raise TDS; pH separates them.

### WP1: correctness and UX
- Detector timing is in **seconds** (`median_s`, `persist_s`, `ewma_lambda` per 1 s) and converted with the
  sample interval measured from the device clock. Old 2.3 s firmware and new 1 s firmware now alarm after
  about the same time. Even median windows are rounded up to odd, persistence is rounded up.
- **Sessions** (batches of water): runs closer than `session_gap_min` (10 min) count as the same water unless
  "new water" is ticked when recording. Evaluation leaves out whole sessions when there are 3+.
- **Nothing seen live is lost**: every sample goes to `logs/stream-YYYYMMDD.csv` (14 days), every alarm
  auto-saves 2 min before to 5 min after as an `unlabeled` run, and "save last N minutes" turns monitor-mode
  tests into runs. Unlabeled runs are skipped by training until they are named.
- **Sidecar metadata** `data/<run>.json` (label, note, excluded, new_water, markers); CSVs are never rewritten.
- Training is skipped when data and settings are unchanged (fingerprint); `force` retrains anyway.
- Link watchdog: `link_lost` / `link_restored` events with push alerts while monitoring.
- UI: stale values are greyed with "last seen", score chart on a log axis with gaps, sparklines keep a
  minimum range, SVG sensor icons, events grouped by day with repeats collapsed, runs table with session /
  source / note / edit / exclude, firmware and "new runs not trained" warnings, model-tab empty states.

### WP4: hyperparameter tuning
- `tuning.py`: two-stage what-if preview (stage 1 fits and raw scores, cached; stage 2 smoothing, threshold,
  persistence, CUSUM). Preview numbers equal the training report exactly (tested).
- Model hyperparameters in `config.json -> model_params` (PCA variance target, IF trees, OCSVM nu/gamma,
  LOF neighbours); server-side range validation for every tunable value.
- Detection limits in real units per sensor and direction for every model (stored in the bundle).
- Suggested floors from data: max(sensor resolution, 3 x sd of per-session medians), needs 3+ sessions.
- Presets (sensitive / balanced / conservative), settings history with exact restore.
- Tuning card on the model tab: instant sliders, refit fields, current vs candidate table, limits table,
  margin sweep chart, history.

### WP3: unsteady state and gradual contamination
- **Drift early warning** (`drift.py`): per-sensor CUSUM on MSPC z-scores, integrated over time so it is
  sampling-rate independent; thresholds `h` calibrated leave-one-session-out on normal water. New live status
  `drifting` (amber) with trend per minute and estimated time to the alarm boundary (Theil-Sen, 60 s).
  The baseline is never adapted. CUSUM is cleared during an alarm and capped at 2h so it recovers quickly.
- **Event markers** while recording (dose with amount, stir, probe moved, free text) stored in the sidecar.
  Evaluation splits dosed runs: before the first dose = normal water (false alarms), after = anomaly; delays
  are measured from the dose; minimum detectable cumulative dose per run.
- New metrics: drift false warnings per hour (held-out), drift lead time before the level alarm.
- Offline change points (binary segmentation) on the MSPC score per run.
- Visualization: per-run sensor contribution over time with markers / change points / drift bands,
  trajectory in PCA space with the normal ellipse, dose-response chart.
- Simulator: `ramp_salt`, `ramp_starch`, `pulse`.

## 1.0.0 (2026-11-15)
First release: five detectors with leave-one-run-out comparison, FastAPI web app, alerts, simulator.
