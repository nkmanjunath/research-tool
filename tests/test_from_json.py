"""Tests for --from-json plan loading with native JSON types."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.masking.gate import seal_outcomes
from core.planning.lock import load_plan

STUDY_ID = "test_from_json"


@pytest.fixture(autouse=True)
def _setup():
    p = DATA_ROOT / STUDY_ID
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)

    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, study_type, is_locked) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (STUDY_ID, "JSON Test", "2025-01-01", str(DATA_ROOT / STUDY_ID), "cohort", 0),
    )
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT, pfs_days TEXT, pfs_event TEXT, treatment_arm TEXT)")
    conn.execute(f"INSERT INTO {raw} (age, pfs_days, pfs_event, treatment_arm) VALUES ('65', '120', '1', 'A')")
    conn.execute(f"INSERT INTO {raw} (age, pfs_days, pfs_event, treatment_arm) VALUES ('70', '90', '0', 'B')")
    conn.execute("DELETE FROM variables WHERE study_id=?", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'age', 'baseline', 'continuous')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'pfs_days', 'outcome', 'time_to_event')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'pfs_event', 'outcome', 'categorical')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'treatment_arm', 'baseline', 'categorical')", (STUDY_ID,))
    conn.commit()
    conn.close()

    seal_outcomes(STUDY_ID)
    yield
    if p.exists():
        shutil.rmtree(p)


def _set_state(state: int):
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute("UPDATE studies SET is_locked=? WHERE id=?", (state, STUDY_ID))
    conn.commit()
    conn.close()


def test_from_json_native_types(tmp_path):
    """--from-json must accept native JSON arrays, not just CLI-encoded strings."""
    from core.cli.main import cmd_plan
    import argparse

    # Get actual variable IDs from the test DB
    conn = get_connection(STUDY_ID)
    rows = conn.execute("SELECT id, column_name FROM variables WHERE study_id=? ORDER BY id", (STUDY_ID,)).fetchall()
    conn.close()
    var_ids = {r["column_name"]: r["id"] for r in rows}

    json_data = {
        "comparison": "PFS by treatment arm",
        "outcome_var_ids": [var_ids["pfs_days"], var_ids["pfs_event"]],  # native array
        "study_type": "cohort",
        "tests": [
            {"variable_name": "pfs_days", "test_name": "logrank", "rationale": "Compare PFS"}
        ],
        "covariates": [var_ids["age"]],  # native array
        "cox_ph_models": [
            {
                "model_name": "pfs_model",
                "survival_time_col": "pfs_days",
                "event_col": "pfs_event",
                "primary_treatment_col": "treatment_arm",
                "covariate_cols": ["age"],
                "rationale": "Adjusted PFS model",
            }
        ],
    }
    json_path = tmp_path / "plan.json"
    json_path.write_text(json.dumps(json_data))

    args = argparse.Namespace(
        study_id=STUDY_ID,
        comparison=None,
        outcome_var_ids=None,
        study_type="cohort",
        tests=None,
        covariates=None,
        cox_ph_models=None,
        interaction_terms=None,
        matching_criteria=None,
        overrides=[],
        from_json=str(json_path),
    )

    _set_state(0)
    cmd_plan(args)

    # Verify the provisional plan was saved
    plan_path = DATA_ROOT / STUDY_ID / "study_plan.provisional.json"
    assert plan_path.exists(), "Provisional plan should be saved"

    plan = json.loads(plan_path.read_text())
    assert plan["primary_comparison"] == "PFS by treatment arm"
    assert len(plan["planned_tests"]) == 1
    assert plan["planned_tests"][0]["test_name"] == "logrank"
    assert len(plan["cox_ph_models"]) == 1
    assert plan["cox_ph_models"][0]["model_name"] == "pfs_model"
    assert plan["cox_ph_models"][0]["covariate_cols"] == ["age"]


def test_from_json_with_lock(tmp_path):
    """--from-json plan must survive lock and reload correctly."""
    from core.cli.main import cmd_plan, cmd_lock
    import argparse

    # Get actual variable IDs from the test DB
    conn = get_connection(STUDY_ID)
    rows = conn.execute("SELECT id, column_name FROM variables WHERE study_id=? ORDER BY id", (STUDY_ID,)).fetchall()
    conn.close()
    var_ids = {r["column_name"]: r["id"] for r in rows}

    json_data = {
        "comparison": "PFS by treatment arm",
        "outcome_var_ids": [var_ids["pfs_days"], var_ids["pfs_event"]],
        "cox_ph_models": [
            {
                "model_name": "pfs_model",
                "survival_time_col": "pfs_days",
                "event_col": "pfs_event",
                "primary_treatment_col": "treatment_arm",
                "covariate_cols": ["age"],
                "rationale": "Adjusted PFS model",
            }
        ],
    }
    json_path = tmp_path / "plan.json"
    json_path.write_text(json.dumps(json_data))

    args = argparse.Namespace(
        study_id=STUDY_ID,
        comparison=None,
        outcome_var_ids=None,
        study_type="cohort",
        tests=None,
        covariates=None,
        cox_ph_models=None,
        interaction_terms=None,
        matching_criteria=None,
        overrides=[],
        from_json=str(json_path),
    )

    _set_state(0)
    cmd_plan(args)

    # Now lock it
    lock_args = argparse.Namespace(study_id=STUDY_ID, allow_duplicate_ids=False)
    _set_state(0)
    cmd_lock(lock_args)

    # Reload and verify
    plan = load_plan(STUDY_ID)
    assert len(plan.cox_ph_models) == 1
    assert plan.cox_ph_models[0].model_name == "pfs_model"
    assert plan.cox_ph_models[0].event_col == "pfs_event"
    assert plan.cox_ph_models[0].covariate_cols == ["age"]
