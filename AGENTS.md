# Rules for AI coding assistants

Read `README.md` (what the app does, project layout) and `docs/ARCHITECTURE.md` (packages, design rules,
extension points) before changing code. Visual changes follow `docs/DESIGN.md`.

## Commands

- Install: `python -m pip install -e ".[dev]"`
- Test: `python -m pytest` (simulator only, no hardware, about 90 s). Run it after every change.
- Lint: `ruff check .`
- Run: `python run.py --sim` (simulated water) or `python run.py` (real ESP32 on USB).

## Rules

- Keep the six design rules in `docs/ARCHITECTURE.md`, above all: training and live scoring share one pipeline,
  `Hub` knows nothing about HTTP, sensor faults are never anomalies, the app writes only to `runtime/`.
- Code, comments and UI text are English. Tests reject Thai text and emoji in the UI.
- Colours come from `frontend/app/tokens.css`; never hard-code hex values in CSS components or JavaScript.
  If you change `tokens.css`, copy it to `frontend/dashboard/tokens.css` (a test checks they match).
- The frontend has no build step. Do not add bundlers, frameworks or CDNs to `frontend/app/` (it must work offline at the bench).
- Never commit anything in `runtime/` (data, models, logs, `config.json`, `edge_secrets.json`) or `firmware/**/secrets.h`.
- Do not delete user data. Runs are archived to `runtime/data/_excluded/`, never removed.
- Do not assume sensor models or pinouts without confirmed data; check voltage compatibility before suggesting wiring.
- Record user-visible changes in `CHANGELOG.md`.
