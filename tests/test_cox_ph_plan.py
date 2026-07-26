"""Tests for Cox PH plan parsing and validation."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

from core.cli.main import cmd_plan, cmd_analyze, cmd_lock
from core.database import DATA_ROOT, get_connection, init_db
from core.planning.study_plan import StudyPlan
from core.planning.lock import lock_plan
from core.masking.gate import unmask_study

STUDY_ID = "test_cox_plan"
RAW = f"raw_{STUDY_ID}"


def _setup_db(extra_rows: list[tuple] | None = None):
    study_path = DATA_ROOT / STUDY_ID
    if study_path.exists():
        shutil.rmtree(study_path)
    study_path.mkdir(parents=True)
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, study_type) VALUES (?, ?, ?, ?, ?)",
        (STUDY_ID, "Cox PH Plan Test", "2025-01-01", str(study_path), "cohort"),
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {RAW} "
        f"(row_id INTEGER PRIMARY KEY, treatment_arm TEXT, pfs_days TEXT, pfs_event TEXT, age TEXT)"
    )
    conn.executemany(
        f"INSERT INTO {RAW} (treatment_arm, pfs_days, pfs_event, age) VALUES (?, ?, ?, ?)",
        extra_rows or [
            ("A", "100", "1", "65"),
            ("A", "200", "0", "70"),
            ("B", "150", "1", "55"),
            ("B", "300", "0", "60"),
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO variables (id, study_id, column_name, role, data_type) VALUES (?, ?, ?, ?, ?)",
        [
            (1, STUDY_ID, "treatment_arm", "baseline", "categorical"),
            (2, STUDY_ID, "pfs_days", "outcome", "time_to_event"),
            (3, STUDY_ID, "pfs_event", "outcome", "categorical"),
            (4, STUDY_ID, "age", "baseline", "continuous"),
        ],
    )
    conn.commit()
    conn.close()


def _plan_args(cox_ph_models=None, **kwargs):
    values = dict(
        study_id=STUDY_ID,
        outcome_var_ids="3",
        tests=[],
        covariates="",
        study_type="cohort",
        comparison="PFS by treatment arm",
        overrides=[],
        matching_criteria="",
    )
    values["cox_ph_models"] = cox_ph_models or []
    values.update(kwargs)
    return argparse.Namespace(**values)


# --- valid ID/name resolution ---


def test_cox_ph_accepts_valid_column_names(capsys):
    _setup_db()
    cmd_plan(_plan_args(cox_ph_models=["m1:pfs_days:pfs_event:treatment_arm:age:test rationale"]))
    prov = DATA_ROOT / STUDY_ID / "study_plan.provisional.json"
    assert prov.exists()


def test_cox_ph_accepts_4_part_format(capsys):
    _setup_db()
    cmd_plan(_plan_args(cox_ph_models=["m1:pfs_days:pfs_event:treatment_arm"]))
    prov = DATA_ROOT / STUDY_ID / "study_plan.provisional.json"
    assert prov.exists()


def test_cox_ph_accepts_5_part_format_no_rationale(capsys):
    _setup_db()
    cmd_plan(_plan_args(cox_ph_models=["m1:pfs_days:pfs_event:treatment_arm:age"]))
    prov = DATA_ROOT / STUDY_ID / "study_plan.provisional.json"
    assert prov.exists()


# --- invalid column name rejected at plan ---


def test_cox_ph_rejects_unknown_column(capsys):
    _setup_db()
    with pytest.raises(SystemExit):
        cmd_plan(_plan_args(cox_ph_models=["m1:pfs_days:nonexistent_col:treatment_arm"]))
    err = capsys.readouterr().err
    assert "nonexistent_col" in err
    assert "not found" in err


def test_cox_ph_rejects_unknown_covariate(capsys):
    _setup_db()
    with pytest.raises(SystemExit):
        cmd_plan(_plan_args(cox_ph_models=["m1:pfs_days:pfs_event:treatment_arm:fake_cov"]))
    err = capsys.readouterr().err
    assert "fake_cov" in err
    assert "not found" in err


# --- non-binary event_col rejected at plan ---


def test_cox_ph_rejects_non_binary_event(capsys):
    _setup_db(extra_rows=[
        ("A", "100", "maybe", "65"),
        ("B", "150", "yes", "55"),
    ])
    with pytest.raises(SystemExit):
        cmd_plan(_plan_args(cox_ph_models=["m1:pfs_days:pfs_event:treatment_arm"]))
    err = capsys.readouterr().err
    assert "binary" in err.lower()
    assert "maybe" in err or "yes" in err


def test_cox_ph_accepts_binary_event(capsys):
    _setup_db(extra_rows=[
        ("A", "100", "1", "65"),
        ("B", "150", "0", "55"),
    ])
    cmd_plan(_plan_args(cox_ph_models=["m1:pfs_days:pfs_event:treatment_arm"]))
    prov = DATA_ROOT / STUDY_ID / "study_plan.provisional.json"
    assert prov.exists()


# --- negative survival_time rejected at plan ---


def test_cox_ph_rejects_negative_survival_time(capsys):
    _setup_db(extra_rows=[
        ("A", "-5", "1", "65"),
        ("B", "150", "0", "55"),
    ])
    with pytest.raises(SystemExit):
        cmd_plan(_plan_args(cox_ph_models=["m1:pfs_days:pfs_event:treatment_arm"]))
    err = capsys.readouterr().err
    assert "negative" in err.lower()


def test_cox_ph_rejects_non_numeric_survival_time(capsys):
    _setup_db(extra_rows=[
        ("A", "abc", "1", "65"),
        ("B", "150", "0", "55"),
    ])
    with pytest.raises(SystemExit):
        cmd_plan(_plan_args(cox_ph_models=["m1:pfs_days:pfs_event:treatment_arm"]))
    err = capsys.readouterr().err
    assert "not numeric" in err.lower()


# --- short-form 4-part format accepted ---


def test_cox_ph_4_part_no_covariates_no_rationale(capsys):
    _setup_db()
    cmd_plan(_plan_args(cox_ph_models=["m1:pfs_days:pfs_event:treatment_arm"]))
    prov = DATA_ROOT / STUDY_ID / "study_plan.provisional.json"
    assert prov.exists()
    import json
    plan = json.loads(prov.read_text())
    m = plan["cox_ph_models"][0]
    assert m["model_name"] == "m1"
    assert m["survival_time_col"] == "pfs_days"
    assert m["event_col"] == "pfs_event"
    assert m["primary_treatment_col"] == "treatment_arm"
    assert m["covariate_cols"] == []
    assert m["rationale"] == ""


# --- regression: locked-plan reload with CoxPHModel dataclass ---


def _seed_large_dataset(study_id: str, n: int = 25):
    """Seed N rows into raw table so EPV check passes."""
    import random
    rng = random.Random(42)
    conn = get_connection(study_id)
    raw = f"raw_{study_id}"
    rows = []
    for i in range(n):
        arm = rng.choice(["A", "B"])
        pfs = int(max(30, rng.expovariate(1/200)))
        evt = rng.choices(["0", "1"], weights=[30, 70])[0]
        rows.append((arm, str(pfs), evt))
    conn.executemany(
        f"INSERT INTO {raw} (treatment_arm, pfs_days, pfs_event) "
        f"VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_cox_ph_regression_locked_plan_reload():
    """Plan → lock → reload (StudyPlan.from_dict) → unmask → analyze.

    Regression: after loading a locked plan from disk, cox_ph_models entries
    are CoxPHModel dataclass instances. cmd_analyze must handle them via
    _model_field helper, not .get().
    """
    sid = "test_cox_reload_regression"
    study_path = DATA_ROOT / sid
    if study_path.exists():
        shutil.rmtree(study_path)
    study_path.mkdir(parents=True)
    conn = get_connection(sid)
    init_db(conn)
    conn.execute(
        "INSERT INTO studies (id, name, created_at, data_dir, study_type) VALUES (?, ?, ?, ?, ?)",
        (sid, "Cox PH Reload Regression", "2025-01-01", str(study_path), "cohort"),
    )
    raw = f"raw_{sid}"
    conn.execute(f"CREATE TABLE {raw} "
        f"(row_id INTEGER PRIMARY KEY, treatment_arm TEXT, pfs_days TEXT, "
        f"pfs_event TEXT, age TEXT, high_risk_fish TEXT, prior_lines TEXT)")
    conn.executemany(
        "INSERT OR REPLACE INTO variables (id, study_id, column_name, role, data_type) VALUES (?, ?, ?, ?, ?)",
        [
            (1, sid, "treatment_arm", "baseline", "categorical"),
            (2, sid, "pfs_days", "outcome", "time_to_event"),
            (3, sid, "pfs_event", "outcome", "categorical"),
        ],
    )
    conn.commit()
    conn.close()

    _seed_large_dataset(sid, n=25)

    # Seal outcomes so the masked shadow table exists at plan time
    from core.masking.gate import seal_outcomes
    seal_outcomes(sid)

    # Plan with a Cox PH model (treatment_arm only, no extra covariates — avoids singular matrix with sparse levels)
    plan_ns = argparse.Namespace(
        study_id=sid,
        outcome_var_ids="3",
        tests=[],
        covariates="",
        study_type="cohort",
        comparison="PFS by treatment arm",
        overrides=[],
        matching_criteria="",
        cox_ph_models=["pfs_simple:pfs_days:pfs_event:treatment_arm"],
    )
    cmd_plan(plan_ns)

    lock_ns = argparse.Namespace(study_id=sid, allow_duplicate_ids=False)
    cmd_lock(lock_ns)

    # Now reload the locked plan from disk as a fresh StudyPlan
    locked_path = sorted(study_path.glob("study_plan.v*.locked.json"))[-1]
    plan_data = json.loads(locked_path.read_text())
    reloaded_plan = StudyPlan.from_dict(plan_data)
    # Verify cox_ph_models entries are CoxPHModel instances
    for m in reloaded_plan.cox_ph_models:
        assert not isinstance(m, dict), "cox_ph_models must be CoxPHModel after from_dict, not dict"
        from core.planning.study_plan import CoxPHModel
        assert isinstance(m, CoxPHModel)

    # Manually lock the reloaded plan to mimic what cmd_analyze does (load_plan)
    path = lock_plan(sid, reloaded_plan)
    assert path.exists()

    # Unmask
    unmask_study(sid)

    # Analyze with --force to bypass EPV warning (EPV < 10 with small N)
    analyze_ns = argparse.Namespace(
        study_id=sid,
        force=True,
        post_hoc=False,
        rerun=False,
    )
    cmd_analyze(analyze_ns)
    # If we get here without AttributeError, the regression is fixed.
    # Check results in DB
    conn = get_connection(sid)
    cur = conn.execute(
        "SELECT id, test_name, statistic, p_value FROM analysis_results "
        "WHERE study_id=? AND test_name=? ORDER BY id DESC LIMIT 1",
        (sid, "cox_ph_model"),
    )
    row = cur.fetchone()
    conn.close()
    assert row is not None, "No Cox PH result found in DB"
    assert row["statistic"] is not None, "Cox PH HR should not be None"
    assert row["p_value"] is not None, "Cox PH p-value should not be None"
    assert row["statistic"] > 0, "HR must be positive"

    shutil.rmtree(study_path)
