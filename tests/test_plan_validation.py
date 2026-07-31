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


def _plan_args(outcome_var_ids="1", test=("age:t_test:",), covariates="",
               study_type="cohort", matching_criteria="", comparison="Test comparison"):
    """Helper to build an argparse Namespace as cmd_plan expects."""
    ns = argparse.Namespace(
        study_id=STUDY_ID,
        outcome_var_ids=outcome_var_ids,
        tests=test,
        covariates=covariates,
        study_type=study_type,
        comparison=comparison,
    )
    if matching_criteria:
        ns.matching_criteria = matching_criteria
    return ns


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


def test_matching_criterion_overlap_warns(capsys):
    """Matching on the comparison variable should produce an advisory warning."""
    conn = get_connection(STUDY_ID)
    conn.execute(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) "
        "VALUES (1, ?, 'response', 'outcome', 'categorical') ON CONFLICT(id) DO UPDATE SET role='outcome'",
        (STUDY_ID,),
    )
    conn.execute(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) "
        "VALUES (5, ?, 'treatment_arm', 'baseline', 'categorical') ON CONFLICT(id) DO UPDATE SET role='baseline'",
        (STUDY_ID,),
    )
    conn.commit()
    conn.close()

    cmd_plan(_plan_args(
        outcome_var_ids="1",
        study_type="case_control",
        comparison="Response by treatment arm",
        matching_criteria="5",
    ))

    stderr = capsys.readouterr().err
    assert "Warning: variable 'treatment_arm' is declared as both" in stderr
    assert "Matching on the comparison variable can obscure" in stderr


def test_matching_criterion_no_overlap_no_warning(capsys):
    """Matching on a different variable should not trigger the overlap warning."""
    conn = get_connection(STUDY_ID)
    conn.execute(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) "
        "VALUES (1, ?, 'response', 'outcome', 'categorical') ON CONFLICT(id) DO UPDATE SET role='outcome'",
        (STUDY_ID,),
    )
    conn.execute(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) "
        "VALUES (5, ?, 'treatment_arm', 'baseline', 'categorical') ON CONFLICT(id) DO UPDATE SET role='baseline'",
        (STUDY_ID,),
    )
    conn.execute(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) "
        "VALUES (3, ?, 'age', 'baseline', 'continuous') ON CONFLICT(id) DO UPDATE SET role='baseline'",
        (STUDY_ID,),
    )
    conn.commit()
    conn.close()

    cmd_plan(_plan_args(
        outcome_var_ids="1",
        study_type="case_control",
        comparison="Response by treatment arm",
        matching_criteria="3",  # matching on age, not treatment_arm
    ))

    stderr = capsys.readouterr().err
    assert "declared as both a matching criterion" not in stderr


def _setup_for_lock_test(with_duplicates: bool):
    """Set up a study with variables, a provisional plan, and optionally duplicates."""
    conn = get_connection(STUDY_ID)
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, patient_id TEXT, age TEXT, response TEXT)")
    conn.execute("DELETE FROM variables WHERE study_id=?", (STUDY_ID,))
    conn.execute("INSERT INTO variables (id, study_id, column_name, role, data_type) VALUES (1, ?, 'response', 'outcome', 'categorical') ON CONFLICT(id) DO UPDATE SET role='outcome'",
                 (STUDY_ID,))
    conn.execute("INSERT INTO variables (id, study_id, column_name, role, data_type) VALUES (2, ?, 'age', 'baseline', 'continuous') ON CONFLICT(id) DO UPDATE SET role='baseline'",
                 (STUDY_ID,))
    conn.execute(f"DELETE FROM {raw}")
    conn.execute(f"INSERT INTO {raw} (patient_id, age, response) VALUES ('P001', '65', 'CR')")
    conn.execute(f"INSERT INTO {raw} (patient_id, age, response) VALUES ('P002', '70', 'PR')")
    if with_duplicates:
        conn.execute(f"INSERT INTO {raw} (patient_id, age, response) VALUES ('P001', '55', 'SD')")
    conn.commit()
    conn.close()

    # Seal outcomes so lock_plan's is_masked check passes
    from core.masking.gate import seal_outcomes
    seal_outcomes(STUDY_ID)

    # Write a provisional plan
    import json
    plan = {
        "study_id": STUDY_ID, "version": 1,
        "study_type": "cohort", "primary_comparison": "test",
        "planned_tests": [{"variable_name": "response", "test_name": "chi_square"}],
        "primary_outcome_variable_ids": [1],
        "covariates": [],
        "matching_criteria": [],
        "warnings": {},
    }
    (DATA_ROOT / STUDY_ID / "study_plan.provisional.json").write_text(json.dumps(plan))


def test_lock_rejects_duplicate_ids():
    """Locking a study with duplicate patient IDs must fail."""
    _setup_for_lock_test(with_duplicates=True)
    with pytest.raises(SystemExit):
        from core.cli.main import cmd_lock
        import argparse
        ns = argparse.Namespace(study_id=STUDY_ID, allow_duplicate_ids=False)
        cmd_lock(ns)


def test_lock_allow_duplicate_ids_override():
    """--allow-duplicate-ids must bypass the duplicate check."""
    _setup_for_lock_test(with_duplicates=True)
    from core.cli.main import cmd_lock
    import argparse
    ns = argparse.Namespace(study_id=STUDY_ID, allow_duplicate_ids=True)
    cmd_lock(ns)
    # Should have produced a locked file
    locked = list(DATA_ROOT.glob(f"{STUDY_ID}/study_plan.v*.locked.json"))
    assert len(locked) >= 1


def test_lock_no_duplicates_normal():
    """Locking a study with no duplicates should proceed normally."""
    _setup_for_lock_test(with_duplicates=False)
    from core.cli.main import cmd_lock
    import argparse
    ns = argparse.Namespace(study_id=STUDY_ID, allow_duplicate_ids=False)
    cmd_lock(ns)
    locked = list(DATA_ROOT.glob(f"{STUDY_ID}/study_plan.v*.locked.json"))
    assert len(locked) >= 1


def test_lock_strict_ids_blocks_duplicates(capsys):
    """--strict-ids must abort with clear error message when duplicates exist."""
    _setup_for_lock_test(with_duplicates=True)
    from core.cli.main import cmd_lock
    import argparse
    ns = argparse.Namespace(study_id=STUDY_ID, strict_ids=True, allow_duplicate_ids=False)
    with pytest.raises(SystemExit):
        cmd_lock(ns)
    err = capsys.readouterr().err
    assert "duplicate patient identifiers found" in err
    assert "P001" in err


def test_lock_strict_ids_passes_when_unique():
    """--strict-ids allows locking when patient IDs are unique."""
    _setup_for_lock_test(with_duplicates=False)
    from core.cli.main import cmd_lock
    import argparse
    ns = argparse.Namespace(study_id=STUDY_ID, strict_ids=True, allow_duplicate_ids=False)
    cmd_lock(ns)
    locked = list(DATA_ROOT.glob(f"{STUDY_ID}/study_plan.v*.locked.json"))
    assert len(locked) >= 1

