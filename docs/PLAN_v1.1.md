# Water Sentinel v1.1 plan

> Status 2026-11-16: **WP1-WP6 all implemented** (WP1/3/4 by Opus, WP2/4b/5/6 by Sonnet; see CHANGELOG.md). Remaining: the hardware-side data collection in section 4 and on-device MSPC.
> Deviations: user turbidity points are stored as `[ntu, volts, temp_c]`; MSPC is always trained; label families strip protocol suffixes (`salt_lvl2`, `salt_ramp` -> `salt`).

Written 2026-11-16 as a handoff. The implementing model should read `README.md` and
`docs/ARCHITECTURE.md` first, keep the five design rules there, and run `py -m pytest -q`
(simulator-based, no hardware) after every work package.

Repo: `DIGITAL LAB\02_software\water_sentinel\` (git, branch `master`, one commit).
Stack: Python 3.13, FastAPI, plain HTML/JS + vendored Chart.js. UI text is Thai; code and comments English.

## 1. What the real data showed (evidence for the plan)

Source: `data/run002..006` (all `normal`), `model/report.json`, `logs/events.csv` from 2026-11-15.

| # | Finding | Evidence | Consequence |
|---|---|---|---|
| F1 | The laptop still runs the **old firmware** | sample interval 2.34 s, `ph_pin_v` empty | `median_win` and `persist` are counted in samples, so detection takes about 12-19 s, not the ~6 s designed for 1 s sampling |
| F2 | **Sensitivity is set by the hand-picked floors, not learned** | MSPC sd = `[1.5, 5.0, 0.02]`, exactly the floors; threshold = 9.0 = `min_sigma²` | A single sensor alarms only beyond about ±4.5 °C, ±15 ppm TDS, ±0.06 V turbidity. Real noise is far smaller (TDS sd ≈ 1 ppm, turbidity sd ≈ 0.003 V) |
| F3 | **Five runs are really two conditions** | runs 2-3 at 27.2 °C; runs 4-5-6 started 6 s apart at 29.0 °C (same water) | Leave-one-run-out overstates independence. The app must tell runs from sessions |
| F4 | **The salt tests were not saved** | anomaly events at 17:27 and 17:34 in monitor mode, no anomaly CSV | No detection rate, AUC or delay can be computed. Monitor-mode data is thrown away |
| F5 | Model is stale without warning | trained 17:52 on runs 2-5; run 6 recorded after | UI should say "N new runs since training" |
| F6 | Turbidity voltage moved with the session | 1.858 V at 27.2 °C vs 1.833 V at 29.0 °C (−1.35%) | About 3× the vendor temperature coefficient, so cup/probe position also matters. Any NTU conversion needs a clear-water reference per session and temperature compensation |
| F7 | Clear water reads 1.85 V at 3.3 V supply | all runs | The generic DFRobot curve rescaled to 3.3 V would call this ≈ 2,900 NTU. Absolute voltage formulas do not transfer to this module |

## 2. User-test findings (from the 2026-11-16 screenshot and a code read)

Severity: **A** misleads the user, **B** friction, **C** polish.

- **A1 Stale values look live.** With "no data from sensor" the tiles still show 28.94 °C / 116 ppm / z-scores. Grey the tiles, show "last seen HH:MM (N min ago)", hide z.
- **A2 No alert when the sensor link dies.** The app sat disconnected overnight with monitor mode on. Add a `link_lost` event after a configurable time (default 60 s) with the same notification channels, and `link_restored`.
- **A3 Monitor mode during recording.** A `normal` run can be recorded while the detector is alarming, and nothing is said. Warn at record start and tag the run.
- **A4 Score chart hides magnitude and gaps.** Values are clipped at 5, disconnected periods are joined, and negative scores ("-0.08") are unexplained. Use a log or symlog axis, break the line on gaps > 3× sample interval, clamp display at 0 with a tooltip for the raw value.
- **B1 Temperature sparkline looks like a square wave.** DS18B20 steps of 0.0625 °C are auto-scaled to full height. Give every sparkline a minimum y-span (the sensor floor).
- **B2 Emoji icons render badly on Windows** (thermometer, salt, fog are nearly invisible). Replace with inline SVG.
- **B3 Training was pressed three times in 80 s** (17:12-17:13). Disable the button with a progress state, and show a "trained just now, nothing changed" notice when the data fingerprint is identical.
- **B4 Events list has no date and repeats faults.** Add date separators, collapse repeated identical faults into one row with a count.
- **B5 Comparison table is empty of meaning without anomaly runs.** Show a clear empty state: "record at least one anomaly run to compare models", with a button to the record tab.
- **B6 "Runs" vs "cups".** Add a per-run note field (cup, source, what was added, amount) and warn when a new `normal` run starts within 2 min of the previous one ("same water? this adds little").
- **C1** Firmware version is not shown. Parse the `# Water Sentinel firmware vN` banner or infer from line format, and prompt to upgrade on the old format.
- **C2** `autostart.bat` and remote access are untested on the real machine. Add to the manual test list.

