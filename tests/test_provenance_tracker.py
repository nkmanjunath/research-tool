"""Tests for provenance tracking."""

import shutil
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.provenance.tracker import ProvenanceTracker, ProvenanceEntry

STUDY_ID = "test_provenance"


@pytest.fixture(autouse=True)
def _setup():
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir) VALUES (?, ?, ?, ?)",
        (STUDY_ID, "Provenance Test", "2025-01-01T00:00:00",
         str(Path("data/studies") / STUDY_ID)),
    )
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT, response TEXT)")
    conn.execute(f"INSERT INTO {raw} (age, response) VALUES ('65', 'CR')")
    conn.execute(f"INSERT INTO {raw} (age, response) VALUES ('70', 'PR')")
    conn.execute("DELETE FROM variables WHERE study_id=?", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'age', 'baseline', 'continuous')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'response', 'outcome', 'categorical')", (STUDY_ID,))
    conn.commit()
    conn.close()
    yield
    p = DATA_ROOT / STUDY_ID
    if p.exists():
        shutil.rmtree(p)


def test_record_and_load():
    tracker = ProvenanceTracker(STUDY_ID)
    tracker.record_run(
        function_name="chi_square",
        parameters={"group_col": "treatment_arm", "outcome_col": "response"},
        source_row_ids=[1, 2, 3, 4, 5],
        column_names=["treatment_arm", "response"],
        test_name="chi_square",
        result_id="result_001",
        is_pre_registered=True,
    )
    assert len(tracker.get_all()) == 1

    # Re-load from disk
    tracker2 = ProvenanceTracker(STUDY_ID)
    assert len(tracker2.get_all()) == 1


def test_get_lineage():
    tracker = ProvenanceTracker(STUDY_ID)
    tracker.record_run(
        function_name="chi_square",
        parameters={},
        source_row_ids=[1, 2, 3],
        column_names=["x", "y"],
        test_name="chi_square",
        result_id="r1",
    )
    tracker.record_run(
        function_name="t_test",
        parameters={},
        source_row_ids=[4, 5, 6],
        column_names=["a", "b"],
        test_name="t_test",
        result_id="r2",
    )
    lines = tracker.get_lineage(test_name="chi_square")
    assert len(lines) == 1
    assert lines[0].test_name == "chi_square"


def test_provenance_entry_timestamp():
    import datetime
    e = ProvenanceEntry(
        function_name="test",
        parameters={},
        source_row_ids=[1],
        column_names=["x"],
        test_name="test",
        result_id="r1",
        study_id=STUDY_ID,
    )
    assert e.computed_at is not None


def test_pre_registered_flag():
    tracker = ProvenanceTracker(STUDY_ID)
    tracker.record_run(
        function_name="t_test",
        parameters={},
        source_row_ids=[],
        column_names=[],
        test_name="t_test",
        result_id="r_post_hoc",
        is_pre_registered=False,
    )
    entries = tracker.get_all()
    assert entries[0].is_pre_registered is False


def test_empty_tracker():
    tracker = ProvenanceTracker("nonexistent_study")
    assert tracker.get_all() == []
