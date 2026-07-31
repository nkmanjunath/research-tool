# Decisions Log — research-tool App Architecture (Sandbox-to-Vault)

**Date:** 2026-07-26
**Scope:** Full end-to-end architecture, Tab 1 through Tab 4. Pivot from CLI to app; Novice/Expert convergent flow design; complete data contracts for all four stages.

---

## 0. Scope Pivot

- Product is now an **app**, not a CLI extension. Existing `research-tool` core (v2.7.0-core, 242 tests passing) becomes the underlying statistical engine, called via IPC from Tab 3.
- Not retrospective-only long-term. The Sandbox-to-Vault architecture is **study-design agnostic** — the math (EPV, masking, locking) doesn't care about retrospective vs. prospective vs. case-control vs. cross-sectional.
- **Phase 1 ships retrospective-only.** Other study designs are stubbed (see §6) but not built, to avoid scope explosion before the core app skeleton is proven end-to-end.
- A separate idea — a full biochemistry/research-methods **teaching curriculum** — was explicitly rejected as part of this app. Different audience (no dataset yet), different pedagogy (open exploration vs. locked rigor), would pollute the masking-gate integrity. May exist later as a *separate* product that feeds students toward this tool, not inside it.

---

## 1. Core Principle: Structural Flow Divergence, Convergent Gate

Novice and Expert paths are **not** the same pipeline with different copy. They are structurally different upfront flows that **converge on one identical rigor engine** at lock time.

```
NOVICE FLOW:  [Blinded Inspection] → [Guided Hypothesis Wizard] → [Lock & Hash Gate] → [Execution & Output]
EXPERT FLOW:  [Direct Plan Spec / JSON Import] ────────────────────────┘
```

Rationale: language-only softening fails because a first-time user forced into a linear "pre-register → lock → execute" pipeline doesn't yet know what covariates belong in their model. They need a pre-analysis workspace that *builds up to* the lock gate, not a friendlier error message at the same gate.

Rigor itself (EPV thresholds, masking, immutable locks) is **never softened or skippable** for novices — only the on-ramp to reaching the gate differs.

---

## 2. Provenance DAG — Unified Numbering (applies across all tabs)

To avoid hash-label collisions discovered mid-design, the system uses **two independent numbering tracks**:

1. **Pipeline stage hashes** (linear, one per major stage): `H0` (data) → `H1` (locked plan) → `Hexec` (execution results) → `Hbundle` (Tab 4 export bundle).
2. **Plan amendment hashes** (branch within Stage 2, only if Autopsy triggers a revision): `H1` (original plan) → `H2...Hn` (successive amendments). `Hexec` and `Hbundle` always reference whichever plan hash was actually executed via `parent_plan_hash` — never assume `H1` is always the executed plan.

This distinction must be documented explicitly in any implementation manifest (e.g., a comment in `manifest.json`) so it doesn't read as one flat linear chain to someone unfamiliar with the system.

---

## 3. Tab 1 (Data & Schema) — Blinded Inspection

**Purpose:** pure data-shaping function. Raw file in, immutable `Stage1Payload` contract out. Read-only inspection — no drop/impute/interaction actions happen here.

**What's allowed (safe by construction — no outcome signal):**
- Missingness % per column
- Covariate-covariate correlation / redundancy checks (|r| > 0.8 flagged as "Multicollinearity & Redundancy," not an open-ended correlation matrix)
- Covariate-covariate sparse cross-tab / cell-count checks
- A single **fixed, static cohort banner**: `N total, E total, event rate %` — computed once, never recomputed per user action, decoupled from the variable browser

**What's blocked:**
- Any cross-tab or correlation **against the outcome**
- Outcome column identity/name, once declared, is vaulted from the variable browser, redundancy matrix, and all cross-tabs for the rest of Tab 1

**Outcome declaration — direct, not heuristic:**
- User explicitly selects the outcome column and confirms event-indicator polarity (e.g., which value = event vs. censored) during Stage 1.
- Rejected: auto-detecting outcome column by name pattern (`event`, `status`, etc.) — clinical data coding is inconsistent and inversion-prone (e.g., is `1` = alive or dead?). STROBE/TRIPOD also mandate investigator pre-specification of the primary endpoint, not algorithmic detection.
- Naming/pointing at a column (without seeing its values or associations) is a metadata declaration, not a statistical peek — safe.

**Why covariate-covariate inspection is safe:** true p-hacking requires observing how predictor choices change the outcome association. Correlating Age vs. BP touches the null distribution of neither variable's relationship to the endpoint. It's necessary data hygiene (e.g., catching Weight-kg vs Weight-lbs duplication) and matches standard blinded-Table-1 practice in trials.

**Guardrail against soft interaction-fishing:** users cannot create interaction terms in Stage 1. Redundancy checks are framed as data-hygiene tools, not an open exploration playground.

**Missing-value sentinels:** user declares which string values mean "missing" (globally and per-column overrides) in Stage 1. Missingness % is only computed *after* sentinels are applied. The tool never guesses which strings mean missing vs. a real category.

### `Stage1Payload` contract (H0)

```json
{
  "provenance": {
    "dataset_fingerprint": "sha256_of_raw_file_bytes",
    "payload_fingerprint_h0": "sha256_of_canonicalized_payload_excluding_this_field",
    "ephemeral_raw_path": "/local/path/to/file.csv"
  },
  "cohort_facts": {
    "n_total": 340,
    "e_total": 47,
    "event_rate": 0.1382
  },
  "outcome_spec": {
    "column_name": "os_event",
    "event_value": 1,
    "censored_value": 0,
    "vaulted": true
  },
  "column_mappings": [
    { "name": "pt_id", "type": "identifier", "has_duplicates": false },
    { "name": "age", "type": "numeric_covariate" },
    { "name": "os_event", "type": "primary_outcome" }
  ],
  "sentinels": {
    "global_na_strings": ["NA", "N/A", "unknown", "?"],
    "column_overrides": {
      "income": ["refused", "declined", "-999"]
    }
  },
  "precomputations_advisory_cache": {
    "is_advisory": true,
    "missingness_pct": { "age": 0.02, "income": 0.38 },
    "high_correlation_pairs": [{ "var1": "sbp", "var2": "map_bp", "r": 0.94 }],
    "sparse_cross_tabs": [{ "var1": "rare_mut", "var2": "stage", "min_cell_count": 1 }]
  }
}
```