## 3. Work packages

Order: WP1 → WP2 → WP4b → WP3 → WP4 → WP5 → WP6. Each lists files and acceptance criteria.

### WP1. Correctness and UX fixes (do first)
1. **Time-based detector settings.** Replace `median_win`/`persist` (samples) with `median_s` (default 5) and `persist_s` (default 3); convert to samples from the measured interval, in training and live, and store the interval in the bundle. Files: `config.py`, `features.py`, `models.py`, `live.py`, `training.py`.
2. **Always-on rolling log.** `hub.py` writes every sample to `logs/stream-YYYYMMDD.csv` (raw columns + status + ratio), keep 14 days. Add "Save last N minutes as a run" and auto-snapshot from 2 min before to 5 min after each anomaly event into `data/` as `runNNN_unlabeled_...` for the user to label. This fixes F4.
3. **Sessions.** Add `session_id` (new id when the gap since the previous run > 10 min or the user says "new water"). Evaluation folds leave one *session* out when ≥ 3 sessions exist; report both run count and session count. Fixes F3.
4. A1-A4, B1-B6, C1 from section 2, plus the stale-model pill (F5).

Accept: tests updated; simulator test proves a monitor-mode anomaly produces a saved snapshot; with the old 2.3 s firmware format the alarm delay in seconds matches the 1 s format within one sample.

### WP2. Data explorer on the Overview page
- Mode switch on the Overview tab: **Live** | **Recorded**.
- Recorded mode: multi-select runs (table with label, session, note, rows, duration). Charts per sensor with runs overlaid (aligned at t = 0) or concatenated; toggle raw vs median-filtered; shade warm-up, fault spans and event markers; optional score trace from the active model (replay).
- Per-run characteristics card: mean, sd, min/max, drift slope per minute (Theil-Sen), noise (MAD of first differences), settling time (time until the rolling mean stays within 1 floor of the final value), fault count, sample interval, firmware.
- Run management: edit label/note, exclude from training (flag, not delete), move to `data/_excluded/`. Never delete files from the UI.
- API: `GET /api/run/{file}` (downsampled series + stats + events), `POST /api/run/{file}/meta`. Metadata lives in a sidecar `data/runNNN_....json` so CSVs stay untouched.
- Export: chart PNG and a stats CSV for the report.

Accept: opening a 130-row run renders in < 300 ms; overlaying 5 runs works on a phone-width screen.

### WP3. Unsteady state and gradual contamination
Algorithm:
1. **Event markers.** "Mark event" button during recording (preset chips: added 1 mL stock, stirred, lifted probe; free text; optional cumulative dose). Stored in the sidecar. Evaluation treats rows before the first dosing marker as normal and after it as anomalous; detection delay is measured from the marker. Runs without markers behave as today.
2. **Drift channel next to the level channel.** Keep MSPC on levels. Add a per-sensor one-sided CUSUM pair on standardized residuals (reference k = 0.5, decision h tuned on normal sessions) to catch small sustained shifts earlier, and a slope estimate (Theil-Sen over a 60 s window) with "time to threshold" extrapolation. Alarm states become: `normal`, `drifting` (early warning, no push by default), `anomaly`.
3. **Keep the baseline fixed.** No automatic baseline adaptation: an adaptive baseline would absorb slow contamination. Offer only an explicit "re-reference to current water" action that is logged.
4. **Offline change-point** for the explorer: binary segmentation on the MSPC score to shade detected regimes in recorded runs.

Evaluation and visualization:
- Ramp protocol in the record tab: baseline 2 min, then dose every 30 s with markers.
- **Dose-response chart:** x = cumulative dose or estimated concentration, y = score, with threshold line and the minimum detectable change per model.
- **Trajectory view:** path through PCA space coloured by time, with the normal ellipse.
- **Contribution-over-time:** stacked per-sensor share of the Mahalanobis distance.
- New comparison metrics: detection delay from marker, minimum detectable dose, early-warning lead time of the drift channel, false drift warnings per hour on normal sessions.
- Simulator: add `ramp_salt`, `ramp_starch`, `pulse` scenarios and tests for each.

Accept: on the simulator ramp, `drifting` fires before `anomaly`; on normal sessions the drift channel gives zero warnings in the test data.

### WP4. Hyperparameter tuning in the UI
- New "Tuning" section on the model tab with three groups:
  - **Instant (no refit, replayed on stored scores):** threshold margin, persist time, release ratio, EWMA λ, CUSUM k/h.
  - **Refit (about 1 s):** floors per sensor, median time, warm-up, quantile, PCA variance target.
  - **Per model:** IsolationForest trees, OCSVM ν and γ, LOF neighbours.
