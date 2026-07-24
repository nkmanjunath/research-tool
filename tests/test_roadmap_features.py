"""Regression tests for classification overrides, Cox checks, and appendix export."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

from core.cli.main import cmd_export, cmd_plan
from core.database import DATA_ROOT, get_connection, init_db
from core.planning.lock import lock_plan
from core.planning.study_plan import StudyPlan
from core.planning.test_selector import check_assumptions


STUDY_ID = "test_roadmap_features"


@pytest.fixture(autouse=True)
def _setup():
    study_path = DATA_ROOT / STUDY_ID
    if study_path.exists():
        shutil.rmtree(study_path)
    study_path.mkdir(parents=True)
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT INTO studies (id, name, created_at, data_dir, study_type) VALUES (?, ?, ?, ?, ?)",
        (STUDY_ID, "Roadmap", "2025-01-01", str(study_path), "cohort"),
    )
    conn.execute(
        "CREATE TABLE raw_test_roadmap_features "
        "(row_id INTEGER PRIMARY KEY, treatment_arm TEXT, response TEXT, "
        "prior_lines TEXT, pfs_days TEXT, pfs_event TEXT)"
    )
    conn.executemany(
        "INSERT INTO raw_test_roadmap_features "
        "(treatment_arm, response, prior_lines, pfs_days, pfs_event) VALUES (?, ?, ?, ?, ?)",
        [("A", "CR", "2", "10", "1"), ("A", "PR", "3", "20", "1"),
         ("B", "SD", "1", "10", "0"), ("B", "PD", "2", "20", "0")],
    )
    conn.executemany(
        "INSERT INTO variables (id, study_id, column_name, role, data_type) VALUES (?, ?, ?, ?, ?)",
        [(1, STUDY_ID, "response", "outcome", "categorical"),
         (2, STUDY_ID, "prior_lines", "baseline", "continuous"),
         (3, STUDY_ID, "pfs_days", "outcome", "time_to_event"),
         (4, STUDY_ID, "treatment_arm", "baseline", "categorical")],
    )
    conn.commit()
    conn.close()
    yield
    if study_path.exists():
        shutil.rmtree(study_path)


def _plan_args(**kwargs):
    values = dict(
        study_id=STUDY_ID,
        outcome_var_ids="1",
        tests=["response:chi_square:"],
        covariates="",
        study_type="cohort",
        comparison="response by treatment arm",
        overrides=[],
    )
    values.update(kwargs)
    return argparse.Namespace(**values)


def test_plan_persists_valid_role_override():
    cmd_plan(_plan_args(overrides=["id=2:role=covariate"]))

    plan = json.loads((DATA_ROOT / STUDY_ID / "study_plan.provisional.json").read_text())
    assert plan["role_overrides"] == {"2": "covariate"}
    assert plan["audit"]["role_overrides"] == [{"variable_id": 2, "role": "covariate"}]


@pytest.mark.parametrize("override", ["id=99:role=covariate", "id=2:role=banana", "2:role=covariate"])
def test_plan_rejects_invalid_override(override, capsys):
    with pytest.raises(SystemExit):
        cmd_plan(_plan_args(overrides=[override]))
    assert "override" in capsys.readouterr().err.lower()


def test_plan_rejects_override_after_lock(capsys):
    lock_plan(STUDY_ID, StudyPlan(study_id=STUDY_ID, study_type="cohort"))

    with pytest.raises(SystemExit):
        cmd_plan(_plan_args(overrides=["id=2:role=covariate"]))
    assert "locked" in capsys.readouterr().err.lower()


def test_cox_assumption_check_warns_on_marginal_event_rate_difference():
    conn = get_connection(STUDY_ID)
    masked = f"raw_masked_{STUDY_ID}"
    conn.execute(f"CREATE TABLE {masked} (row_id INTEGER PRIMARY KEY, pfs_days TEXT, pfs_event TEXT)")
    conn.execute(
        f"INSERT INTO {masked} SELECT row_id, pfs_days, pfs_event FROM raw_{STUDY_ID}"
    )
    conn.commit()
    conn.close()

    warnings = check_assumptions(
        STUDY_ID,
        [{"variable_name": "pfs_days", "test_name": "cox_proportional_hazards"}],
    )
    assert any("proportional hazards" in warning.lower() for warning in warnings)
    assert all("A" not in warning.split(":")[-1] for warning in warnings)


def test_plan_persists_cox_warning_for_analysis_enforcement():
    conn = get_connection(STUDY_ID)
    masked = f"raw_masked_{STUDY_ID}"
    conn.execute(f"CREATE TABLE {masked} (row_id INTEGER PRIMARY KEY, pfs_days TEXT, pfs_event TEXT)")
    conn.execute(f"INSERT INTO {masked} SELECT row_id, pfs_days, pfs_event FROM raw_{STUDY_ID}")
    conn.commit()
    conn.close()

    cmd_plan(_plan_args(
        outcome_var_ids="3",
        tests=["pfs_days:cox_proportional_hazards:adjusted survival"],
    ))
    plan = json.loads((DATA_ROOT / STUDY_ID / "study_plan.provisional.json").read_text())
    assert "pfs_days" in plan["warnings"]


def test_appendix_export_contains_table1_uro_warnings_and_strobe():
    lock_plan(STUDY_ID, StudyPlan(study_id=STUDY_ID, study_type="cohort"))
    args = argparse.Namespace(
        study_id=STUDY_ID,
        mode="supplementary",
        study_plan_version=None,
        format="appendix",
    )
    cmd_export(args)

    appendix = DATA_ROOT / STUDY_ID / "study_result.v1.appendix.md"
    assert appendix.exists()
    text = appendix.read_text()
    assert "Table 1" in text
    assert "URO" in text
    assert "STROBE" in text
    assert "Warnings" in text