**Hardening notes:**
- `dataset_fingerprint` hashes raw file bytes only (immutable input reference).
- `payload_fingerprint_h0` hashes the full canonicalized payload (sentinels + mappings + outcome_spec + precomputations) **excluding itself** — compute hash first, insert field after. Circular-hash implementation gotcha; must be handled correctly in code.
- `ephemeral_raw_path` is explicitly non-load-bearing. Cross-machine/session reproducibility relies on `dataset_fingerprint` content hash, never the filesystem path.
- `column_overrides` shape: `Dict[str, List[str]]`.
- `precomputations_advisory_cache.is_advisory = true`: these are UI acceleration hints only. Stage 3's execution engine always recomputes statistics fresh from raw data + rules at lock/execution time — never trusts this cache as authoritative.

**Open follow-ups tied to existing backlog:**
- `--strict-ids` duplicate-patient-id check → Implemented in `core.cli.main` (`lock` command `--strict-ids` flag) & surfaced in Tab 1's ID-column mapping step. Blocks plan lock when duplicate patient IDs exist.
- `--na-values` custom sentinel flag → this *is* the `sentinels` block above.

---

## 4. Tab 2 (Socratic Wizard & Lock Gate)

**Purpose:** convert `Stage1Payload` into a locked, hashed analysis plan. Novice and Expert paths converge here — same validation, same EPV check, same hash generation, just different entry UI (guided wizard vs. direct JSON/form upload).

**Steps (novice guided wizard):**
- **Step A — Exposure:** select primary exposure/treatment from unvaulted covariates.
- **Step B — Outcome confirmation:** display the Stage-1-declared outcome back to the user (read-only re-display, not re-selection) + assign time-to-event column if survival design.
- **Step C — Confounders:** multi-select covariates. Redundancy warnings surfaced here from Stage 1's advisory cache. Interaction terms permitted here only, gated by a required clinical rationale (see §5).
- **Step D — Rigor check:** missing-data strategy declared per covariate with missingness (complete-case / imputation / flag-as-sensitivity-category). **Live EPV gauge** (`E_effective / k`) recalculates in real time as variables, interactions, and missing-data strategy change — never a static/cached number.

**Sample-size transparency decision:** showing `N_effective` / `E_effective` **before lock**, as missing-data strategy is chosen, is safe and necessary — it's a feasibility fact (how well a variable was collected), not an outcome association. Hiding it just delays a catastrophic Stage 3 convergence failure (e.g., complete-case N=12) to a worse moment. Locking blind to this number turns protocol locking into trial-and-error instead of an intentional design choice.

**What must stay locked at Stage 2 (not deferred to execution):** missing-data handling *method* (complete-case vs. impute vs. flag) must be declared and hashed into the plan before Stage 3 executes. Never applied silently in Stage 1, never chosen post-hoc after seeing results.

### `Stage2Payload` contract (H1)

```json
{
  "provenance": {
    "payload_fingerprint_h0": "sha256_...",
    "plan_fingerprint_h1": "sha256_of_this_object_excluding_this_field",
    "lock_timestamp_utc": "2026-07-26T20:15:00Z",
    "entry_mode": "socratic_wizard"
  },
  "protocol": {
    "study_design": "retrospective_cohort",
    "exposure": { "column_name": "treatment_group", "reference_level": "Arm A" },
    "outcome_confirmation": { "column_name": "os_event", "event_value": 1, "time_column": "os_months" },
    "confounders": ["age", "stage", "baseline_ecog"],
    "interactions": [
      {
        "term": "age:treatment_group",
        "rationale": "Pre-clinical models suggest treatment efficacy declines in older cohorts."
      }
    ],
    "missing_data_strategy": {
      "global_default": "complete_case",
      "column_overrides": { "baseline_ecog": "flag_as_missing_category" }
    },
    "pre_specified_transforms": {
      "age": "linear"
    }
  },
  "locked_feasibility_metrics": {
    "n_effective": 312,
    "e_effective": 44,
    "parameters_k": 5,
    "epv": 8.8
  }
}
```

`plan_fingerprint_h1` chains off `payload_fingerprint_h0` as parent, forming the provenance tree: `H0 → H1(plan_v1) → H2(plan_v2 amendment)...`

**Note:** `protocol.pre_specified_transforms` records, per continuous covariate, whether a linear term or a specific transform (e.g., restricted cubic spline) was declared *before* execution. This field is read by Tab 3's Gate 4 (linearity check) — see §6.

---

## 5. Interaction Term Rationale — "Manuscript Mirror" (Good-Faith, Not Gated)

- No hard word-count or keyword validation on the interaction rationale field. Strict gating produces obstacle theater (users type fluff to pass a word-count, not genuine justification).
- Minimal sanity floor only (~≥15 characters, to block literal "yes"/"none").
- **Manuscript Mirror UI**: live preview showing exactly how the rationale text will be permanently embedded in the exported Methods section / STROBE supplement / audit log. Leverages professional pride and peer-review awareness as the real quality incentive, not a character-count gate.
- The rationale text is part of the pre-registered protocol and is intentionally stored *inside* the hashed `protocol` object (not in an advisory/non-load-bearing cache) — it was a real decision made before unblinding, not UI decoration.

---

## 6. Tab 3 (Execution Engine, Diagnostics & Autopsy Canvas)

**Purpose:** unattended, deterministic execution engine. Takes vaulted raw data (H0) + locked protocol (H1 or latest amendment) as input, executes the model with zero interactive manual tuning, evaluates a fixed diagnostic battery, and routes to either a publication package or a formal Autopsy Canvas.

### 6.1 Diagnostic Gates (four checks, fixed thresholds)

