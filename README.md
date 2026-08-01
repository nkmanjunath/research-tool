# Retrospective Clinical Research Tool

Local-first CLI and Web application that takes raw retrospective clinical study data to a STROBE-compliant manuscript draft. Built around a provenance-first, outcome-masking workflow to prevent HARKing and p-hacking.

## Setup

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
STUDY=$(research-tool new-study "Myeloma EMD Study")
research-tool ingest $STUDY data.csv
research-tool classify-variables $STUDY

# Look up outcome variable IDs (assigned per-study at ingest time)
research-tool list-variables $STUDY

# Use the IDs from list-variables for outcome columns (e.g. pfs_days=8, pfs_event=9)
research-tool plan $STUDY \
  --comparison "PFS by treatment arm" \
  --outcome-var-ids "8,9" \
  --cox-ph-models "pfs_model:pfs_days:pfs_event:treatment_arm:age,iss_stage:Adjusted PFS model"
research-tool lock $STUDY
research-tool unmask $STUDY
research-tool analyze $STUDY
research-tool flowchart $STUDY
research-tool plot-forest $STUDY
research-tool strobe-check $STUDY
research-tool draft $STUDY
research-tool export-excel $STUDY
```

> **Note:** Variable IDs are auto-assigned per-study at ingest time and are **not predictable**. Always run `list-variables` after `classify-variables` to get the correct IDs for `--outcome-var-ids`.

See `research-tool --help` and `app/README.md` for web interface instructions.

---

## Authors & Acknowledgments

- **Lead Maintainer**: [Manjunath N K](https://github.com/nkmanjunath)
- **AI Pair Programming & Collaboration**:
  - **Google Antigravity (AGY)** — Pair Programming, 4-Gate Diagnostics & UI/UX design.
  - **OpenCode** — Statistical engine verification & benchmarking fixtures.
