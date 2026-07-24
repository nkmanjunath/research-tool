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
    data_dir    TEXT NOT NULL
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
    computed_at       TEXT NOT NULL
);
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
class StudyPlan:
    study_id: str
    version: int = 1
    locked_at: Optional[str] = None
    primary_comparison: str = ""
    primary_outcome_variable_ids: list[int] = field(default_factory=list)
    planned_tests: list[dict] = field(default_factory=list)
    covariates: list[int] = field(default_factory=list)
    file_path: Optional[str] = None
    warnings: dict[str, str] = field(default_factory=dict)
    role_overrides: dict[int, str] = field(default_factory=dict)
    audit: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> StudyPlan:
        d.setdefault("warnings", {})
        d.setdefault("role_overrides", {})
        d.setdefault("audit", {})
        d["role_overrides"] = {int(k): v for k, v in d["role_overrides"].items()}
        return StudyPlan(**d)


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
