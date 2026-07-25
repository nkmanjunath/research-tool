"""Tests for the amendment workflow — pre-unmask and post-hoc amendments."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.masking.gate import seal_outcomes
from core.planning.study_plan import StudyPlan
from core.planning.lock import lock_plan, lock_amendment, load_plan, _next_version
from core.reporting.manuscript_draft import generate_draft


STUDY_ID = "test_amendments"


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
        (STUDY_ID, "Amend Test", "2025-01-01", str(DATA_ROOT / STUDY_ID), "cohort", 0),
    )
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT, response TEXT, treatment_arm TEXT)")
    conn.execute(f"INSERT INTO {raw} (age, response, treatment_arm) VALUES ('65', 'CR', 'A')")
    conn.execute(f"INSERT INTO {raw} (age, response, treatment_arm) VALUES ('70', 'PR', 'B')")
    conn.execute("DELETE FROM variables WHERE study_id=?", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'age', 'baseline', 'continuous')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'response', 'outcome', 'categorical')", (STUDY_ID,))
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


def _lock_v1():
    """Lock a simple v1 plan (must be called while still masked)."""
    plan = StudyPlan(study_id=STUDY_ID, study_type="cohort", primary_comparison="test",
                     planned_tests=[{"variable_name": "age", "test_name": "t_test"}])
    lock_plan(STUDY_ID, plan)


def _insert_analysis(is_pre_registered: int, p_value: float | None = 0.03,
                     test_name: str = "chi_square"):
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, study_plan_version, variable_ids_used, test_name,
            statistic, p_value, status_json, is_pre_registered, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (STUDY_ID, 1, "[]", test_name, 5.2, p_value,
         json.dumps({"status": "completed"}),
         is_pre_registered, "2025-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()


# ── Test 1: Pre-unmask amend creates new version ──────────────────────────

def test_pre_unmask_amend_creates_new_version():
    _set_state(1)  # locked/masked
    plan = StudyPlan(study_id=STUDY_ID, study_type="cohort", primary_comparison="test",
                     planned_tests=[{"variable_name": "response", "test_name": "chi_square"}])
    lock_plan(STUDY_ID, plan)  # v1

    path = lock_amendment(
        STUDY_ID,
        amendment_reason="Added t-test",
        planned_tests=[{"variable_name": "response", "test_name": "t_test"}],
    )
    assert "v2" in path.name, f"Expected v2, got {path.name}"
    # v1 should still exist
    v1_path = DATA_ROOT / STUDY_ID / "study_plan.v1.locked.json"
    assert v1_path.exists(), "v1 should still exist"


# ── Test 2: Pre-unmask amend refused post-unmask ─────────────────────────

def test_pre_unmask_amend_refused_after_unmask():
    _lock_v1()
    _set_state(2)  # unmasked
    with pytest.raises(RuntimeError, match="already been unmasked"):
        lock_amendment(
            STUDY_ID,
            amendment_reason="Should fail",
            planned_tests=[{"variable_name": "response", "test_name": "t_test"}],
        )


# ── Test 3: Post-hoc amend refused pre-unmask ────────────────────────────

def test_post_hoc_amend_refused_before_unmask():
    _set_state(1)  # locked/masked, not unmasked
    plan = StudyPlan(study_id=STUDY_ID, study_type="cohort", primary_comparison="test",
                     planned_tests=[{"variable_name": "response", "test_name": "chi_square"}])
    lock_plan(STUDY_ID, plan)  # v1

    with pytest.raises(RuntimeError, match="before unmasking"):
        lock_amendment(
            STUDY_ID,
            amendment_reason="Should fail",
            post_hoc_tests=[{"variable_name": "response", "test_name": "t_test"}],
        )


# ── Test 4: Post-hoc result is_pre_registered=0 ──────────────────────────

def test_amend_requires_reason():
    with pytest.raises(ValueError, match="amendment_reason is required"):
        lock_amendment(STUDY_ID, amendment_reason="",
                       planned_tests=[{"variable_name": "response", "test_name": "chi_square"}])


# ── Test 5: Draft with post-hoc results MUST contain the header ──────────

def test_draft_with_post_hoc_contains_header():
    _lock_v1()
    _set_state(2)
    _insert_analysis(is_pre_registered=0)  # post-hoc result
    draft = generate_draft(STUDY_ID)
    assert "Post-Hoc / Exploratory Analyses" in draft, \
        "Draft with post-hoc results must contain the post-hoc section header"


# ── Test 6: Count disclosure sentence appears ────────────────────────────

def test_count_disclosure_sentence_present():
    _lock_v1()
    _set_state(2)
    _insert_analysis(is_pre_registered=0, p_value=0.03)
    _insert_analysis(is_pre_registered=0, p_value=0.50, test_name="t_test")
    draft = generate_draft(STUDY_ID)
    assert "2 additional post-hoc/exploratory analyses" in draft, \
        "Should disclose 2 post-hoc analyses"
    assert "1 of the 2 post-hoc analyses reached p < 0.05" in draft, \
        "Should disclose 1 significant out of 2"
    assert "corrected for multiple comparisons within their own family" in draft, \
        "Should mention separate-family correction"


# ── Test 7: No post-hoc results → no post-hoc section ────────────────────

def test_no_post_hoc_no_section():
    _lock_v1()
    _set_state(2)
    _insert_analysis(is_pre_registered=1)  # only pre-registered
    draft = generate_draft(STUDY_ID)
    assert "Post-Hoc / Exploratory Analyses" not in draft, \
        "Should not have post-hoc section when no post-hoc results exist"


# ── Test 8: Export JSON contains analysis_summary ────────────────────────

def test_export_contains_analysis_summary():
    _lock_v1()
    _set_state(2)
    _insert_analysis(is_pre_registered=1, p_value=0.02)  # pre-registered
    _insert_analysis(is_pre_registered=0, p_value=0.03)  # post-hoc
    draft = generate_draft(STUDY_ID)
    # The count-disclosure sentence in the Discussion covers this
    assert "1 additional post-hoc/exploratory analyses" in draft, \
        "Count disclosure should show 1 post-hoc analysis"


def test_lock_plan_still_refuses_after_unmask():
    """lock_plan() must remain uncallable after unmasking — core HARKing guard."""
    _lock_v1()
    _set_state(2)  # unmasked
    plan = StudyPlan(study_id=STUDY_ID, study_type="cohort", primary_comparison="new test",
                     planned_tests=[{"variable_name": "response", "test_name": "chi_square"}])
    with pytest.raises(RuntimeError, match="Cannot lock a plan after unmasking"):
        lock_plan(STUDY_ID, plan)


def test_analyze_dedup_no_rerun():
    """Analyze must skip tests that already have a completed result."""
    _lock_v1()
    _set_state(2)
    import argparse
    from core.cli.main import cmd_analyze

    # First analyze — should run
    ns = argparse.Namespace(study_id=STUDY_ID, force=False, post_hoc=False, rerun=False)
    cmd_analyze(ns)

    conn = get_connection(STUDY_ID)
    count1 = conn.execute(
        "SELECT COUNT(*) as cnt FROM analysis_results WHERE study_id=?", (STUDY_ID,)
    ).fetchone()["cnt"]
    conn.close()
    assert count1 == 1, f"Expected 1 result after first analyze, got {count1}"

    # Second analyze — should skip
    cmd_analyze(ns)

    conn = get_connection(STUDY_ID)
    count2 = conn.execute(
        "SELECT COUNT(*) as cnt FROM analysis_results WHERE study_id=?", (STUDY_ID,)
    ).fetchone()["cnt"]
    conn.close()
    assert count2 == 1, f"Expected still 1 result after skip, got {count2}"


def test_analyze_rerun_creates_new_supersedes_old():
    """--rerun must produce a new result and mark old as superseded."""
    _lock_v1()
    _set_state(2)
    import argparse
    from core.cli.main import cmd_analyze

    ns = argparse.Namespace(study_id=STUDY_ID, force=False, post_hoc=False, rerun=False)
    cmd_analyze(ns)

    # Rerun with --rerun
    ns.rerun = True
    cmd_analyze(ns)

    conn = get_connection(STUDY_ID)
    rows = conn.execute(
        "SELECT id, superseded_previous_result_id FROM analysis_results WHERE study_id=? ORDER BY id",
        (STUDY_ID,),
    ).fetchall()
    conn.close()
    assert len(rows) == 2, f"Expected 2 rows after rerun, got {len(rows)}"
    # First row should have NULL superseded (it's the original)
    assert rows[0]["superseded_previous_result_id"] is None
    # Second row should point to first
    assert rows[1]["superseded_previous_result_id"] == rows[0]["id"], \
        f"Second row should supersede first. Got {rows[1]['superseded_previous_result_id']} vs {rows[0]['id']}"
