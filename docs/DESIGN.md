# Design system

Water Sentinel is a lab instrument first. People read it at the bench and on a projected screen,
so the one question it must answer at a glance is: **is the water normal right now?**

## Colour

Status colours come from the pH calibration buffers used to set up the probes:

| Token | Light | Meaning |
|---|---|---|
| `--ok` | `#187a4f` (pH 7 buffer green) | normal water |
| `--bad` | `#c2362b` (pH 4 buffer red) | anomaly, alarm line |
| `--accent` | `#1f5fbf` (pH 10 buffer blue) | actions, links, focus |
| `--warn` | `#946010` amber | slow drift (early warning, not an alarm) |
| `--fault` | `#5b6770` graphite | sensor problem (not the water's fault) |
| `--paper` / `--panel` | `#e4eceb` / `#f7faf9` | cool tap-water page and working surfaces |
| `--ink` / `--muted` | `#102a30` / `#4c6469` | text |

Each status has a `-bg` tint for large areas. Sensors (`--sensor-*`), models (`--model-*`) and up to ten
data series (`--series-1..10`) have their own tokens. Dark theme values live in the same file.
All tokens are in `frontend/app/tokens.css`; never write a hex value in a component or in JavaScript.

## Type

- **Atkinson Hyperlegible** (body, 400/700): designed for legibility; good on projectors and for low vision.
- **Barlow Condensed** (600): headings, readouts, chart axes, numbers in tables. Condensed numerals read like an instrument panel and avoid Atkinson's slashed zero in dense figures.
- Scale: 13 / 15 / 18 / 24 / 36 / 54 px (`--step--1` … `--step-4`). Sentence case everywhere; no all-caps labels.

## Layout and shape

- Flat panels (`--panel`) on the page colour. No drop shadows. Radius: 14 px panels, 8 px controls, round only for status pills.
- Content is left-aligned; hints are capped at about 78 characters per line.
- Sensor readouts form one instrument strip divided by rules, not separate floating cards.

## The one expressive element

The overview hero shows the anomaly score as a **graduated cylinder**: the liquid level is the score
(0 at the bottom, the alarm line at 1.0, full at 1.5 and above), the liquid takes the state colour, and the
big number beside it is the same score. Keep it the only decorative element; everything around it stays quiet.
Motion is limited to the liquid level changing, and it is disabled under `prefers-reduced-motion`.

## Writing

- Name things by what the user does: *Record a run*, *Train models*, *Use this model*.
- Buttons say exactly what happens; the toast repeats the same verb (*Save* → *Saved*).
- Errors say what happened and how to fix it, without apologising.
- No emoji in UI chrome (they render inconsistently on Windows). Use the status colour and words instead.
- Never imply safety: an alarm means "differs from the normal water you recorded".

## Quality floor

Responsive to 360 px wide, visible keyboard focus, colour never the only signal (every status also has words),
reduced motion respected, contrast at least 4.5:1 for body text in both themes.
