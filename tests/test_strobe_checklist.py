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
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT, response TEXT)")
    conn.execute(f"INSERT INTO {raw} (age, response) VALUES ('65', 'CR')")
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