| Gate | Check | Fail condition |
|---|---|---|
| 1. Complete Separation & Convergence | Detects infinite MLE estimates from zero-cell categories or perfect predictors | Non-convergence, or max SE > 100 |
| 2. Multicollinearity (VIF) | VIF per covariate | Warning: VIF 5.0–10.0. Fail: VIF > 10.0 |
| 3. Proportional Hazards | Schoenfeld residuals (survival models only) | Warning: global or per-covariate p ∈ [0.01, 0.05). Fail: p < 0.01 |
| 4. Linearity of continuous terms | Box-Tidwell / CCPR residuals, evaluated **against `Stage2Payload.protocol.pre_specified_transforms`** | Fail only if non-linearity is significant *and* a linear term (not a pre-specified spline/transform) was locked — prevents post-hoc curve-fitting without an amendment |

**Gate 4 is structurally different from Gates 1–3:** it is not a bare statistical test. Its pass/fail depends on what was pre-registered in the locked plan. Implementers must not build Gate 4 as a standalone test that ignores `pre_specified_transforms` — doing so would silently reopen a post-hoc-curve-fitting loophole the rest of the system is designed to close.

**Threshold governance:** all four gates' numeric thresholds are fixed per a versioned `diagnostic_ruleset_version` (e.g., `"v1.2.0-strobe-default"`), recorded in every execution payload. Thresholds are never tunable per-run — that would be an undocumented p-hacking lever (e.g., rerunning with VIF threshold 10 instead of 5 until a model passes). **Open follow-up:** the ruleset version needs its own external changelog/registry documenting what changed between versions (e.g., `v1.2.0` → `v1.3.0`), separate from the per-study provenance hashes — not yet built, flagged for later.

### 6.2 Three-way status router

```
UNVAULT & EXECUTE → RUN DIAGNOSTIC RULESET →
   PASS      → Publication Package (standard Tables 1–2, Forest Plot, Methods text)
   WARNING   → Publication Package + auto-injected "Sensitivity & Limitations" section
   FAIL      → Autopsy Canvas (execution halts, no publication package generated)
```

WARNING is a real, distinct outcome — not merged into PASS or FAIL. Borderline cases (e.g., VIF = 6.2) publish with a mandatory, auto-generated caveat rather than being silently accepted or needlessly blocked.

### 6.3 Autopsy Canvas — no silent fallback, ever

- **No automated fix-and-continue.** No auto-retry, no auto-dropping a covariate, no auto-switching optimizer/penalty. Any automated correction is itself a hidden analytic decision that would break the meaning of "locked plan."
- On failure, the engine halts and produces a diagnostic-specific payload: which gate failed, which variable(s) are implicated, supporting evidence (VIF value, Schoenfeld p, cell counts), and **pre-formatted remediation options specific to that failure type** (e.g., for a PH violation: "Option A: stratify by X" / "Option B: model X with a time-varying interaction X × log(t)"). The user selects or edits a standard methodological solution — they are never handed a blank rationale box and asked to invent a fix from scratch.
- Clicking "Prepare Protocol Amendment" routes back to Tab 2 in **Amendment Mode**: all previously validated fields (Step A/B — exposure, outcome, study design) become read-only; only Step C/D (covariates, interactions, missing-data strategy) are editable, pre-populated from the failed plan with the offending variable flagged and the remediation option pre-loaded into the rationale field for the user to confirm or edit.
- **Live gauges stay active during amendment** — EPV, N_effective, E_effective recalculate globally as variables are toggled, since dropping one variable changes missingness-driven N for the whole model (cascade effect), not just the flagged term.
- A rationale field for the amendment itself is required and gets auto-appended to the final Methods/provenance log.
- New plan gets its own hash, parent-referenced to the failed plan's hash. Immutable chain, never overwritten.
- **Outcome stays masked throughout the amendment process** — user sees diagnostic stats (separation source, VIF, Schoenfeld p) but never sees outcome direction/significance, so revising the plan doesn't reopen a p-hacking window.

### 6.4 Mandatory sensitivity/robustness computation

E-values (minimum unmeasured-confounder strength needed to explain away the effect) and tipping-point analyses (for missing-data assumptions under MNAR) are **always computed and always reported** for every run, regardless of study-specific pre-specification. Rationale: unlike a covariate or interaction choice, a sensitivity analysis is a fixed-methodology robustness check *on* the already-locked primary result — it doesn't alter primary coefficients or introduce a p-hacking lever, so making it non-optional strengthens default STROBE/TRIPOD compliance without adding a decision point a novice could get wrong or skip.

### 6.5 `Stage3Payload` contract (Hexec)

```json
{
  "provenance": {
    "payload_fingerprint_h0": "sha256_7a1b9c3f...",
    "parent_plan_hash": "sha256_9d8e7f6a...",
    "execution_fingerprint": "sha256_e4f5a6b7... (hash of this execution payload, computed excluding this field)",
    "execution_timestamp_utc": "2026-07-26T20:25:00Z",
    "was_amended": false
  },
  "diagnostic_config": {
    "ruleset_version": "v1.2.0-strobe-default",
    "thresholds_locked": {
      "vif_warning": 5.0,
      "vif_fail": 10.0,
      "ph_p_warning": 0.05,
      "ph_p_fail": 0.01,
      "separation_max_se": 100.0
    }
  },
  "diagnostics_summary": {
    "overall_status": "WARNING",
    "tests": [
      { "test_name": "complete_separation", "status": "PASS", "metric_value": 0.42 },
      { "test_name": "multicollinearity_vif", "status": "WARNING", "metric_value": 6.2, "affected_variables": ["sbp", "map_bp"] },
      { "test_name": "proportional_hazards", "status": "PASS", "metric_value": 0.24 },
      { "test_name": "linearity_continuous_terms", "status": "PASS", "details": "Age non-linearity p = 0.08, evaluated against H1 linear pre-specification." }
    ]
  },
  "model_results": {
    "model_type": "cox_ph",
    "sample_sizes": { "n_total": 340, "n_effective": 312, "e_effective": 44 },
    "coefficients": [
      {
        "variable": "treatment_group_ArmB",
        "label": "Treatment Arm B vs Arm A",
        "unadjusted_hr": 0.62, "unadjusted_ci_95": [0.41, 0.94], "unadjusted_p": 0.024,
        "adjusted_hr": 0.58, "adjusted_ci_95": [0.37, 0.91], "adjusted_p": 0.018
      }
    ]
  },
  "manuscript_artifacts": {
    "table_1_asset_url": "/assets/exports/table_1_h1.html",
    "table_2_asset_url": "/assets/exports/table_2_h1.html",
    "forest_plot_svg_url": "/assets/exports/forest_plot_h1.svg",
    "methods_paragraph_url": "/assets/exports/methods_text_h1.txt",
    "strobe_checklist_pdf_url": "/assets/exports/strobe_checklist_h1.pdf"
  }
}
```

