# Sandbox-to-Vault app — Phase 1 scaffold

All four tabs wired live. Phase 1 feature-complete, several pieces intentionally stubbed (see below).

## Run

```bash
cd app/backend
pip install fastapi uvicorn pandas openpyxl python-multipart statsmodels numpy
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000`. Flow: upload → schema → outcome → sentinels → lock (Tab1) →
exposure → confounders → interactions → strategy → lock (Tab2) → run (Tab3) → generate
assets → build binder (Tab4).

## Where this goes in the real repo

Drop `backend/` + `frontend/` into a new `app/` folder at your existing repo root,
sibling to `core/`, `cli/`, `tests/` — not nested under this wrapper name. `main.py`
will eventually do `from core.ingestion import variable_classifier`, which only
resolves if `core/` is a sibling at repo root.

## Wire real engine (all NOTE-flagged in code)

- `routers/ingestion.py` — naive pandas dtype-guess + inline corr/crosstab → swap for `core.ingestion.variable_classifier`.
- `routers/planning.py` — EPV calc is simplified complete-case dropna → swap for real `core.planning` math.
- `routers/execution.py` — statsmodels Logit stand-in, **binary outcome only, no Cox PH/survival path yet** (needs `lifelines` + real Schoenfeld residuals for Gate 3). Gate 4 (linearity) is a stubbed PASS — needs a real Box-Tidwell check against `pre_specified_transforms`. Swap `_fit_model` for an IPC call into `core.execution` — gate routing / Autopsy payload shape / hash chaining around it is the real contract, shouldn't need to change.
- `routers/reporting.py` — Table 1 has no SMD calc yet (plain mean/sd, n/%), forest plot is a single static SVG (no log/linear or CI-band toggle re-render), STROBE checklist is a plain-text stand-in not a real filled PDF, `verification_script.py` checks the hash chain structurally but doesn't actually re-fit the model — all NOTE-flagged in code.

## Not done here

- Cox PH / survival model path (Gate 3 real implementation)
- Expert direct-JSON-import entry mode for Tab 2 (`entry_mode="direct_spec"`) — only `socratic_wizard` built
- Amendment mode isn't a separate Tab 2 UI state yet — `/api/execute/amendment/prepare` returns the editable-field snapshot, but Tab 2's HTML doesn't yet lock Steps A/B visually or show a "you're amending" banner. Functional via API, not yet reflected in the UI.
- Session persistence beyond single in-memory dict (fine for localhost single-user)
- `--strict-ids` / `--na-values` CLI flags aren't called from here — `/schema`'s
  `has_duplicates` check is a lightweight inline reimplementation, not the real flag
