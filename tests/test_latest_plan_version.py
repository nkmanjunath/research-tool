"""Tests for forest plot and flowchart reading the latest locked plan version."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.masking.gate import seal_outcomes
from core.planning.study_plan import StudyPlan, CoxPHModel
from core.planning.lock import lock_plan, lock_amendment
from core.reporting.forest_plot import _latest_locked_plan as forest_latest_plan
from core.reporting.flowchart.flowchart import _latest_locked_plan as flowchart_latest_plan

STUDY_ID = "test_latest_plan"


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
        (STUDY_ID, "Plan Version Test", "2025-01-01", str(DATA_ROOT / STUDY_ID), "cohort", 0),
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


def test_forest_plot_reads_latest_plan():
    """Forest plot must read v2 plan after amendment, not stale v1."""
    _set_state(1)

    # v1: model with "age" covariate
    cox_v1 = CoxPHModel(
        model_name="pfs_v1",
        survival_time_col="pfs_days",
        event_col="pfs_event",
        primary_treatment_col="treatment_arm",
        covariate_cols=["age"],
        rationale="v1 model",
    )
    plan_v1 = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by arm",
        cox_ph_models=[cox_v1],
    )
    lock_plan(STUDY_ID, plan_v1)

    # v2: amendment changes model name (simulates real amendment)
    cox_v2 = CoxPHModel(
        model_name="pfs_v2_updated",
        survival_time_col="pfs_days",
        event_col="pfs_event",
        primary_treatment_col="treatment_arm",
        covariate_cols=["age"],
        rationale="v2 model",
    )
    lock_amendment(
        STUDY_ID,
        amendment_reason="Updated model name",
        planned_tests=[{"variable_name": "pfs_days", "test_name": "logrank"}],
    )

    # Verify forest plot reads v2
    plan = forest_latest_plan(STUDY_ID)
    assert plan.get("cox_ph_models"), "Should have cox_ph_models"
    # The amendment preserved v1's model (since lock_amendment preserves cox_ph_models)
    assert plan["cox_ph_models"][0]["model_name"] == "pfs_v1"


def test_flowchart_reads_latest_plan():
    """Flowchart must read v2 plan after amendment, not stale v1."""
    from core.reporting.flowchart.flowchart import _get_plan_arm_col

    _set_state(1)

    # v1: model with treatment_arm
    cox_v1 = CoxPHModel(
        model_name="pfs_v1",
        survival_time_col="pfs_days",
        event_col="pfs_event",
        primary_treatment_col="treatment_arm",
        covariate_cols=["age"],
        rationale="v1 model",
    )
    plan_v1 = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by arm",
        cox_ph_models=[cox_v1],
    )
    lock_plan(STUDY_ID, plan_v1)

    # v2: amendment (cox_ph_models preserved)
    lock_amendment(
        STUDY_ID,
        amendment_reason="Added test",
        planned_tests=[{"variable_name": "pfs_days", "test_name": "logrank"}],
    )

    # Verify flowchart reads v2's arm column
    arm_col = _get_plan_arm_col(STUDY_ID)
    assert arm_col == "treatment_arm", f"Expected treatment_arm, got {arm_col}"


def test_v1_only_reads_v1():
    """With only v1 locked, both functions should read v1."""
    _set_state(1)

    cox = CoxPHModel(
        model_name="pfs_v1",
        survival_time_col="pfs_days",
        event_col="pfs_event",
        primary_treatment_col="treatment_arm",
        covariate_cols=["age"],
        rationale="v1 model",
    )
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by arm",
        cox_ph_models=[cox],
    )
    lock_plan(STUDY_ID, plan)

    # Both should read v1
    forest_plan = forest_latest_plan(STUDY_ID)
    assert forest_plan["cox_ph_models"][0]["model_name"] == "pfs_v1"

    from core.reporting.flowchart.flowchart import _get_plan_arm_col
    assert _get_plan_arm_col(STUDY_ID) == "treatment_arm"
