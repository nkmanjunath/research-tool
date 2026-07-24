"""Tests for CSV ingestion — whitespace stripping and duplicate detection."""

from __future__ import annotations

import csv
import io
import shutil

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.ingestion.csv_loader import load_file


STUDY_ID = "test_csv_loader"


@pytest.fixture(autouse=True)
def _setup():
    p = DATA_ROOT / STUDY_ID
    if p.exists():
        shutil.rmtree(p)
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir) VALUES (?, ?, ?, ?)",
        (STUDY_ID, "CSV Loader Test", "2025-01-01", str(DATA_ROOT / STUDY_ID)),
    )
    conn.commit()
    conn.close()
    yield
    if p.exists():
        shutil.rmtree(p)


def _csv_content(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerows(rows)
    return buf.getvalue()


def test_whitespace_stripped_from_all_string_columns():
    """Leading/trailing whitespace in string values should be stripped during ingest."""
    content = _csv_content([
        ["patient_id", "sex", "treatment_arm"],
        ["001", " M", "A"],          # leading space
        ["002", "F ", "B"],          # trailing space
        ["003", " M ", " B "],       # both sides
    ])
    tmp = DATA_ROOT / STUDY_ID / "_test.csv"
    tmp.write_text(content)
    load_file(STUDY_ID, str(tmp))
    tmp.unlink()

    conn = get_connection(STUDY_ID)
    cur = conn.execute("SELECT sex FROM raw_test_csv_loader ORDER BY row_id")
    sexes = [r["sex"] for r in cur.fetchall()]
    conn.close()

    # All whitespace should be stripped
    assert sexes == ["M", "F", "M"]


def test_duplicate_patient_id_warns(capsys):
    """Duplicate patient_id values should produce a warning on stderr."""
    content = _csv_content([
        ["patient_id", "age"],
        ["P001", "45"],
        ["P002", "50"],
        ["P001", "55"],  # duplicate
    ])
    tmp = DATA_ROOT / STUDY_ID / "_test.csv"
    tmp.write_text(content)
    load_file(STUDY_ID, str(tmp))
    tmp.unlink()

    stderr = capsys.readouterr().err
    assert "Warning: duplicate patient identifiers" in stderr
    assert "'P001'" in stderr


def test_duplicate_patient_id_no_warning_when_unique(capsys):
    """No duplicate warning when all patient_ids are unique."""
    content = _csv_content([
        ["patient_id", "age"],
        ["P001", "45"],
        ["P002", "50"],
        ["P003", "55"],
    ])
    tmp = DATA_ROOT / STUDY_ID / "_test.csv"
    tmp.write_text(content)
    load_file(STUDY_ID, str(tmp))
    tmp.unlink()

    stderr = capsys.readouterr().err
    assert "Warning: duplicate patient identifiers" not in stderr


def test_no_patient_id_column_no_crash():
    """Files without a patient_id column should ingest normally."""
    content = _csv_content([
        ["age", "sex"],
        ["45", "M"],
        ["50", "F"],
    ])
    tmp = DATA_ROOT / STUDY_ID / "_test.csv"
    tmp.write_text(content)
    cols = load_file(STUDY_ID, str(tmp))
    tmp.unlink()
    assert cols == ["age", "sex"]


def test_whitespace_only_values_become_null():
    """Values that are only whitespace should become NULL, not empty strings."""
    content = _csv_content([
        ["patient_id", "age"],
        ["001", "   "],
        ["002", ""],
    ])
    tmp = DATA_ROOT / STUDY_ID / "_test.csv"
    tmp.write_text(content)
    load_file(STUDY_ID, str(tmp))
    tmp.unlink()

    conn = get_connection(STUDY_ID)
    ages = [r["age"] for r in conn.execute("SELECT age FROM raw_test_csv_loader ORDER BY row_id").fetchall()]
    conn.close()
    assert ages == [None, None]


def test_find_duplicate_patient_ids_detects_dupes():
    """find_duplicate_patient_ids() should detect ingested duplicates."""
    content = _csv_content([
        ["patient_id", "age"],
        ["P001", "45"],
        ["P002", "50"],
        ["P001", "55"],
    ])
    tmp = DATA_ROOT / STUDY_ID / "_test.csv"
    tmp.write_text(content)
    load_file(STUDY_ID, str(tmp))
    tmp.unlink()

    from core.ingestion.csv_loader import find_duplicate_patient_ids
    dupes = find_duplicate_patient_ids(STUDY_ID)
    assert len(dupes) == 1
    assert dupes[0][0] == "P001"
    assert dupes[0][1] == 2


def test_find_duplicate_patient_ids_no_dupes():
    """find_duplicate_patient_ids() returns empty when no duplicates."""
    content = _csv_content([
        ["patient_id", "age"],
        ["P001", "45"],
        ["P002", "50"],
    ])
    tmp = DATA_ROOT / STUDY_ID / "_test.csv"
    tmp.write_text(content)
    load_file(STUDY_ID, str(tmp))
    tmp.unlink()

    from core.ingestion.csv_loader import find_duplicate_patient_ids
    assert find_duplicate_patient_ids(STUDY_ID) == []


def test_na_values_converts_unknown_to_missing():
    """--na-values 'unknown' should make 'unknown' register as missing."""
    content = _csv_content([
        ["patient_id", "treatment_arm"],
        ["P001", "A"],
        ["P002", "unknown"],
        ["P003", "B"],
    ])
    tmp = DATA_ROOT / STUDY_ID / "_test.csv"
    tmp.write_text(content)
    load_file(STUDY_ID, str(tmp), na_values=["unknown"])
    tmp.unlink()

    conn = get_connection(STUDY_ID)
    arms = [r["treatment_arm"] for r in conn.execute("SELECT treatment_arm FROM raw_test_csv_loader ORDER BY row_id").fetchall()]
    conn.close()
    # 'unknown' should be None (missing), not the string 'unknown'
    assert arms == ["A", None, "B"]


def test_na_values_multiple_sentinels():
    """--na-values 'unknown,missing' should convert both to missing."""
    content = _csv_content([
        ["patient_id", "treatment_arm"],
        ["P001", "A"],
        ["P002", "unknown"],
        ["P003", "missing"],
    ])
    tmp = DATA_ROOT / STUDY_ID / "_test.csv"
    tmp.write_text(content)
    load_file(STUDY_ID, str(tmp), na_values=["unknown", "missing"])
    tmp.unlink()

    conn = get_connection(STUDY_ID)
    arms = [r["treatment_arm"] for r in conn.execute("SELECT treatment_arm FROM raw_test_csv_loader ORDER BY row_id").fetchall()]
    conn.close()
    assert arms == ["A", None, None]


def test_na_values_default_behavior_unchanged():
    """Without --na-values, 'unknown' remains a literal category."""
    content = _csv_content([
        ["patient_id", "treatment_arm"],
        ["P001", "A"],
        ["P002", "unknown"],
        ["P003", "B"],
    ])
    tmp = DATA_ROOT / STUDY_ID / "_test.csv"
    tmp.write_text(content)
    load_file(STUDY_ID, str(tmp))
    tmp.unlink()

    conn = get_connection(STUDY_ID)
    arms = [r["treatment_arm"] for r in conn.execute("SELECT treatment_arm FROM raw_test_csv_loader ORDER BY row_id").fetchall()]
    conn.close()
    # 'unknown' should remain as the string 'unknown'
    assert arms == ["A", "unknown", "B"]
