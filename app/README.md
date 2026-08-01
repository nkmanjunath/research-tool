# Sandbox-to-Vault Web Interface

A clean, local-first web interface for **research-tool**. It brings the core protocol pre-registration engine into an interactive 4-tab workflow: vaulting outcomes before inspection, locking study plans with cryptographic hashes, running unattended diagnostic gates, and generating publication-ready audit binders.

Designed to run strictly on `localhost` as a single-user SPA backed by FastAPI.

---

## Quick Start

### 1. Install Dependencies

From the repository root:

```bash
pip install fastapi uvicorn pandas openpyxl python-multipart statsmodels numpy
```

### 2. Launch Server

```bash
uvicorn app.backend.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

---

## 4-Tab Guided Workflow

```
[01 Data & Schema] ──► [02 Wizard & Lock] ──► [03 Execution] ──► [04 Assets & Binder]
```

1. **`01 · Data & Schema (Blind Inspection)`**
   - Upload raw CSV/Excel files ($H_0$ dataset fingerprint calculated immediately).
   - Set column types and explicitly declare the primary outcome column to vault it from correlation and missingness views.
   - Configure custom missing-value sentinels and review blinded summary statistics.
   - Lock Stage 1 to produce the $H_0$ payload.

2. **`02 · Wizard & Lock (Socratic Pre-Registration)`**
   - Choose primary exposure covariate and confirm outcome settings (read-only).
   - Select confounders, document interaction term rationales ($15+$ character clinical floor), and monitor the live EPV gauge.
   - Choose missing-data handling strategy and seal $H_1$ to lock the protocol.

3. **`03 · Execution (Unattended 4-Gate Diagnostics)`**
   - Execute the locked plan against fixed diagnostic rulesets (no manual tuning knobs exposed).
   - Passes complete separation, VIF multicollinearity, proportional hazards, and continuous linearity checks.
   - **PASS / WARNING**: Proceed directly to publication asset generation ($H_{\text{exec}}$ payload sealed).
   - **FAIL**: Routes to the **Autopsy Canvas** with diagnostic evidence and canned remediation options, preparing a hashed protocol amendment.

4. **`04 · Assets (Publication Package & Audit Binder)`**
   - View Table 1 (baseline characteristics) and Table 2 (adjusted odds ratios with $E$-values).
   - Render SVG forest plots, STROBE checklist items, and manuscript Methods drafts.
   - Download the full cryptographic `study_audit_binder_{hash}.zip` containing data fingerprints, locked protocols, execution results, and automated verification scripts.

---

## Project Structure

```
app/
├── backend/
│   ├── main.py              # FastAPI entry point mounting routers & static files
│   ├── state.py             # Single-user in-memory session state machine
│   └── routers/
│       ├── ingestion.py     # Tab 1 endpoints: upload, schema, vaulting, H0 lock
│       ├── planning.py      # Tab 2 endpoints: exposure, confounders, EPV, H1 lock
│       ├── execution.py     # Tab 3 endpoints: gate evaluation, statsmodels fit, autopsy
│       └── reporting.py     # Tab 4 endpoints: tables, forest plot SVG, zip binder
└── frontend/
    ├── index.html           # Single Page Application shell
    ├── css/app.css          # Clinical lab dark theme, responsive cards & badges
    └── js/
        ├── app.js           # API base routing & tab navigation controller
        ├── tab1.js          # Ingest & schema controller
        ├── tab2_wizard.js   # Protocol wizard & live EPV controller
        ├── tab3_execution.js # Execution engine & autopsy canvas controller
        └── tab4_report.js   # Report asset generation & binder download controller
```

---

## Core Engine Integration Notes

The FastAPI routers in `app/backend/routers/` currently use lightweight native Python helpers so the web app can run standalone out of the box. Key integration points for linking directly to `core/`:

- **`routers/ingestion.py`**: Connect `POST /schema` directly to `core.ingestion.variable_classifier`.
- **`routers/planning.py`**: Connect live EPV calculations directly to `core.planning`.
- **`routers/execution.py`**: Swap `_fit_model` for direct IPC into `core.execution`. (Gate routing, Autopsy payload structures, and hash chaining contracts remain identical).
- **`routers/reporting.py`**: Connect Table 1 SMD calculations and STROBE checklist PDF generation to `core.reporting`.

---

## Current Status & Roadmap

- [x] All 4 tab interfaces fully wired and functional end-to-end.
- [x] Automated diagnostic gate routing (PASS / WARNING / FAIL -> Autopsy Canvas).
- [x] Cryptographic hash chaining ($H_0 \rightarrow H_1 \rightarrow H_{\text{exec}} \rightarrow H_{\text{bundle}}$).
- [x] Standalone zip audit binder creation with embedded verification scripts.
- [ ] Cox Proportional Hazards survival path (currently stubbed for logistic binary models).
- [ ] Direct JSON spec import mode (`entry_mode="direct_spec"` in Tab 2).
