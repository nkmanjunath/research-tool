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
