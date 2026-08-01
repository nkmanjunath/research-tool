# Retrospective Clinical Research Tool

Local-first CLI and Web application that takes raw retrospective clinical study data to a STROBE-compliant manuscript draft. Built around a provenance-first, outcome-masking workflow to prevent HARKing and p-hacking.

## Setup & Installation

Install from PyPI:
```bash
pip install research-tool-cli
```

Or install locally in editable mode:
```bash
pip install -e .
```

For development and tests, install the project environment with its dev dependencies and run pytest through that interpreter:

```bash
uv sync --dev
.venv/bin/python -m pytest
```

This keeps pytest on the same Python environment as scipy, pandas, and tableone.

## Usage (CLI)

```bash
# ── Phase 1: Pre-Unmasking Protocol Specification ──────────────────────────
# 1. Create study
research-tool new-study "EMD Study"

# 2. Ingest CSV data
research-tool ingest <STUDY_ID> /Users/manjunathnk/Research/research-tool/synthetic_21_v2.csv

# 3. Classify variables (masks outcome variables)
research-tool classify-variables <STUDY_ID>

# 4. Explore baseline data (outcomes masked)
research-tool explore-baseline <STUDY_ID>

# 5. Declare study plan
research-tool plan <STUDY_ID> --type cohort --comparison "PFS by treatment arm" --outcome-var-ids "8,9" --test "8:kaplan_meier_logrank:KM PFS comparison" --cox-ph-models "pfs_multivariable:pfs_days:pfs_event:treatment_arm:age,high_risk_fish,prior_lines:Adjusted PFS model"

# 6. Lock study plan
research-tool lock <STUDY_ID>

# ── Phase 2: Data Unmasking & Execution ──────────────────────────────────
# 7. Unmask outcome data
research-tool unmask <STUDY_ID>

# 8. Run analyses
research-tool analyze <STUDY_ID> --force

# ── Phase 3: Visualizations & Tables ──────────────────────────────────────
# 9. Table 1 (baseline characteristics)
research-tool table1 <STUDY_ID>

# 10. Kaplan-Meier plot (requires test_id, e.g. 1)
research-tool plot-km <STUDY_ID> 1

# 11. Forest plot (SVG & ASCII)
research-tool plot-forest <STUDY_ID>
research-tool plot-forest <STUDY_ID> --ascii

# 12. CONSORT flowchart
research-tool flowchart <STUDY_ID>

# ── Phase 4: Compliance & Reporting ───────────────────────────────────────
# 13. STROBE checklist audit
research-tool strobe-check <STUDY_ID>

# 14. Manuscript draft
research-tool draft <STUDY_ID>

# 15. Provenance lineage DAG
research-tool lineage <STUDY_ID>

# 16. Data forensics
research-tool forensics <STUDY_ID>

# 17. Reviewer JSON export
research-tool export <STUDY_ID>

# ── Phase 5: Bundling, Excel Export & Verification ───────────────────────
# 18. Create hash-verified bundle archive (generates manifest & composite SHA-256)
research-tool bundle <STUDY_ID>

# 19. Export Excel report (reads bundle manifest & populates composite hash on Tab 4)
research-tool export-excel <STUDY_ID>

# 20. Verify bundle integrity
research-tool verify-bundle data/studies/<STUDY_ID>/<STUDY_ID>_bundle.tar.gz
```

> **Note:** Variable IDs are auto-assigned per-study at ingest time and are **not predictable**. Always run `list-variables` after `classify-variables` to get the correct IDs for `--outcome-var-ids`.

See `research-tool --help` and `app/README.md` for web interface instructions.

---

## Authors & Acknowledgments

- **Lead Maintainer**: [Manjunath N K](https://github.com/nkmanjunath)
- **AI Pair Programming & Collaboration**:
  - **Google Antigravity (AGY)** — Pair Programming, 4-Gate Diagnostics & UI/UX design.
  - **OpenCode** — Statistical engine verification & benchmarking fixtures.
