# Retrospective Clinical Research Tool

Local-first CLI tool that takes raw retrospective clinical study data to a
STROBE-compliant manuscript draft. Built around a provenance-first,
outcome-masking workflow to prevent HARKing and p-hacking.

## Setup

```bash
pip install -e .
```

For development and tests, install the project environment with its dev
dependencies and run pytest through that interpreter:

```bash
uv sync --dev
.venv/bin/python -m pytest
```

This keeps pytest on the same Python environment as scipy, pandas, and
tableone.

## Usage

```bash
research-tool new-study "Myeloma EMD Study"
research-tool ingest study_1 data.csv
research-tool classify-variables study_1
research-tool explore-baseline study_1
research-tool table1 study_1
research-tool plan study_1
research-tool lock study_1
research-tool unmask study_1
research-tool analyze study_1
research-tool strobe-check study_1
research-tool draft study_1
```

See `research-tool --help` for details.
