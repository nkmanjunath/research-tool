"""Regression tests for analyze batch robustness.

Bug: --test "8:kaplan_meier_logrank:..." stored ID "8" as variable_name,
but event_col derivation expected column name "pfs_days" → produced "8_event".

Bug: One test failure aborted entire batch — Cox PH never ran.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.masking.gate import seal_outcomes, unmask_study
from core.planning.study_plan import StudyPlan
from core.planning.lock import lock_plan, load_plan
from core.cli.main import cmd_analyze
import argparse


STUDY_ID = "test_batch_robust"


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
        (STUDY_ID, "Batch Robust Test", "2025-01-01", str(DATA_ROOT / STUDY_ID), "cohort", 0),
    )
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT, pfs_days TEXT, pfs_event TEXT, treatment_arm TEXT)")
    for i in range(30):
        arm = "A" if i % 2 == 0 else "B"
        event = "1" if i % 3 == 0 else "0"
        conn.execute(f"INSERT INTO {raw} (age, pfs_days, pfs_event, treatment_arm) VALUES (?, ?, ?, ?)",
                     (str(30 + i), str(100 + i * 5), event, arm))
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


def test_var_id_resolved_to_column_name():
    """Variable ID '8' in --test must resolve to column_name before event_col derivation."""
    # Get the actual variable ID for pfs_days
    conn = get_connection(STUDY_ID)
    row = conn.execute("SELECT id FROM variables WHERE study_id=? AND column_name=?", (STUDY_ID, "pfs_days")).fetchone()
    conn.close()
    pfs_var_id = str(row["id"])

    _set_state(1)
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by arm",
        planned_tests=[
            {"variable_name": pfs_var_id, "test_name": "kaplan_meier_logrank", "rationale": "KM test"},
        ],
    )
    lock_plan(STUDY_ID, plan)
    _set_state(2)
    unmask_study(STUDY_ID)

    args = argparse.Namespace(study_id=STUDY_ID, force=False, post_hoc=False, rerun=False)
    cmd_analyze(args)

    conn = get_connection(STUDY_ID)
    rows = conn.execute(
        "SELECT test_name, status_json FROM analysis_results WHERE study_id=?",
        (STUDY_ID,),
    ).fetchall()
    conn.close()

    km_results = [r for r in rows if r["test_name"] == "kaplan_meier_logrank"]
    assert len(km_results) == 1
    status = json.loads(km_results[0]["status_json"])
    # Should succeed, not fail with "8_event not found"
    assert status["status"] == "completed", f"Expected completed, got {status}"


def test_batch_continues_after_test_failure():
    """One test failure must not abort remaining tests in the batch."""
    _set_state(1)
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by arm",
        planned_tests=[
            # This will fail: no event column "nonexistent_event"
            {"variable_name": "pfs_days", "test_name": "kaplan_meier_logrank", "rationale": "KM test"},
            # This should still run
            {"variable_name": "age", "test_name": "t_test", "rationale": "Age comparison"},
        ],
    )
    lock_plan(STUDY_ID, plan)
    _set_state(2)
    unmask_study(STUDY_ID)

    # Monkey-patch run_test to simulate failure for KM
    import core.cli.main as main_mod
    original_run_test = main_mod.run_test

    def mock_run_test(test_name, data, **kwargs):
        if test_name == "kaplan_meier_logrank":
            raise ValueError("Simulated failure: no linked event column")
        return original_run_test(test_name, data, **kwargs)

    main_mod.run_test = mock_run_test
    try:
        args = argparse.Namespace(study_id=STUDY_ID, force=False, post_hoc=False, rerun=False)
        cmd_analyze(args)
    finally:
        main_mod.run_test = original_run_test

    conn = get_connection(STUDY_ID)
    rows = conn.execute(
        "SELECT test_name, status_json FROM analysis_results WHERE study_id=?",
        (STUDY_ID,),
    ).fetchall()
    conn.close()

    # Both tests should have results
    assert len(rows) == 2, f"Expected 2 results, got {len(rows)}"

    km_result = [r for r in rows if r["test_name"] == "kaplan_meier_logrank"][0]
    ttest_result = [r for r in rows if r["test_name"] == "t_test"][0]

    km_status = json.loads(km_result["status_json"])
    ttest_status = json.loads(ttest_result["status_json"])

    # KM should be error, t_test should be completed
    assert km_status["status"] == "error"
    assert "Simulated failure" in km_status.get("reason", "")
    assert ttest_status["status"] == "completed"
