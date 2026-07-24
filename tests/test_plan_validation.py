"""Tests for plan-time variable validation in cmd_plan()."""

import argparse
import shutil

import pytest

from core.cli.main import cmd_plan
from core.database import get_connection, init_db, DATA_ROOT


STUDY_ID = "test_plan_val"


@pytest.fixture(autouse=True)
def _setup():
    if (DATA_ROOT / STUDY_ID).exists():
        shutil.rmtree(DATA_ROOT / STUDY_ID)
    (DATA_ROOT / STUDY_ID).mkdir(parents=True, exist_ok=True)
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, study_type) VALUES (?, ?, ?, ?, ?)",
        (STUDY_ID, "Plan Validation", "2025-01-01", str(DATA_ROOT / STUDY_ID), "cohort"),
    )
    conn.commit()
    conn.close()
    yield
    p = DATA_ROOT / STUDY_ID
    if p.exists():
        shutil.rmtree(p)


def _plan_args(outcome_var_ids="1", test=("age:t_test:",), covariates="", study_type="cohort"):
    """Helper to build an argparse Namespace as cmd_plan expects."""
    return argparse.Namespace(
        study_id=STUDY_ID,
        outcome_var_ids=outcome_var_ids,
        tests=test,
        covariates=covariates,
        study_type=study_type,
        comparison="Test comparison",
    )


def test_plan_rejects_no_variables():
    """No classified variables → error, no provisional plan saved."""
    prov_path = DATA_ROOT / STUDY_ID / "study_plan.provisional.json"
    with pytest.raises(SystemExit):
        cmd_plan(_plan_args())
    assert not prov_path.exists()


def test_plan_rejects_unknown_outcome_id(capsys):
    """outcome-var-ids references a non-existent variable ID."""
    # Insert a variable with id=1 only
    conn = get_connection(STUDY_ID)
    conn.execute(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) "
        "VALUES (1, ?, 'age', 'baseline', 'continuous') ON CONFLICT(id) DO UPDATE SET role='baseline'",
        (STUDY_ID,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        cmd_plan(_plan_args(outcome_var_ids="2"))  # id=2 doesn't exist

    stderr = capsys.readouterr().err
    assert "not found" in stderr
    assert "2" in stderr


def test_plan_rejects_baseline_as_outcome(capsys):
    """outcome-var-ids references a baseline variable → error."""
    conn = get_connection(STUDY_ID)
    conn.execute(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) "
        "VALUES (1, ?, 'age', 'baseline', 'continuous') ON CONFLICT(id) DO UPDATE SET role='baseline'",
        (STUDY_ID,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        cmd_plan(_plan_args(outcome_var_ids="1"))

    stderr = capsys.readouterr().err
    assert "baseline" in stderr
    assert "not 'outcome'" in stderr or "not outcome" in stderr


def test_plan_rejects_unknown_covariate_id(capsys):
    """covariates references a non-existent variable ID."""
    conn = get_connection(STUDY_ID)
    conn.execute(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) "
        "VALUES (1, ?, 'response', 'outcome', 'categorical') ON CONFLICT(id) DO UPDATE SET role='outcome'",
        (STUDY_ID,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        cmd_plan(_plan_args(covariates="99"))

    stderr = capsys.readouterr().err
    assert "99" in stderr
    assert "not found" in stderr


def test_plan_accepts_valid_variables(capsys):
    """Valid outcome and covariate IDs → plan proceeds normally."""
    conn = get_connection(STUDY_ID)
    conn.execute(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) "
        "VALUES (1, ?, 'response', 'outcome', 'categorical') ON CONFLICT(id) DO UPDATE SET role='outcome'",
        (STUDY_ID,),
    )
    conn.execute(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) "
        "VALUES (2, ?, 'age', 'baseline', 'continuous') ON CONFLICT(id) DO UPDATE SET role='baseline'",
        (STUDY_ID,),
    )
    conn.commit()
    conn.close()

    # Should NOT exit
    cmd_plan(_plan_args(outcome_var_ids="1", covariates="2"))
    prov_path = DATA_ROOT / STUDY_ID / "study_plan.provisional.json"
    assert prov_path.exists()
