"""Tests for STROBE checklist compliance checker."""

from __future__ import annotations
import shutil
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.masking.gate import seal_outcomes
from core.planning.study_plan import StudyPlan
from core.planning.lock import lock_plan
from core.reporting.strobe_checklist import check_study, generate_report, STROBE_ITEMS

STUDY_ID = "test_strobe"


@pytest.fixture(autouse=True)
def _setup():
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, study_type) VALUES (?, ?, ?, ?, ?)",
        (STUDY_ID, "STROBE Test", "2025-01-01T00:00:00",
         str(Path("data/studies") / STUDY_ID), "cohort"),
    )
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT, response TEXT, treatment_arm TEXT)")
    conn.execute(f"INSERT INTO {raw} (age, response, treatment_arm) VALUES ('65', 'CR', 'A')")
    conn.execute("DELETE FROM variables WHERE study_id=?", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'age', 'baseline', 'continuous')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'response', 'outcome', 'categorical')", (STUDY_ID,))
    conn.commit()
    conn.close()
    yield
    p = DATA_ROOT / STUDY_ID
    if p.exists():
        shutil.rmtree(p)


def test_check_study_returns_list():
    items = check_study(STUDY_ID)
    assert len(items) > 0


def test_check_study_filters_by_type():
    """Only items applicable to 'cohort' should be in the response."""
    items = check_study(STUDY_ID)
    for item in items:
        assert "cohort" in item.applies_to


def test_item_3_satisfied_with_locked_plan():
    """Item 3 (objectives/hypotheses) should be satisfied after locking a plan."""
    plan = StudyPlan(study_id=STUDY_ID, study_type="cohort",
                     primary_comparison="survival by treatment arm")
    lock_plan(STUDY_ID, plan)

    items = check_study(STUDY_ID)
    item3 = next(i for i in items if i.item_id == "3")
    assert item3.satisfied is True


def test_generate_report_includes_status_counts():
    report = generate_report(STUDY_ID)
    assert "STROBE Compliance Report" in report
    # Status symbols: ✓ satisfied, [  ] pending, ✗ unsatisfied
    assert "✓" in report or " [" in report or "✗" in report


def test_strobe_items_have_required_fields():
    for item in STROBE_ITEMS:
        assert item.item_id
        assert item.section
        assert item.description
        assert len(item.applies_to) > 0


def test_strobe_items_cover_22_base():
    """Should have at least 22 unique items (some are design-specific)."""
    ids = set(i.item_id for i in STROBE_ITEMS)
    assert len(ids) >= 22


def _add_completed_result(study_id, test_name="chi_square", extra=True):
    """Insert a completed analysis result for the test study."""
    conn = get_connection(study_id)
    init_db(conn)
    from datetime import datetime, timezone
    import json
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, statistic, p_value, status_json,
            sample_counts_json, is_pre_registered, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (study_id, test_name, 5.5, 0.24,
         json.dumps({"status": "completed", "reason": None}),
         json.dumps({"n_total": 100, "n_analyzed": 95, "n_excluded": 5}),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def test_items_18_22_satisfied_with_analyses_no_draft():
    """Items 18-22 should be satisfied when analyses exist, even without writing draft."""
    plan = StudyPlan(
        study_id=STUDY_ID, study_type="cohort",
        primary_comparison="survival by treatment arm",
        planned_tests=[{"variable_name": "response", "test_name": "chi_square"}],
    )
    lock_plan(STUDY_ID, plan)
    _add_completed_result(STUDY_ID)

    items = check_study(STUDY_ID)

    for item in items:
        if item.item_id in ("18", "19", "20", "21", "22"):
            assert item.satisfied, (
                f"Item {item.item_id} should be satisfied: {item.evidence}"
            )


def test_strobe_order_independence():
    """Strobe-check must produce consistent items 18-22 regardless of draft-file existence.

    The live draft is generated in-memory from study data, so calling
    check_study() before and after a write_draft() must report the same
    satisfied/status for the draft-dependent items.
    """
    plan = StudyPlan(
        study_id=STUDY_ID, study_type="cohort",
        primary_comparison="survival by treatment arm",
        planned_tests=[{"variable_name": "response", "test_name": "chi_square"}],
    )
    lock_plan(STUDY_ID, plan)
    _add_completed_result(STUDY_ID)

    # Strobe check when no draft file exists on disk → uses live in-memory draft
    items_no_draft = check_study(STUDY_ID)

    # Write draft to disk
    from core.reporting.manuscript_draft import write_draft
    write_draft(STUDY_ID)
    assert (DATA_ROOT / STUDY_ID / "manuscript_draft.md").exists()

    # Second strobe check (draft file now exists)
    items_with_draft = check_study(STUDY_ID)

    # Items 18-22 must be identical — the live draft always wins
    for item_no, item_with in zip(items_no_draft, items_with_draft):
        if item_no.item_id in ("18", "19", "20", "21", "22"):
            assert item_no.satisfied == item_with.satisfied, (
                f"Item {item_no.item_id}: satisfied differs "
                f"(no_draft={item_no.satisfied}, with_draft={item_with.satisfied})"
            )
            assert item_no.status == item_with.status, (
                f"Item {item_no.item_id}: status differs "
                f"(no_draft={item_no.status}, with_draft={item_with.status})"
            )


def test_item_22_non_bracketed_funding():
    """Item 22 should be satisfied because funding text is not in brackets."""
    plan = StudyPlan(study_id=STUDY_ID, study_type="cohort",
                     primary_comparison="test")
    lock_plan(STUDY_ID, plan)
    _add_completed_result(STUDY_ID)

    items = check_study(STUDY_ID)
    item22 = next(i for i in items if i.item_id == "22")
    assert item22.satisfied, f"Item 22 should be satisfied: {item22.evidence}"
