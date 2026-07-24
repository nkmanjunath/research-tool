# Architecture

## Core Design

### Outcome-Masking Gate

The single most important mechanism. It works like this:

1. Researcher uploads raw data. Variables are classified as **baseline** or **outcome**.
2. Outcome columns are **masked** — hidden from all exploration, summary stats, and visualization — until the researcher explicitly locks a study plan.
3. While blinded, the researcher can freely explore baseline covariates, check missingness, generate Table 1, and assess data quality.
4. The researcher must declare the study type, primary comparison, outcome variables, planned statistical tests, and covariates **before** unmasking.
5. Locking writes an immutable, timestamped, versioned JSON snapshot to disk (`study_plan.v{N}.locked.json`).
6. Unmasking reveals outcome data. The stats engine runs only the pre-registered tests by default.
7. Any analysis run after unmasking that was NOT in the locked plan is automatically tagged **EXPLORATORY_POST_HOC** everywhere it appears.

### Enforcement at the data-access layer

Masking is enforced at the `sqlite3.Connection` level via a `MaskedConnection` proxy class. This catches raw SQL queries, ORM access, and any other code path — not just the CLI. Outcome columns are replaced with NULLs in every SELECT result until the study transitions to the unmasked state.

### Provenance

Every computed statistic carries a `ProvenanceEntry` with:
- Function name and parameters
- Source row IDs from the raw data
- Column names used
- Whether the analysis was pre-registered or post-hoc

This lineage is stored as JSON and is queryable: given a statistic name, return the full chain of inputs that produced it.

### Ponytail reductions applied

- `sqlite3` stdlib instead of SQLAlchemy ORM
- `argparse` instead of typer/click
- Inline `make_patients()` helpers over pytest fixture framework
- Runtime-generated study directories under `$PWD/data/studies/<id>/`

### File layout

```
core/
  models.py           — dataclasses + SQL schema
  database.py         — sqlite3 connection + init
  ingestion/          — CSV/Excel load, variable classification
  masking/gate.py     — MaskedConnection (outcome masking)
  planning/           — StudyPlan, test_selector, lock/unmask
  stats/              — descriptive, inferential, multiple comparisons, post_hoc
  provenance/         — ProvenanceTracker
  reporting/          — STROBE checklist, manuscript draft template
  cli/main.py         — argparse entrypoint
tests/
  test_masking_gate.py — adversarial tests
  test_lock_immutability.py
  test_*.py            — per-module unit tests
  test_end_to_end.py   — synthetic myeloma data pipeline
docs/
  architecture.md
  strobe_reference.md
```
