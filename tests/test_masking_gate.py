"""Adversarial tests for the masking gate.

The gate MUST prevent outcome data access at the physical storage level.

Tests:
  1. Raw sqlite3 SELECT on outcome columns pre-classify → visible (not yet sealed)
  2. After classify + seal → outcome columns are NULL in the raw table
  3. After lock → still NULL
  4. After unmask → outcome data restored
  5. Direct sqlite3 CLI attack → still NULL (storage-level, not proxy-level)
  6. No outcome columns → no interference
"""

from __future__ import annotations
import json
import sqlite3
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.masking.gate import seal_outcomes, unmask_study, lock_study, is_masked
from core.ingestion.variable_classifier import _classify_batch

TEST_STUDY_ID = "test_masking_001"
RAW_TABLE = f"raw_{TEST_STUDY_ID}"
MASKED_TABLE = f"raw_masked_{TEST_STUDY_ID}"


@pytest.fixture(autouse=True)
def _setup():
    conn = get_connection(TEST_STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, is_locked) VALUES (?, ?, ?, ?, ?)",
        (TEST_STUDY_ID, "Test Study", "2025-01-01T00:00:00",
         str(Path("data/studies") / TEST_STUDY_ID), 0),
    )
    # Create raw table with outcome columns that have actual values
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
            "row_id" INTEGER PRIMARY KEY,
            "age" TEXT,
            "response" TEXT,
            "pfs_days" TEXT
        )
    """)
    conn.execute(f"DELETE FROM {RAW_TABLE}")
    for i in range(3):
        conn.execute(
            f"INSERT INTO {RAW_TABLE} (age, response, pfs_days) VALUES (?, ?, ?)",
            (str(65 + i), "CR" if i == 0 else "PR", str(300 + i)),
        )
    conn.commit()
    conn.close()
    yield
    import shutil
    p = DATA_ROOT / TEST_STUDY_ID
    if p.exists():
        shutil.rmtree(p)


def _classify(study_id, variables):
    """Helper to classify variables and seal outcomes."""
    _classify_batch(study_id, variables)
    seal_outcomes(study_id)


def test_raw_outcomes_visible_before_seal():
    """Before sealing, outcome values are visible in the raw table."""
    # Classify but don't seal yet
    _classify_batch(TEST_STUDY_ID, [
        {"column": "age", "role": "baseline", "data_type": "continuous"},
        {"column": "response", "role": "outcome", "data_type": "categorical"},
        {"column": "pfs_days", "role": "outcome", "data_type": "continuous"},
    ])
    conn = get_connection(TEST_STUDY_ID)
    cur = conn.execute(f"SELECT response, pfs_days FROM {RAW_TABLE}")
    rows = cur.fetchall()
    conn.close()
    for r in rows:
        assert r["response"] is not None, "pre-seal outcomes should be visible"
        assert r["pfs_days"] is not None


def test_outcomes_nulled_after_seal():
    """After seal_outcomes(), outcome values are physically NULL in the raw table."""
    _classify_batch(TEST_STUDY_ID, [
        {"column": "age", "role": "baseline", "data_type": "continuous"},
        {"column": "response", "role": "outcome", "data_type": "categorical"},
        {"column": "pfs_days", "role": "outcome", "data_type": "continuous"},
    ])
    seal_outcomes(TEST_STUDY_ID)

    # Direct conn — no proxy
    conn = get_connection(TEST_STUDY_ID)
    cur = conn.execute(f"SELECT age, response, pfs_days FROM {RAW_TABLE}")
    rows = cur.fetchall()
    conn.close()
    for r in rows:
        assert r["age"] is not None, "baseline should be visible"
        assert r["response"] is None, f"outcome should be NULL after seal, got {r['response']}"
        assert r["pfs_days"] is None


def test_sqlite3_cli_bypass_still_null():
    """Even direct sqlite3 CLI (simulated via sqlite3 module) sees NULLs after seal."""
    _classify_batch(TEST_STUDY_ID, [
        {"column": "age", "role": "baseline", "data_type": "continuous"},
        {"column": "response", "role": "outcome", "data_type": "categorical"},
        {"column": "pfs_days", "role": "outcome", "data_type": "continuous"},
    ])
    seal_outcomes(TEST_STUDY_ID)

    # Connect with raw sqlite3, not through our code
    db_path = DATA_ROOT / TEST_STUDY_ID / "study.db"
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.row_factory = sqlite3.Row
    cur = raw_conn.execute(f"SELECT response, pfs_days FROM {RAW_TABLE}")
    rows = cur.fetchall()
    raw_conn.close()
    for r in rows:
        assert r["response"] is None, "direct sqlite3 should also see NULL"
        assert r["pfs_days"] is None


def test_lock_still_null():
    """After lock_study(), outcomes remain NULL."""
    _classify(TEST_STUDY_ID, [
        {"column": "age", "role": "baseline", "data_type": "continuous"},
        {"column": "response", "role": "outcome", "data_type": "categorical"},
        {"column": "pfs_days", "role": "outcome", "data_type": "continuous"},
    ])
    lock_study(TEST_STUDY_ID)
    conn = get_connection(TEST_STUDY_ID)
    cur = conn.execute(f"SELECT response FROM {RAW_TABLE}")
    rows = cur.fetchall()
    conn.close()
    for r in rows:
        assert r["response"] is None


def test_unmask_restores_values():
    """After unmask_study(), outcome values are restored in the raw table."""
    _classify(TEST_STUDY_ID, [
        {"column": "age", "role": "baseline", "data_type": "continuous"},
        {"column": "response", "role": "outcome", "data_type": "categorical"},
        {"column": "pfs_days", "role": "outcome", "data_type": "continuous"},
    ])
    unmask_study(TEST_STUDY_ID)
    conn = get_connection(TEST_STUDY_ID)
    cur = conn.execute(f"SELECT age, response, pfs_days FROM {RAW_TABLE} ORDER BY row_id")
    rows = cur.fetchall()
    conn.close()
    assert rows[0]["response"] == "CR"
    assert rows[1]["response"] == "PR"
    assert rows[0]["pfs_days"] == "300"


def test_is_masked_after_seal():
    """is_masked() returns True after sealing, False after unmask."""
    _classify(TEST_STUDY_ID, [
        {"column": "age", "role": "baseline", "data_type": "continuous"},
        {"column": "response", "role": "outcome", "data_type": "categorical"},
        {"column": "pfs_days", "role": "outcome", "data_type": "continuous"},
    ])
    assert is_masked(TEST_STUDY_ID) is True
    unmask_study(TEST_STUDY_ID)
    assert is_masked(TEST_STUDY_ID) is False


def test_no_outcome_variables_no_effect():
    """If no outcome columns exist, sealing does nothing."""
    _classify_batch(TEST_STUDY_ID, [
        {"column": "age", "role": "baseline", "data_type": "continuous"},
    ])
    seal_outcomes(TEST_STUDY_ID)
    conn = get_connection(TEST_STUDY_ID)
    cur = conn.execute(f"SELECT age FROM {RAW_TABLE}")
    row = cur.fetchone()
    conn.close()
    assert row["age"] is not None


def test_shadow_table_contains_values():
    """The masked shadow table should have the original values."""
    _classify(TEST_STUDY_ID, [
        {"column": "age", "role": "baseline", "data_type": "continuous"},
        {"column": "response", "role": "outcome", "data_type": "categorical"},
    ])
    conn = get_connection(TEST_STUDY_ID)
    cur = conn.execute(f"SELECT response FROM {MASKED_TABLE} ORDER BY row_id")
    rows = cur.fetchall()
    conn.close()
    assert rows[0]["response"] == "CR"
    assert rows[1]["response"] == "PR"
