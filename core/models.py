"""Data model dataclasses and SQL schema for the research tool."""

from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

# ── SQL schema ──────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS studies (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,       -- ISO-8601
    study_type  TEXT,                -- cohort | case_control | cross_sectional
    is_locked   INTEGER NOT NULL DEFAULT 0,  -- 0 = pre-lock, 1 = locked, 2 = unmasked
    unmasked_at TEXT,                -- ISO-8601 timestamp of unmask event
    data_dir    TEXT NOT NULL,
    unmask_audit_json TEXT           -- JSON array of override events
);

CREATE TABLE IF NOT EXISTS variables (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id    TEXT NOT NULL REFERENCES studies(id),
    column_name TEXT NOT NULL,
    role        TEXT NOT NULL,       -- baseline | outcome
    data_type   TEXT NOT NULL,       -- categorical | continuous | time_to_event
    is_masked   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(study_id, column_name)
);

CREATE TABLE IF NOT EXISTS raw_data (
    row_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id  TEXT NOT NULL REFERENCES studies(id),
    json_row  TEXT NOT NULL          -- full row as JSON for flexibility
);

CREATE TABLE IF NOT EXISTS analysis_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id          TEXT NOT NULL REFERENCES studies(id),
    study_plan_version INTEGER,
    variable_ids_used TEXT,          -- JSON list
    test_name         TEXT,
    statistic         REAL,
    p_value           REAL,
    adjusted_p_value  REAL,
    ci_lower          REAL,
    ci_upper          REAL,
    effect_size_json  TEXT,          -- JSON: {"metric": "Cramér's V", "value": 0.32}
    sample_counts_json TEXT,         -- JSON: {"n_total": 21, "n_analyzed": 21, "n_excluded": 0}
    status_json       TEXT,          -- JSON: {"status": "completed", "reason": "..."}
    is_pre_registered INTEGER NOT NULL DEFAULT 1,
    provenance_json   TEXT,          -- JSON blob
    computed_at       TEXT NOT NULL,
    superseded_previous_result_id INTEGER DEFAULT NULL,
    lr_test_p         REAL,
    concordance_index REAL,
    ph_diagnostics_json TEXT
);

CREATE TABLE IF NOT EXISTS analysis_covariate_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id   INTEGER NOT NULL REFERENCES analysis_results(id),
    covariate   TEXT NOT NULL,
    hr          REAL,
    ci_lower    REAL,
    ci_upper    REAL,
    wald_p      REAL,
    coef        REAL,
    se          REAL,
    z           REAL,
    reference_level TEXT,
    tested_level TEXT
);
"""

MIGRATIONS_SQL = """
ALTER TABLE analysis_results ADD COLUMN lr_test_p REAL;
ALTER TABLE analysis_results ADD COLUMN concordance_index REAL;
ALTER TABLE analysis_results ADD COLUMN ph_diagnostics_json TEXT;

CREATE TABLE IF NOT EXISTS analysis_covariate_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id   INTEGER NOT NULL REFERENCES analysis_results(id),
    covariate   TEXT NOT NULL,
    hr          REAL,
    ci_lower    REAL,
    ci_upper    REAL,
    wald_p      REAL,
    coef        REAL,
    se          REAL,
    z           REAL,
    reference_level TEXT,
    tested_level TEXT
);
ALTER TABLE analysis_covariate_results ADD COLUMN reference_level TEXT;
ALTER TABLE analysis_covariate_results ADD COLUMN tested_level TEXT;
ALTER TABLE studies ADD COLUMN unmask_audit_json TEXT;
"""

# ── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass
class Study:
    id: str
    name: str
    created_at: str  # ISO-8601
    study_type: Optional[str] = None
    is_locked: int = 0
    data_dir: str = ""


@dataclass
class Variable:
    id: int
    study_id: str
    column_name: str
    role: str  # "baseline" | "outcome"
    data_type: str  # "categorical" | "continuous" | "time_to_event"
    is_masked: bool = True


@dataclass
class AnalysisResult:
    id: int
    study_id: str
    study_plan_version: int
    variable_ids_used: list[int]
    test_name: str
    statistic: Optional[float] = None
    p_value: Optional[float] = None
    adjusted_p_value: Optional[float] = None
    confidence_interval: tuple[Optional[float], Optional[float]] = (None, None)
    is_pre_registered: bool = True
    provenance: Optional[dict] = None
    computed_at: Optional[str] = None
