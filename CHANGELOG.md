# Changelog

## v1.0.0-core — 2026-07-24

First verified checkpoint of the research-tool integrity core.  All capabilities
personally verified end-to-end on synthetic clinical data.

### Outcome-Masking Gate

- Physical SQLite storage-layer enforcement, not an application-level proxy.
  Outcome values are moved to a shadow table (`raw_masked_<study_id>`) during
  classification and restored only by the irreversible `unmask_study()` command.
- Verified by querying the database file directly via the `sqlite3` CLI tool
  while the study was in the locked state — outcome columns physically return
  NULL from the raw table regardless of access path.

### Tamper-Evident Locked Plans

- Cryptographically signed plan snapshots (SHA-256 content hash embedded in
  each locked file).  Every load via `load_plan()` verifies the hash.
- Versioned and append-only — old lock files are never deleted or overwritten.
- Verified by hand-editing a locked JSON file and confirming that `analyze`
  refuses to run, raising a `ValueError` with a clear tamper message.

### Deterministic Statistics Engine

- Chi-square, Fisher's exact, t-test, Mann-Whitney U, ANOVA, Kruskal-Wallis,
  Kaplan-Meier log-rank, and Cox proportional hazards — all backed by scipy
  and lifelines.  Table 1 powered by tableone.
- Uniform URO (Unified Result Object) return schema across all tests: includes
  `effect_size` (metric + value), `sample_counts` (total, analyzed, excluded),
  and `status`/`reason` strings.
- Effect sizes computed: Cramér's V (chi-square), Cohen's d (t-test), odds
  ratio (Fisher's exact), hazard ratio (Cox PH).
- Multiple-comparisons correction applied automatically when more than one
  test runs, using Bonferroni, Holm-Bonferroni, or Benjamini-Hochberg as
  appropriate.  Only completed tests receive corrected p-values; skipped or
  errored results retain null.

### Pre-Lock Assumption Checking

- Chi-square expected-cell-count warning computed from independent marginal
  totals only — never a cross-tabulation of outcome × arm — preserving the
  masking gate's guarantee.
- Warnings printed at plan time, stored in the locked plan, and enforced at
  analyze time.  Violating tests are skipped with a full audit trail recorded
  in the database (status, reason, sample counts).  `--force` flag available
  for explicit override.
- Cox proportional-hazards marginal event-rate screen (event count, event
  rate, number of groups) computed entirely from pre-lock data.

### Variable Classification

- Heuristic-based auto-classification from column names.  Count data (e.g.
  `prior_lines`) correctly identified as continuous.  Ordinal stages (e.g.
  `iss_stage`) preserved as categorical.
- `--override` flag on the `plan` command allows correcting a role before
  locking without restarting the study.  Overrides are recorded in the plan
  audit trail and blocked after locking.

### Plan Validation

- `plan` command refuses to run if no variables have been classified, or if
  `--outcome-var-ids` or `--covariates` reference non-existent or wrong-role
  variables.

### Table 1

- Descriptive-only, no baseline p-values (CONSORT-compliant).
- Auto-stratified by `treatment_arm` when a locked plan exists.
- `--overall` flag forces the unstratified single-column view.
- JSON export flattens MultiIndex column names and excludes the grouping
  variable from its own characteristic rows.

### STROBE Compliance

- Checks manuscript draft content for hydrated text, not just template section
  presence.  Sections with only bracketed placeholders report as pending `[ ]`
  rather than satisfied `[✓]`.
- 22-item structured checklist filtered by study design.

### Export

- `research-tool export <study_id>` produces `study_result.v1.json` in
  supplementary mode — no row-level patient data, includes variable catalog,
  locked plan, URO results with effect sizes and sample counts, Table 1
  (stratified when applicable), and a SHA-256 data hash for authenticity.
- `--format appendix` additionally renders a Markdown appendix document with
  Table 1, URO table, plan warnings, and the STROBE checklist.

### Manuscript Draft

- IMRaD-structured markdown output with live Table 1 data and effect sizes.
- Results section displays effect-size metric, CI, and significance labels.

### Verification

- 82 automated tests covering locking, masking, inferential statistics,
  multiple-comparisons correction, variable classification, STROBE checking,
  test-selector logic, assumption checks, role-override audit, Cox screening,
  appendix export, and plan validation.
- `verify.sh` script re-runs the full pipeline end-to-end on synthetic 21-patient
  myeloma-EMD data, pausing for manual adversarial checks (sqlite3 outcome
  null test, tamper detection).
