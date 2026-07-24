"""Tests for descriptive statistics (Table 1)."""

from __future__ import annotations
import shutil
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.stats.descriptive import generate_table1

STUDY_ID = "test_descriptive"


@pytest.fixture(autouse=True)
def _setup():
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir) VALUES (?, ?, ?, ?)",
        (STUDY_ID, "Descriptive Test", "2025-01-01T00:00:00",
         str(Path("data/studies") / STUDY_ID)),
    )
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT, sex TEXT, iss_stage TEXT)")
    for i in range(10):
        conn.execute(f"INSERT INTO {raw} (age, sex, iss_stage) VALUES (?, ?, ?)",
                     (str(50 + i * 3), ["M", "F"][i % 2], ["I", "II", "III"][i % 3]))
    conn.execute("DELETE FROM variables WHERE study_id=?", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'age', 'baseline', 'continuous')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'sex', 'baseline', 'categorical')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'iss_stage', 'baseline', 'categorical')", (STUDY_ID,))
    conn.commit()
    conn.close()
    yield
    p = DATA_ROOT / STUDY_ID
    if p.exists():
        shutil.rmtree(p)


def test_table1_returns_dataframe():
    tbl = generate_table1(STUDY_ID)
    assert tbl is not None
    # Should have rows for each variable
    assert len(tbl) > 0


def test_table1_includes_age():
    tbl = generate_table1(STUDY_ID)
    # tableone labels rows with variable names
    age_rows = tbl.filter(like="age", axis=0)
    assert len(age_rows) > 0


def test_table1_grouped():
    tbl = generate_table1(STUDY_ID, groupby="sex")
    assert tbl is not None
