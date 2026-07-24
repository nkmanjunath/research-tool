"""Tests for variable classification."""

from core.ingestion.variable_classifier import classify_variables_interactive


def test_classify_age_continuous():
    """Age should be classified as continuous baseline."""
    result = classify_variables_interactive("test", ["age"])
    assert result[0]["role"] == "baseline"
    assert result[0]["data_type"] == "continuous"


def test_classify_outcome_response():
    """Response should be classified as categorical outcome."""
    result = classify_variables_interactive("test", ["response"])
    assert result[0]["role"] == "outcome"
    assert result[0]["data_type"] == "categorical"


def test_classify_pfs_time_to_event():
    """pfs_days should be classified as time_to_event outcome."""
    result = classify_variables_interactive("test", ["pfs_days"])
    assert result[0]["role"] == "outcome"
    assert result[0]["data_type"] == "time_to_event"


def test_classify_sex_categorical():
    """Sex should be classified as categorical baseline."""
    result = classify_variables_interactive("test", ["sex"])
    assert result[0]["role"] == "baseline"
    assert result[0]["data_type"] == "categorical"


def test_classify_iss_stage_categorical():
    """ISS stage should be categorical baseline."""
    result = classify_variables_interactive("test", ["iss_stage"])
    assert result[0]["role"] == "baseline"
    assert result[0]["data_type"] == "categorical"


def test_multiple_columns_all_classified():
    """All columns in a list should get a classification."""
    cols = ["age", "sex", "response", "pfs_days", "os_days", "os_event"]
    result = classify_variables_interactive("test", cols)
    assert len(result) == len(cols)
    names = [r["column"] for r in result]
    assert names == cols


def test_patient_id_skipped():
    """Identifier columns (id, patient, name) should be skipped."""
    result = classify_variables_interactive("test", ["patient_id", "age"])
    assert result[0]["column"] == "age"  # patient_id was skipped


def test_prior_lines_continuous():
    """prior_lines should be classified as continuous baseline (count data)."""
    result = classify_variables_interactive("test", ["prior_lines"])
    assert result[0]["role"] == "baseline"
    assert result[0]["data_type"] == "continuous"


def test_float_column_continuous():
    """Float column with many distinct values → continuous regardless of name."""
    import shutil, tempfile, csv
    from core.database import get_connection, init_db, DATA_ROOT
    from core.ingestion.variable_classifier import classify_variables_interactive

    sid = "test_float_col"
    p = DATA_ROOT / sid
    if p.exists():
        shutil.rmtree(p)

    conn = get_connection(sid)
    init_db(conn)
    conn.execute("INSERT OR REPLACE INTO studies (id, name, created_at, data_dir) VALUES (?, ?, ?, ?)",
                 (sid, "FloatCol", "2025-01-01", str(p)))
    raw = f"raw_{sid}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, followup_m_protein TEXT)")
    for i in range(20):
        conn.execute(f"INSERT INTO {raw} (followup_m_protein) VALUES (?)", (str(i * 0.5 + 0.1),))
    conn.commit()
    conn.close()

    result = classify_variables_interactive(sid, ["followup_m_protein"])
    # "followup" is in the outcome keyword set, so role should be outcome
    assert result[0]["role"] == "outcome"
    assert result[0]["data_type"] == "continuous"

    shutil.rmtree(p)


def test_low_cardinality_categorical():
    """Column with 3 distinct string values → categorical."""
    import shutil
    from core.database import get_connection, init_db, DATA_ROOT

    sid = "test_low_card"
    p = DATA_ROOT / sid
    if p.exists():
        shutil.rmtree(p)

    conn = get_connection(sid)
    init_db(conn)
    conn.execute("INSERT OR REPLACE INTO studies (id, name, created_at, data_dir) VALUES (?, ?, ?, ?)",
                 (sid, "LowCard", "2025-01-01", str(p)))
    raw = f"raw_{sid}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, grade TEXT)")
    for i in range(20):
        conn.execute(f"INSERT INTO {raw} (grade) VALUES (?)",
                     (["mild", "moderate", "severe"][i % 3],))
    conn.commit()
    conn.close()

    result = classify_variables_interactive(sid, ["grade"])
    assert result[0]["data_type"] == "categorical"

    shutil.rmtree(p)


def test_unknown_name_flagged_unclassified():
    """Column with unrecognized column name → unclassified."""
    result = classify_variables_interactive("test", ["xyz_something_random"])
    assert result[0]["role"] == "unclassified"
