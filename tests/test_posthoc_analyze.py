"""Tests for post-hoc amendment execution and deduplication."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.masking.gate import seal_outcomes, unmask_study
from core.planning.study_plan import StudyPlan, CoxPHModel
from core.planning.lock import lock_plan, lock_amendment, load_plan
from core.cli.main import cmd_analyze
import argparse


STUDY_ID = "test_posthoc"


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
        (STUDY_ID, "Post-hoc Test", "2025-01-01", str(DATA_ROOT / STUDY_ID), "cohort", 0),
    )
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT, pfs_days TEXT, pfs_event TEXT, treatment_arm TEXT)")
    for i in range(20):
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


def test_posthoc_test_executed_by_analyze():
    """Post-hoc tests added via amend must be executed by analyze without --post-hoc flag."""
    _set_state(1)
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by arm",
        planned_tests=[],
    )
    lock_plan(STUDY_ID, plan)
    _set_state(2)  # unmasked
    unmask_study(STUDY_ID)

    lock_amendment(
        STUDY_ID,
        amendment_reason="Add logrank test",
        post_hoc_tests=[{"variable_name": "pfs_days", "test_name": "kaplan_meier_logrank", "rationale": "Post-hoc KM"}],
    )

    args = argparse.Namespace(study_id=STUDY_ID, force=False, post_hoc=False, rerun=False)
    cmd_analyze(args)

    conn = get_connection(STUDY_ID)
    rows = conn.execute(
        "SELECT test_name, is_pre_registered, status_json FROM analysis_results WHERE study_id=?",
        (STUDY_ID,),
    ).fetchall()
    conn.close()

    logrank_results = [r for r in rows if r["test_name"] == "kaplan_meier_logrank"]
    assert len(logrank_results) == 1, f"Expected 1 logrank result, got {len(logrank_results)}"
    assert logrank_results[0]["is_pre_registered"] == 0, "Post-hoc test should have is_pre_registered=0"
    status = json.loads(logrank_results[0]["status_json"])
    assert status["status"] in ("completed", "error"), f"Unexpected status: {status}"


def test_amend_dedup_prevents_duplicate_posthoc_tests():
    """Amending with the same test twice should not create duplicates."""
    _set_state(1)
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by arm",
        planned_tests=[],
    )
    lock_plan(STUDY_ID, plan)
    _set_state(2)
    unmask_study(STUDY_ID)

    lock_amendment(
        STUDY_ID,
        amendment_reason="First add",
        post_hoc_tests=[{"variable_name": "pfs_days", "test_name": "kaplan_meier_logrank", "rationale": "Post-hoc KM"}],
    )
    lock_amendment(
        STUDY_ID,
        amendment_reason="Second add (duplicate)",
        post_hoc_tests=[{"variable_name": "pfs_days", "test_name": "kaplan_meier_logrank", "rationale": "Post-hoc KM"}],
    )

    latest = load_plan(STUDY_ID)
    logrank_tests = [t for t in latest.post_hoc_tests if t["test_name"] == "kaplan_meier_logrank"]
    assert len(logrank_tests) == 1, f"Expected 1 logrank test after dedup, got {len(logrank_tests)}"


def test_planned_and_posthoc_both_executed():
    """analyze should run both planned_tests and post_hoc_tests in a single call."""
    _set_state(1)
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by arm",
        planned_tests=[{"variable_name": "pfs_days", "test_name": "kaplan_meier_logrank", "rationale": "Pre-registered KM"}],
    )
    lock_plan(STUDY_ID, plan)
    _set_state(2)
    unmask_study(STUDY_ID)

    lock_amendment(
        STUDY_ID,
        amendment_reason="Add chi-square",
        post_hoc_tests=[{"variable_name": "pfs_event", "test_name": "chi_square", "rationale": "Post-hoc chi"}],
    )

    args = argparse.Namespace(study_id=STUDY_ID, force=False, post_hoc=False, rerun=False)
    cmd_analyze(args)

    conn = get_connection(STUDY_ID)
    rows = conn.execute(
        "SELECT test_name, is_pre_registered FROM analysis_results WHERE study_id=?",
        (STUDY_ID,),
    ).fetchall()
    conn.close()

    pre_reg = [r for r in rows if r["is_pre_registered"] == 1]
    post_hoc = [r for r in rows if r["is_pre_registered"] == 0]
    assert len(pre_reg) == 1, f"Expected 1 pre-registered result, got {len(pre_reg)}"
    assert len(post_hoc) == 1, f"Expected 1 post-hoc result, got {len(post_hoc)}"
    assert pre_reg[0]["test_name"] == "kaplan_meier_logrank"
    assert post_hoc[0]["test_name"] == "chi_square"


def test_plot_km_works_with_posthoc_km_result():
    """plot-km must succeed against a post-hoc kaplan_meier_logrank result."""
    from core.reporting.plots import _resolve_km_vars

    _set_state(1)
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by arm",
        planned_tests=[],
    )
    lock_plan(STUDY_ID, plan)
    _set_state(2)
    unmask_study(STUDY_ID)

    lock_amendment(
        STUDY_ID,
        amendment_reason="Add KM test",
        post_hoc_tests=[{"variable_name": "pfs_days", "test_name": "kaplan_meier_logrank", "rationale": "Post-hoc KM"}],
    )

    args = argparse.Namespace(study_id=STUDY_ID, force=False, post_hoc=False, rerun=False)
    cmd_analyze(args)

    # Find the kaplan_meier_logrank result ID
    conn = get_connection(STUDY_ID)
    row = conn.execute(
        "SELECT id FROM analysis_results WHERE study_id=? AND test_name='kaplan_meier_logrank' LIMIT 1",
        (STUDY_ID,),
    ).fetchone()
    conn.close()
    assert row is not None, "Expected a kaplan_meier_logrank result"

    # _resolve_km_vars should not raise — it must find the test in post_hoc_tests
    resolved = _resolve_km_vars(STUDY_ID, row["id"])
    assert resolved["time_col"] == "pfs_days"
    assert resolved["event_col"] == "pfs_event"
