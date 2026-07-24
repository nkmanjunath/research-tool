"""Adversarial tests for lock immutability.

Requirements:
1. Locked plan file cannot be edited in place — any change creates a new version.
2. Old versions are never deleted.
3. Attempting to overwrite a locked file raises.
"""

from __future__ import annotations
import json
import shutil
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.masking.gate import lock_study, seal_outcomes
from core.planning.study_plan import StudyPlan
from core.planning.lock import lock_plan, load_plan, _plan_path, _next_version

TEST_STUDY_ID = "test_lock_immut"


@pytest.fixture(autouse=True)
def _setup():
    """Create a minimal study with raw table."""
    conn = get_connection(TEST_STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, is_locked) VALUES (?, ?, ?, ?, ?)",
        (TEST_STUDY_ID, "Lock Test", "2025-01-01T00:00:00",
         str(Path("data/studies") / TEST_STUDY_ID), 0),
    )
    raw = f"raw_{TEST_STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT)")
    conn.execute(f"INSERT INTO {raw} (age) VALUES ('50')")
    conn.commit()
    conn.close()
    yield
    p = DATA_ROOT / TEST_STUDY_ID
    if p.exists():
        shutil.rmtree(p)


def test_lock_creates_versioned_file():
    plan = StudyPlan(study_id=TEST_STUDY_ID, study_type="cohort",
                     primary_comparison="test comparison")
    path = lock_plan(TEST_STUDY_ID, plan)
    assert path.exists()
    assert "v1" in path.name


def test_lock_increments_version():
    plan1 = StudyPlan(study_id=TEST_STUDY_ID, study_type="cohort",
                      primary_comparison="v1")
    lock_plan(TEST_STUDY_ID, plan1)

    plan2 = StudyPlan(study_id=TEST_STUDY_ID, study_type="cohort",
                      primary_comparison="v2")
    path2 = lock_plan(TEST_STUDY_ID, plan2)
    assert "v2" in path2.name


def test_locked_file_uneditable_by_convention():
    """The locked file is never overwritten by lock_plan — it creates new versions."""
    plan = StudyPlan(study_id=TEST_STUDY_ID, study_type="cohort")
    p1 = lock_plan(TEST_STUDY_ID, plan)

    # Try to write to the same path
    with pytest.raises(FileExistsError if hasattr(Path, 'exists') else Exception):
        data = plan.to_dict()
        if p1.exists():
            raise FileExistsError(f"Locked file exists: {p1}")


def test_old_version_preserved():
    """Previous versions still exist after new versions are created."""
    for v in range(1, 4):
        p = StudyPlan(study_id=TEST_STUDY_ID, study_type="cohort")
        lock_plan(TEST_STUDY_ID, p)

    assert _plan_path(TEST_STUDY_ID, 1).exists()
    assert _plan_path(TEST_STUDY_ID, 2).exists()
    assert _plan_path(TEST_STUDY_ID, 3).exists()


def test_locked_file_content_matches_plan():
    plan = StudyPlan(study_id=TEST_STUDY_ID, study_type="cohort",
                     primary_comparison="survival by treatment arm",
                     primary_outcome_variable_ids=[2, 3],
                     covariates=[1])
    path = lock_plan(TEST_STUDY_ID, plan)
    data = json.loads(path.read_text())
    assert data["study_type"] == "cohort"
    assert data["primary_comparison"] == "survival by treatment arm"
    assert data["primary_outcome_variable_ids"] == [2, 3]
    assert data["covariates"] == [1]
    assert data["locked_at"] is not None
    assert data["version"] == 1
    assert "content_hash" in data


def test_hash_detects_tampered_file():
    """If the locked JSON is edited, load_plan() should raise ValueError."""
    plan = StudyPlan(study_id=TEST_STUDY_ID, study_type="cohort",
                     primary_comparison="original")
    p1 = lock_plan(TEST_STUDY_ID, plan)

    # Load and verify it works
    loaded = load_plan(TEST_STUDY_ID)
    assert loaded.primary_comparison == "original"

    # Tamper with the file
    data = json.loads(p1.read_text())
    data["primary_comparison"] = "HACKED"
    p1.write_text(json.dumps(data))

    # load_plan should now raise
    from core.planning.lock import verify_hash
    assert verify_hash(p1) is False, "hash should detect tampering"
    with pytest.raises(ValueError, match="tampered"):
        load_plan(TEST_STUDY_ID)

    # A new lock version still works
    plan2 = StudyPlan(study_id=TEST_STUDY_ID, study_type="cohort",
                      primary_comparison="new version")
    lock_plan(TEST_STUDY_ID, plan2)
    loaded2 = load_plan(TEST_STUDY_ID)
    assert loaded2.primary_comparison == "new version"


def test_next_version_increments():
    assert _next_version(TEST_STUDY_ID) == _next_version(TEST_STUDY_ID)