**Hardening notes:**
- `execution_fingerprint` does not overload the "H2" label used by plan amendments. `parent_plan_hash` dynamically points to whichever plan (H1 or latest amendment Hn) was actually executed.
- All manuscript artifacts are uniform asset/file URL references, not inline HTML/text blobs — lean payload, scalable storage.

---

## 7. Tab 4 (Publication Assets, Interactive Visualizations & Cryptographic Audit Binder)

**Purpose:** turns validated execution results (Hexec) into a complete, publication-ready submission bundle: interactive journal-style figures, STROBE/TRIPOD-compliant tables, manuscript draft text, and an immutable, independently-verifiable audit package.

### 7.1 Modules

- **Module 1 — Interactive Figure Canvas:** vector-native (SVG/D3) forest plots, KM/cumulative-incidence curves with number-at-risk tables, and sensitivity plots (E-value curve, tipping-point heatmap). Journal-style toggles (log/linear scale, serif/sans-serif, CI bands vs. error bars) without re-running statistical code. Exports: SVG, PDF (CMYK), 300+ DPI PNG/TIFF.
- **Module 2 — STROBE/TRIPOD Tables:** Table 1 (baseline characteristics, stratified by exposure, with SMD and missing-data counts), Table 2 (unadjusted + adjusted effect estimates, with `k`, `N_effective`, `E_effective`, and E-value in the footer), Supplementary Table S1 (full diagnostic + sensitivity matrix). Export formats: HTML, LaTeX, DOCX, CSV.
- **Module 3 — Manuscript Draft & Checklist Generator:** Methods/Results section text export citing the protocol hash directly (e.g., "Analysis was performed in accordance with a pre-specified protocol locked prior to unblinding (Protocol Hash: `sha256_...`)"), plus a fully filled STROBE/TRIPOD checklist PDF with each item linked to the exact section/table satisfying it.
- **Module 4 — Cryptographic Audit Binder (.zip):** self-contained archive for independent verification by an editor, auditor, or peer reviewer. Structure:

```
study_audit_binder_Hexec.zip
├── manifest.json                        (full cryptographic DAG + metadata, with a note
│                                          explaining the two-track numbering per §2)
├── verification_script.py               (standalone, pinned-dependency-version script)
├── 01_raw_vaulted_data/
│   ├── dataset_fingerprint.sha256
│   └── schema_mapping.json
├── 02_pre_registered_protocols/
│   ├── protocol_h1_locked.json
│   └── amendment_chain.json             (full per-version diagnostics for every
│                                          amendment, not rationale text alone —
│                                          a reviewer must be able to independently
│                                          check *why* each amendment was made, not
│                                          just read the stated reason)
├── 03_execution_and_diagnostics/
│   ├── execution_results_hexec.json
│   └── diagnostic_suite_results.json
└── 04_manuscript_assets/
    ├── table_1_baseline.html
    ├── table_2_primary_model.html
    ├── figure_1_forest_plot.svg
    ├── figure_2_survival_curve.svg
    └── strobe_checklist_completed.pdf
```

### 7.2 Verification script — tolerance and versioning fix

**Problem identified:** an initial design claimed bit-exact reproduction ("match to 10⁻⁸ precision"). This is not realistic for iterative MLE fits (Cox PH, etc.) — different machines, BLAS backends, or minor library version differences can produce legitimate numerical differences well above 1e-8 with identical data and code. A strict bit-exact check would produce **false-positive integrity failures** on a legitimate, unmodified study, wrongly implying fraud where none occurred.

**Fix:**
- Verification tolerance is relaxed to a documented, reasonable band (e.g., relative tolerance ~1e-4) rather than an absolute 1e-8 claim.
- The manifest pins exact library/solver versions used for the original execution (e.g., `lifelines==X.Y.Z`, BLAS backend) so re-verification instructions explicitly say "reproduce using these pinned versions," rather than leaving version drift as an unstated variable.
- The script's three checks remain: (1) hash-chain integrity (H0 → H1[→Hn] → Hexec unbroken), (2) protocol audit (no post-hoc parameter changes between lock and execution), (3) deterministic re-fit within the documented tolerance band.

### 7.3 `Stage4Payload` contract (Hbundle)

```json
{
  "provenance": {
    "payload_fingerprint_h0": "sha256_7a1b9c3f...",
    "parent_plan_hash": "sha256_9d8e7f6a...",
    "execution_fingerprint": "sha256_e4f5a6b7...",
    "bundle_fingerprint_hbundle": "sha256_b1c2d3e4... (renamed from 'h4' to avoid implying it is part of the plan-amendment numbering track — see §2)",
    "generated_timestamp_utc": "2026-07-26T20:30:00Z"
  },
  "sensitivity_metrics": {
    "e_value_point_estimate": 2.21,
    "e_value_confidence_bound": 1.45,
    "e_value_interpretation": "An unmeasured confounder would need to be associated with both exposure and outcome by a Hazard Ratio of at least 2.21 to fully explain away the observed effect.",
    "tipping_point_grid_summary": {
      "mnar_shift_threshold": "-15% outcome rate shift required in missing arm to tip p >= 0.05",
      "robustness_rating": "HIGH"
    }
  },
  "export_manifest": {
    "tables": [
      { "table_id": "table_1", "title": "Baseline Cohort Characteristics",
        "formats": { "html_url": "/exports/table_1.html", "latex_url": "/exports/table_1.tex", "csv_url": "/exports/table_1.csv", "docx_url": "/exports/table_1.docx" } },
      { "table_id": "table_2", "title": "Multivariable Regression & Sensitivity Model",
        "formats": { "html_url": "/exports/table_2.html", "latex_url": "/exports/table_2.tex", "csv_url": "/exports/table_2.csv", "docx_url": "/exports/table_2.docx" } }
    ],
    "figures": [
      { "figure_id": "fig_1_forest_plot", "title": "Adjusted Hazard Ratios",
        "formats": { "svg_url": "/exports/fig_1.svg", "pdf_url": "/exports/fig_1.pdf", "png_300dpi_url": "/exports/fig_1.png" } }
    ],
    "documents": {
      "manuscript_draft_text_url": "/exports/manuscript_draft.txt",
      "strobe_checklist_pdf_url": "/exports/strobe_checklist.pdf",
      "audit_binder_zip_url": "/exports/study_audit_binder_Hexec.zip"
    }
  }
}
```