- Presets: Sensitive / Balanced / Conservative, plus "Reset to defaults".
- **Effective detection limits card** per sensor in real units ("alarms at about ±15 ppm TDS"), computed from the fitted model. This makes F2 visible and is the main guide for choosing floors.
- **"Suggest floors from data"**: floor = max(sensor resolution, 3 × between-session sd of run medians); needs ≥ 3 sessions; show the suggestion, never apply silently.
- **What-if preview** before saving: false-alarm rate, detection rate and delay under the candidate settings, shown next to the current ones; sweep chart of margin vs false alarm / detection.
- Settings snapshot is stored in the bundle (already true); add a small history list with "restore".
- Guard rails: validate ranges server-side, and state plainly that tuning on the same runs used for evaluation is optimistic.

Accept: changing an instant parameter updates the preview without a refit; a restored snapshot reproduces the earlier report numbers exactly.

### WP4b. Choosing which data trains the model
- **Training set picker** on the model tab: a table of all runs with checkboxes, grouped by session, showing label, date, rows, note, fault count and a role column: `train (normal)`, `test only (normal)`, `anomaly test`, `excluded`. Quick filters: by date range, session, label, "has pH", "new since last training". Bulk actions: select all in session, invert, clear.
- Roles: a `normal` run can be **train** or **test only** (held out, never fitted, always reported as an honest false-alarm check). Anomaly runs can be included or excluded from evaluation and from the cause centroids (WP6) separately.
- Sensor selection: choose which sensors enter the model (any subset of temperature, TDS, turbidity, pH; at least 2), not only the 3-vs-4 switch.
- Model selection: tick which of the five models to train and compare.
- **Named datasets**: save a selection as a named set ("lab bench Nov", "with pH"), stored in `data/datasets.json`. The bundle records the dataset name, the exact file list and a content hash, and the report shows them, so every result can be traced to its data.
- Guard rails before training: block with a clear message when there is no train-normal run; warn when fewer than 3 sessions, when selected runs mix files with and without pH, when calibration differs between runs, or when a run has > 10% fault rows.
- API: `POST /api/train` accepts `{dataset | files, roles, sensors, models}`; with no body it behaves as today (all usable runs). CLI: `py -m sentinel train --dataset NAME`.
- Compare two trainings side by side (current vs previous) on the model tab.

Accept: training on a chosen subset reproduces exactly when repeated; a "test only" normal run never influences the fitted parameters (test by checking the bundle is identical with and without it); the default path with no selection is unchanged.

### WP5. Turbidity in NTU (display only; the model keeps volts)
Research result:
- There is no "Thai tap water calibration curve". The curve belongs to the sensor, not to the water. Thai tap water is the low end of it: PWA reports 0.26-4 NTU against a limit of 4 NTU (WHO-based). MWA's limit should be confirmed from MWA directly before it is quoted.
- Generic curve (DFRobot SEN0189, same sensor type, 5 V): `NTU = −1120.4 V² + 5742.3 V − 4352.9`, valid 2.5-4.2 V. Forum users report about ±7 NTU resolution at best and recommend formazin or a reference meter. The cybertice chart for TSW-30 gives about 4.5 V at 0 NTU, 3.9 V at 1000, 2.6 V at 3000 (10 °C), falling about 0.22%/°C.
- This module reads 1.85 V in clear water (F7), so only a **ratio method** is usable: `r = V / V_clear`, then a curve in `r`.
- Expected resolution here: single-sample noise 0.003 V is r ≈ 0.0016, about 12-25 NTU depending on the curve; a 60 s average may reach roughly ±5 NTU. **Tap-water turbidity (< 4 NTU) is below what this sensor can resolve.**