---

## 8. Study Design Generalization (Deferred, Schema Reserved)

- The Sandbox-to-Vault architecture requires **zero changes** to Tab 1 or Tab 3 to generalize beyond retrospective cohorts — only Tab 2 gets a new selector step.
- Phase 1 ships **retrospective-only**. Other designs are stubbed in the schema (`protocol.study_design` field exists from day one, even with only one legal value) so the UI shape doesn't need retrofitting later.
- Deferred designs and their expected impact on the Wizard:
  - **Prospective Cohort:** adds follow-up time window definition, loss-to-follow-up tracking.
  - **Case-Control:** adjusts Wizard warnings (odds ratios vs. risk ratios).
  - **Cross-Sectional:** larger structural fork — disables Cox PH, defaults to logistic/linear regression, replaces EPV with a different sample-size check. This fork's impact on the Stage 3 Engine IPC interface is worth scoping (even briefly) before Phase 1 code starts, so the Engine contract isn't accidentally Cox-PH-only in a way that requires surgery later. **Not yet scoped — flagged for follow-up.**

---

## 9. Phase 1 Minimal Slice (Scope Lock)

Retrospective-only MVP, four tabs:

1. **Tab 1 (Data):** Upload CSV/Excel → schema mapping → outcome declaration + vaulting → sentinel declaration → missingness display → redundancy/sparse cross-tab checks → fixed cohort banner. Emits `Stage1Payload` (H0).
2. **Tab 2 (Wizard):** Socratic 4-step form (Steps A–D) → live EPV gauge → Manuscript Mirror for interactions → Lock button. Emits `Stage2Payload` (H1). Expert path: same lock code path via direct JSON/form import, skipping guided UI.
3. **Tab 3 (Engine):** IPC call to `v2.7.0-core` executable → 4-gate diagnostics → 3-way status router → pass outputs, or Autopsy Report + Amendment Canvas on failure. Emits `Stage3Payload` (Hexec).
4. **Tab 4 (Assets):** Interactive figures, STROBE/TRIPOD tables and checklist, manuscript draft text, cryptographic audit binder. Emits `Stage4Payload` (Hbundle).

**Explicitly not in Phase 1:** biochemistry/teaching curriculum (separate product), non-retrospective study designs (schema-stubbed only), OCR/photo ingestion (mandatory human-in-the-loop sign-off required before it ever touches the rigor engine — deferred to a later ingestion phase after structured-file ingestion is solid).

**Explicitly not yet designed, flagged for follow-up:**
- Cross-sectional model interface fork's effect on Tab 3's Engine IPC contract.
- External changelog/registry for `diagnostic_ruleset_version` changes over time.
- Full end-to-end user-journey walkthrough (raw CSV → published bundle) as a single narrative pass.
- Technical stack and API endpoint design — deliberately deferred as a separate, focused decision (local-first desktop vs. web app has real tradeoffs given sensitive patient-data handling, especially for future OCR ingestion).

**Visual polish backlog (non-urgent, no schedule):**
- Forest plot: header/column-header text overlap; whisker-to-label collision on long covariate rows; missing space between CI bracket and p-value; x-axis tick marks stopping at 1.0 despite CIs extending past it; footer warning text clipped at canvas bottom.
- Table 1 (excel export): blank unlabeled trailing column.
- KM plot: number-at-risk table misaligned/cramped relative to x-axis ticks; risk table column alignment drifts from x-axis ticks progressively (risk table container width doesn't match the main plot's coordinate space); CI bands visually overwhelming for small-N studies, obscuring the step function curves underneath.
- Manuscript draft: raw DB integer IDs shown instead of column names ("Primary outcome variable IDs: 5", "Covariates: 3, 4"); unhydrated template placeholders (Conclusions, Background, Interpretation, Generalisability); truncated "kaplan_meier_logrank:" test description.
- Rounding precision: age CI shown as [1.00, 1.01] hiding that the true lower bound (0.9977) sits below 1.0, could visually mislead about null inclusion.

---

## 10. Working Discipline (carried over, still applies)

Never trust a summary claim. Verify with real terminal output and independent manual cross-checks. This caught real bugs repeatedly in prior sessions, including cases where passing tests masked genuine defects — the same discipline applies to this app's architecture claims once code exists: test the gates, don't assume "looks locked" means "is locked." The verification-script tolerance fix in §7.2 is a direct application of this discipline to the architecture itself, not just to code review.

---

## 11. Bug Audit Backlog (MEDIUM severity — deferred)

Captured from full-repo audit on 2026-07-28. HIGH/CRITICAL items already fixed in this session. These are MEDIUM severity — logged for later, not scheduled yet.

### M1: Force-reingest FK violation
**File:** `core/ingestion/csv_loader.py`, `_cascade_clear()` (lines 73-88)
**Issue:** Deletes from `analysis_results` and `variables` but not `analysis_covariate_results`. With `PRAGMA foreign_keys=ON`, DELETE from `analysis_results` raises `sqlite3.IntegrityError` when covariate results exist.
**Fix:** Add `analysis_covariate_results` to the cascade clear.

### M2: Data hash inconsistency
**File:** `core/cli/main.py`, `cmd_export` (lines 1284-1295)
**Issue:** Data hash computed two different ways (JSON-lines vs columnar format). Different serialization = different hashes for same data, undermining integrity verification.
**Fix:** Standardize on one format for hash computation.

### M3: Multiple-testing correction timing
**File:** `core/cli/main.py`, `cmd_analyze` (lines 878-886 vs 930-941)
**Issue:** Correction filters on `status == "completed"`, but later some results get promoted to `"assumption_violation"`. These promoted results have `adjusted_p_value` set while skipped assumption violations have `None`.
**Fix:** Apply correction after status promotion, or exclude promoted results from correction.

### M4: Dedup check uses wrong field
**File:** `core/cli/main.py`, `cmd_analyze` (lines 677-685, 825-834)
**Issue:** Dedup queries `WHERE variable_ids_used=?` with `json.dumps([])`. But `variable_ids_used` is always `[]` — the actual variable is in `variable_name`. Two tests of same type on different variables collide.
**Fix:** Include `variable_name` in dedup key.

### M5: Excel audit hash omits ph_diagnostics_json
**File:** `core/reporting/excel_export.py`, `_build_tab3_analyses` (line 580)
**Issue:** Audit hash decodes most JSON fields but omits `ph_diagnostics_json`. This differs from bundle hash which includes it. Excel audit hash won't match bundle hash for Cox PH studies.
**Fix:** Include `ph_diagnostics_json` in hash computation.

### M6: Test name mismatch
**Files:** `core/stats/inferential.py` (line 84), `core/cli/main.py` (line 722), `core/planning/test_selector.py` (lines 221-226)
**Issue:** Stats engine uses `fishers_exact` but CLI suggestions say `fisher_exact`. Users who follow suggestions get `Unknown test` error.
**Fix:** Normalize to one name, or add alias.

### M7: Duplicate `StudyPlan` dataclass
**Files:** `core/models.py` (lines 126-148) vs `core/planning/study_plan.py` (lines 33-71)
**Issue:** Two separate `StudyPlan` dataclasses. `core/models.py` version missing 6 fields (`study_type`, `matching_criteria`, `post_hoc_tests`, `amendment_reason`, `cox_ph_models`, `diagnostic_results`). Importing from wrong one = silent data corruption.
**Fix:** Remove stale version from `core/models.py`, ensure single source of truth.

### M8: Duplicate `_filter_superseded()`
**Files:** `core/reporting/strobe_checklist.py` (line 24), `core/reporting/manuscript_draft.py` (line 30)
**Issue:** Identical function in two places. Divergence risk.
**Fix:** Extract to shared utility module.

### M9: `cmd_table1` hardcodes `"treatment_arm"`
**File:** `core/cli/main.py`, line 619
**Issue:** Default groupby hardcoded to `"treatment_arm"`. Studies using different column names get unstratified Table 1 with no warning.
**Fix:** Read groupby from locked plan's `primary_comparison` or `primary_treatment_col`.

### D1: `_latest_locked_plan()` duplicated
**Files:** `core/reporting/flowchart/flowchart.py` (line 54), `core/reporting/forest_plot.py` (line 95)
**Issue:** Identical function in two places. Both glob for locked plan files and return the latest as a dict.
**Status:** Fixed — extracted to `core/reporting/__init__.py`.

### D2: `_svg_escape()` triplicated
**Files:** `core/reporting/flowchart/flowchart.py` (line 243), `core/reporting/forest_plot.py` (line 218), `core/reporting/lineage.py` (line 555)
**Issue:** Two identical copies (flowchart, forest_plot) escape `&<>`. Third copy (lineage) additionally escapes `"'` — subtle divergence already happened.
**Status:** Fixed — extracted to `core/reporting/__init__.py` with the superset (5-char) version.

### D3: `_format_label()` duplicated
**Files:** `core/reporting/flowchart/flowchart.py` (line 41), `core/reporting/excel_export.py` (line 26)
**Issue:** Identical acronym-aware label formatter in two places.
**Status:** Fixed — extracted to `core/reporting/__init__.py`.

### D4: `unmask_study()` — intentional facade (not a bug)
**Files:** `core/planning/lock.py` (line 117), `core/masking/gate.py` (line 83)
**Issue:** lock.py delegates to gate.py. This is an intentional wrapper, not a true duplicate.
**Status:** No action needed.

---

## 12. Robustness Audit (BATCH-FAILURE severity — fixed)

Captured from manual full-pipeline run on 2026-07-29. Two bugs in `cmd_analyze` that caused silent batch abortion.

### B1: Variable ID not resolved to column name
**File:** `core/cli/main.py`, `cmd_analyze` (lines 749-752)
**Issue:** `--test "8:kaplan_meier_logrank:..."` stores "8" as `variable_name`. Event-col derivation expects column name "pfs_days" → produces "8_event" → ValueError → abort.
**Root cause:** `cmd_plan` stores first token of `--test` spec as `variable_name` without resolving ID to column name.
**Fix:** In `cmd_analyze`, build `var_id_to_name` lookup from variables table, resolve before event-col derivation.

### B2: One test failure aborts entire batch
**File:** `core/cli/main.py`, `cmd_analyze` (lines 846-873)
**Issue:** No try/except around `run_test()`. Any exception (e.g., ValueError from B1) crashes entire analysis, preventing Cox PH and other independent tests from running.
**Root cause:** Missing error isolation per test.
**Fix:** Wrap each test execution in try/except, record error result, continue batch. Same for Cox PH models.

---

## 13. Visualization & Export Audit (E2E severity — fixed)

Captured from manual full-pipeline run on synthetic_21_v2.csv (N=21) on 2026-07-29. Five bugs in plotting and Excel export.

### B3: KM risk-table axis drift
**File:** `core/reporting/plots.py`, `generate_km_plot` (lines 397-563)
**Issue:** Risk-table columns drift left relative to x-axis ticks. Root cause: `tight_layout()` + `bbox_inches="tight"` shifts axes AFTER text annotations are placed in data coordinates.
**Fix:** Use `constrained_layout=True` in `plt.subplots()` instead of post-hoc `tight_layout()`. Remove manual position sync.

### B4: KM curve/canvas right-margin padding
**File:** `core/reporting/plots.py`, line 528
**Issue:** Group B curve truncates at canvas edge with only 5% padding — visually cramped on small N datasets.
**Fix:** Increase right padding from 5% to 10% of max observed time.

### B5: Excel embedded KM aspect-ratio distortion
**File:** `core/reporting/excel_export.py`, line 274
**Issue:** `img.width, img.height = 600, 380` — hardcoded dimensions don't preserve source image aspect ratio. KM plot appears squished in Excel.
**Fix:** Read actual PNG dimensions with PIL, scale proportionally to fit 600px width.