Design:
1. `features.turbidity_ntu(turb_v, temp_c, calib)` → `(ntu, quality)`; never part of `feature_list`.
2. Calibration object in `config.json`: `v_clear`, `t_clear`, `temp_coeff` (default −0.0022 per °C, fit from the user's own normal sessions when ≥ 3 exist), `curve` = `generic_dfrobot` | `generic_vendor` | `user_points`, `points: [[ntu, r], ...]` with monotone interpolation (PCHIP) and no extrapolation beyond the last point.
3. Settings UI: "Set clear-water reference" button (median of last 30 s), point table for user standards, curve preview chart with the live point on it.
4. Display rules: below the detection limit show "< X NTU" where X = 3 × noise converted through the curve; label generic curves "estimate (uncalibrated)"; show volts on hover. NTU appears on the tile, explorer and reports; alarms and models stay on volts.
5. Calibration procedure page: preferred route is to make 4-5 suspensions (kaolin, or diluted milk as a stand-in), read their NTU on the department's benchtop turbidimeter, then enter the points. Commercial sealed standards are the alternative. Mixing formazin from hydrazine sulfate is not recommended outside a supervised lab.

Accept: with only `v_clear` set, clear water displays "< X NTU", not a number; with user points, the displayed NTU matches the entered points within interpolation error; model outputs are byte-identical with NTU display on or off.

### WP6. Experimental: likely cause
Clearly badged "experimental" in the UI and off by default in notifications.
1. **Fingerprint.** For an alarm, take the median z-vector over the alarm window (ΔT, ΔTDS, Δturbidity, ΔpH if present), sign and relative magnitude.
2. **Rule layer** (works with zero labelled anomalies): a table in `sentinel/causes.py`, e.g. TDS↑ only → dissolved salts; turbidity voltage↓ with TDS flat → suspended solids; T↑ with small TDS↑ → temperature change; TDS↑ and pH↑ → alkaline salt (baking soda); TDS↑ and pH↓ → acid; everything jumps at once then a fault → probe disturbed.
3. **Learned layer** when labelled anomaly runs exist: nearest-centroid on unit-normalised fingerprints with cosine similarity, one centroid per label family (strip `_lvlN`). Open-set: if the best similarity < 0.8 or the top two are within 0.05, answer "unknown / ambiguous".
4. Output: top 3 candidates with similarity, the sensors that drove it, and what would disambiguate ("add pH to separate salt from baking soda").
5. Evaluate honestly: leave-one-anomaly-run-out confusion matrix on the model tab. With 3 sensors, salt and baking soda are expected to be indistinguishable; say so in the UI.
6. Wording: "consistent with", never "is". It suggests a class of cause, not a substance, and says nothing about safety.

Accept: simulator scenarios map to the right family; an unseen scenario returns "unknown".

## 4. Data the user needs to collect (blocks WP3, WP4, WP6 evaluation)
1. Upload `firmware/water_sentinel/water_sentinel.ino` (1 s sampling, pH pin).
2. Normal: at least 3 more **sessions** on different days, fresh water each run.
3. Anomaly step runs: `salt_lvl1`, `salt_lvl2`, `starch_lvl1`, `baking_soda_lvl1`, `hot_water`.
4. After WP3: one ramp run each for salt and starch, with markers.
5. For WP5: one clear-water reference per session, and turbidimeter readings for 4-5 suspensions.

## 5. Out of scope here
On-device MSPC for the ESP32 with LED and WiFi alerts was agreed separately and should follow WP1, because it must use the time-based parameters. pH hardware bring-up (10k/10k or 4.7k/4.7k divider, two-point calibration) is unchanged.

## 7. After v1.1 (2026-11-17, Opus 5.5)

**Viewer link (done).** Tunnelled requests (proxy headers or non-local Host) are never treated as "this computer". Read-only viewer role (`remote.public_view`): every POST refused server-side, secrets stripped, no expensive compute. Settings → "Share" card starts a Cloudflare quick tunnel (`share.py`) or takes a Tailscale Funnel URL; full-screen QR. PIN login: 6+ chars, lock-out after 8 wrong in 15 min.

**Target architecture (agreed direction):**
```
ESP32: sensors -> median -> MSPC (EWMA + persistence) -> LED/buzzer + ntfy push      (always on)
   |-- USB/Wi-Fi --> laptop app: record, train, tune, explore, export model -> flash    (when open)
   '-- Wi-Fi, 15 s --> ThingSpeak channel (8 fields) <-- public dashboard on Netlify    (24/7, read-only)
```

**Edge MSPC (simulated, done).** `edge.py` exports MSPC (JSON + C header `edge_model.h`) and has a plain-maths `EdgeDetector` the firmware will copy; a test proves identical scores/alarms vs the app. `py -m sentinel edge-sim` runs it on the laptop with simulated sensors -> ThingSpeak (fields: temp, TDS, turb V, pH, score, status 0/1/2/3, driver 1-4, driver z; worst status + peak score per 15 s window) + ntfy on alarm/recovery. Keys in `edge_secrets.json` (git-ignored).

**Next:** user creates ThingSpeak channel + ntfy topic and runs edge-sim; then Netlify dashboard (needs channel ID + read key); then real ESP32 firmware (open choices: LED vs buzzer active/passive, which Wi-Fi; no captive-portal networks).

## Sources
- DFRobot curve and 3.3 V discussion: https://forum.arduino.cc/t/getting-ntu-from-turbidity-sensor-on-3-3v/658067
- PWA water quality report 2568 (turbidity limit and measured range): https://reg10.pwa.co.th/pwa/upload/ccr_upload/2568/11.pdf
- TSW-30 vendor page and chart: https://www.cybertice.com/product/2270/