### B6: Excel cox_ph_model CI wrong
**File:** `core/reporting/excel_export.py`, lines 477-484
**Issue:** Overall model summary row populates 95% CI (Lower/Upper) from first covariate (treatment_arm). Omnibus LR test has no single CI — it's a chi-square test.
**Fix:** For `cox_ph_model`, show "N/A" in CI columns for main row. CI appears only in per-covariate sub-rows.

### B7: Audit hash mismatch (Excel vs bundle)
**File:** `core/reporting/excel_export.py`, lines 581-588 vs `core/reporting/bundle.py`, lines 90-95
**Issue:** Bundle includes `covariate_results` in results hash; Excel export doesn't. Different data = different composite hash. Excel displayed a "Composite Bundle Hash" that would never match the actual bundle.
**Root cause:** Excel tried to predict a composite hash before bundle existed, using different serialization.
**Fix:** Excel shows "Run 'bundle' command to generate" if bundle doesn't exist, or reads actual hash from bundle's manifest.json if it does. Component hashes (raw_data, plan, results) still displayed.

### B8: Excel "Comparison" column shows raw variable ID
**File:** `core/reporting/excel_export.py`, lines 417-439
**Issue:** `var_map` built from locked plan stores raw `variable_name` (e.g., "8") instead of column name ("pfs_days"). Row shows "8 by Treatment Arm" instead of "Pfs Days by Treatment Arm".
**Fix:** Resolve variable ID to column name via variables table lookup when building `var_map`.

### B9: Table 1 orphan border column E
**File:** `core/reporting/excel_export.py`, `_autofit_columns` (lines 64-83)
**Issue:** `_autofit_columns` iterated `ws.columns` (ALL columns including empty), creating default `Border()` objects on cells beyond the data range. Column E+ appeared with borders but no data.
**Fix:** Find max column with actual data first, only iterate up to that column.

### B10: Flowchart mislabeled "CONSORT" for cohort study
**File:** `core/reporting/flowchart/flowchart.py`, line 342
**Issue:** Title hardcoded as "CONSORT Participant Flow Diagram". CONSORT is RCT-specific; STROBE governs observational/cohort designs.
**Fix:** Added `study_type` to `FlowchartData`. Title now uses "STROBE Participant Flow Diagram" for non-RCT studies, "CONSORT" only when `study_type == "rct"`.

### Investigation: KM x-axis unit (days vs months)
**File:** `core/reporting/plots.py`, lines 381-382, 418
**Finding:** Conversion IS correct. `DAYS_PER_MONTH = 30.44`. Raw `pfs_days` values divided by 30.44 before plotting. Label "Time (months)" correctly reflects the plotted unit. No fix needed.

---

## 14. Third-Pass Audit (E2E severity — fixed)

Captured from manual full-pipeline run on 2026-07-29.

### B9 (3rd attempt): Table 1 orphan border column E
**File:** `core/reporting/excel_export.py`, `_build_tab2_table1` (line 310)
**Root cause:** Prior fixes touched `_autofit_columns` (irrelevant) and border loop (correct). The ACTUAL visible artifact was Excel gridlines, not cell borders. Column E had `Border()` with `style=None` (no border), but worksheet gridlines extended across all columns.
**Fix:** `ws.sheet_view.showGridLines = False` on Table 1 sheet. Only actual cell borders are now visible.

### B11: Flowchart phase labels use RCT terminology for non-RCT
**File:** `core/reporting/flowchart/flowchart.py`, lines 357-365
**Issue:** Phase pills hardcoded as ENROLLMENT/ALLOCATION/ANALYSIS. "ALLOCATION" implies randomized allocation, inappropriate for cohort/case-control studies.
**Fix:** Use STROBE-appropriate labels for non-RCT: STUDY POPULATION / FOLLOW-UP / ANALYSIS. CONSORT labels only when `study_type == "rct"`.

### B12: EPV warning vague in manuscript Limitations
**File:** `core/reporting/manuscript_draft.py`, lines 58-267
**Issue:** Existing EPV check said "may be inadequate for reliable inference" — vague, doesn't mention overfitting or unstable estimates.
**Fix:** Now explicitly states "severe overfitting produces unstable coefficient estimates and unreliable confidence intervals" with the EPV value and recommended threshold.

### B5 re-verification: KM image bottom padding
**Finding:** Fresh render confirms risk-table row B fully visible with margin below. No truncation. Prior session's XML analysis was correct — image displays at 600x450 (4:3 aspect ratio preserved).

---

## 15. Fourth-Pass Audit & Refinements (B13 & B14)

Captured from verification pass and task prompt on 2026-07-29.

### B13: Excel nested Cox PH covariate rows misaligned
**File:** `core/reporting/excel_export.py`, lines 552 & 559
**Issue:** Covariate header ("Covariate") and covariate names were written to Column 2 (Test Name), causing sub-table headers and values to be shifted left relative to parent table columns (HR under Comparison, etc.).
**Fix:** Changed `column=2` to `column=4` (Variable). Nested sub-rows now align under:
  - Cell D (Col 4): Variable / Covariate name (`treatment_arm`, `age`, `iss_stage`, `high_risk_fish`)
  - Cell F (Col 6): Statistic / HR
  - Cell G (Col 7): P-Value / Wald P-Value
  - Cell I (Col 9): 95% CI (Lower) / CI Lower
  - Cell J (Col 10): 95% CI (Upper) / CI Upper
**Verification:** Added regression test `test_excel_cox_ph_covariate_alignment` in `tests/test_excel_export.py`. Visually inspected output `study_report.xlsx` sheet `Statistical Analyses` coordinates D5:J9.

### B14: Flowchart stage labels for cohort studies
**File:** `core/reporting/flowchart/flowchart.py`, lines 282 & 363
**Issue:** For non-RCT/cohort studies, the middle stage label pill at row y3 (group/treatment arm split) was labeled "FOLLOW-UP", which misidentified exposure/cohort categorization as follow-up.
**Decision & Wording:** Refined middle pill label to `COHORT ASSIGNMENT` (and increased `pill_w` from 95 to 125 to fit cleanly). Flowchart pills for cohort studies are now `ELIGIBILITY / COHORT ASSIGNMENT / ANALYSIS`.
**Source Verification Status:** Judgement-based refinement. A web search of STROBE guidelines (PLoS Med 2007; BMJ 2007; Equator Network) confirmed STROBE Item 13 requires reporting numbers at each stage (potentially eligible, examined, included, completing follow-up, analyzed), but STROBE explicitly does *not* mandate a single rigid visual box/pill labeling template (unlike CONSORT 2010 for RCTs). "COHORT ASSIGNMENT" reflects best clinical judgement for the group-split row in observational studies.

### TestAgainstRealStudy Orphaned Study ID Fix
**File:** `tests/test_forest_plot.py`, lines 113-178
**Root Cause:** `TestAgainstRealStudy` hardcoded a loose study UUID (`a0368cf06b4444ca8a4118704b1edd2f`) generated during initial local development. Because `data/` is gitignored, the local directory was never committed, making the test suite depend on uncommitted local disk state. Git log confirmed the ID was never in git history. The test expected a 4-covariate study matching the synthetic myeloma cohort (21 patients, EPV=2.5, all 4 CIs crossing 1).
**Fix:** Added `setup_method` and `teardown_method` to `TestAgainstRealStudy` that constructs a clean isolated test study via `_make_study` containing the exact 4-covariate myeloma cohort profile (`treatment_arm`, `age`, `high_risk_fish`, `prior_lines`, EPV=2.5 warning) and tears it down after tests run. All 6 tests now pass cleanly in any clean environment.

### Forest Plot SVG Header & Layout Geometry Refinement
**File:** `core/reporting/forest_plot.py`, lines 227-348
**Issue 1 (Header Alignment):** `"aHR [95% CI]"` and `"p-value"` headers were both positioned at `x = plot_l + plot_w + 10` with `y = header_y` and `y = header_y + 14`, stacking them vertically on top of each other instead of side-by-side.
**Issue 2 (Text Collision):** `margin_l` was 180 and `plot_l` was `margin_l + 20` (200), leaving only 20px of label allocation before the plot canvas. Long covariate labels like `high_risk_fish (per 1-unit increase)` (~250px wide) extended into `x = 180..430`, striking through the plot graphics (`x = 225..440`).
**Fix:**
  - **Header & Column Alignment**: Positioned `"aHR [95% CI]"` at `x = txt_x` (740) and `"p-value"` at `x = txt_x + p_col_offset` (885) on the same line (`y = header_y`), matching the exact column X coordinates of data rows.
  - **Layout Geometry & Width Expansion**: Adjusted `margin_l = 20`, allocated `label_w = 230` (`plot_l = 270`), expanded plot width `plot_w = 450` (up from 400), placing data labels in `x = 20..250` cleanly separated from plot graphics (`x = 270..720`).

---

## 16. Methodological & Policy Decisions (Flagged for User Decision, Not Implemented)

### P1: Multivariable Cox Model EPV & Covariate Restriction / Penalized Regression
- **Flagged Issue:** Low EPV ratio (EPV = 2.5) in small cohorts (N=21, 10 events, 4 predictors).
- **Status:** Flagged for user policy decision — NOT auto-implemented.
- **Tradeoff:** Restricting multivariable predictor counts or forcing Firth's penalized Cox regression alters statistical methodology and investigator protocol intent. The engine reports transparent EPV warnings in manuscript text, forest plot, and Excel audit logs, preserving investigator intent while flagging statistical caveats.

### P2: Multiple Testing Adjustment Across Unadjusted & Adjusted Models
- **Flagged Issue:** Bonferroni correction applied across Kaplan-Meier log-rank and multivariable Cox modeling for the primary endpoint.
- **Status:** Flagged as a methodology decision requiring explicit user sign-off — NOT changed this session.
- **Candidate future enhancement:** Expose `multiplicity_correction: none | bonferroni | fdr` as a user-configurable plan option rather than a silent default change.

### D1: KM Plot Confidence Interval Baseline at t=0
- **Finding:** At t=0 (before any events occur), S(0)=1.0 deterministically with zero variance.
- **Status:** Fixed. Clamped `kmf.confidence_interval_.loc[0.0] = 1.0` so shaded band originates as a single point at (0, 1.0) with zero width, opening up at first event t_1.

### D4: Factor Level Encoding for Categorical Predictors (high_risk_fish)
- **Finding:** Checked DB `analysis_covariate_results` for `high_risk_fish`: `reference_level='no'`, `tested_level='yes'`, `coef=-0.9691`, `aHR=0.38`, `p=0.27`.
- **Status:** Verified-no-action. Factor level encoding ('no' as reference, 'yes' as tested) is 100% correct and consistent across the pipeline. Point estimate direction (aHR < 1.0, 95% CI: 0.07-2.14, p=0.27) is small-sample EPV=2.5 noise, not a code defect.

---

## 17. Dead-Code Audit & Cleanup Pass (2026-07-31)

Audit of full codebase identified 5 dead-code items for removal and 2 items confirmed as load-bearing/reserved logic.

### Removals (5 items — verified 285 tests pass after each deletion)
1. **`core/reporting/forest_plot.py:102` (`_get_events_from_sample_counts`)**: Unused helper function in forest plot generation.
2. **`core/reporting/flowchart/flowchart.py:38` (`_find_duplicate_patient_ids`)**: Unused helper function in flowchart module.
3. **`core/reporting/flowchart/flowchart.py:44` (`_get_plan_event_col`)**: Unused helper function in flowchart module.
4. **`core/ingestion/csv_loader.py:17` (`infer_dtype`)**: Unused heuristic function; superseded by `core/ingestion/variable_classifier.py`.
5. **`core/planning/test_selector.py:375` (`CoxPHModelAssumptionCheck`)**: Unused class stub (`applies_to` returned `False`, unreferenced by test runner).

### Confirmed Intentional / Retained (2 items)
1. **`MaskedDataError` (`core/masking/gate.py:25`)**: Reserved domain exception for outcome data access violations before lock.
2. **`unmask_study` (`core/planning/lock.py:117`)**: Intentional facade wrapper over `core.masking.gate.unmask_study`.




