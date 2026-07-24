"""Tests for hash-verified study bundle creation and verification."""

from __future__ import annotations

import csv
import io
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.ingestion.csv_loader import load_file
from core.ingestion.variable_classifier import classify_variables_interactive, _classify_batch
from core.masking.gate import seal_outcomes, unmask_study
from core.planning.study_plan import StudyPlan
from core.planning.lock import lock_plan
from core.stats.inferential import run_test
from core.reporting.bundle import create_bundle, verify_bundle, format_verification_report
from core.reporting.manuscript_draft import write_draft


STUDY_ID = "test_bundle"


@pytest.fixture(autouse=True)
def _setup():
    if (DATA_ROOT / STUDY_ID).exists():
        shutil.rmtree(DATA_ROOT / STUDY_ID)
    (DATA_ROOT / STUDY_ID).mkdir(parents=True, exist_ok=True)
    yield
    if (DATA_ROOT / STUDY_ID).exists():
        shutil.rmtree(DATA_ROOT / STUDY_ID)


def _setup_completed_study(with_analysis: bool = True):
    """Create a full study through analysis. Returns nothing (uses STUDY_ID)."""
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, study_type, is_locked) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (STUDY_ID, "Bundle Test", "2025-01-01T00:00:00",
         str(DATA_ROOT / STUDY_ID), "cohort", 0),
    )
    conn.commit()
    conn.close()

    # Ingest minimal CSV
    csv_content = (
        "patient_id,age,sex,treatment_arm,response_category\n"
        "P001,65,M,A,CR\n"
        "P002,70,F,B,PR\n"
        "P003,55,M,A,SD\n"
    )
    tmp = Path(tempfile.mkstemp(suffix=".csv")[1])
    tmp.write_text(csv_content)
    load_file(STUDY_ID, str(tmp))
    tmp.unlink()

    # Classify
    suggestions = classify_variables_interactive(STUDY_ID,
        ["patient_id", "age", "sex", "treatment_arm", "response_category"])
    from core.ingestion.variable_classifier import _classify_batch
    _classify_batch(STUDY_ID, suggestions)
    seal_outcomes(STUDY_ID)

    # Lock plan
    plan = StudyPlan(
        study_id=STUDY_ID, study_type="cohort",
        primary_comparison="Response by treatment arm",
        primary_outcome_variable_ids=[],
        planned_tests=[{"variable_name": "response_category", "test_name": "chi_square"}],
        covariates=[],
    )
    lock_plan(STUDY_ID, plan)

    if not with_analysis:
        return

    # Unmask + analyze
    unmask_study(STUDY_ID)
    import pandas as pd
    conn = get_connection(STUDY_ID)
    df = pd.read_sql_query(f"SELECT * FROM raw_{STUDY_ID}", conn)
    init_db(conn)
    from datetime import datetime, timezone

    result = run_test("chi_square", df, outcome_col="response_category", group_col="treatment_arm")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO analysis_results (study_id, study_plan_version, variable_ids_used, "
        "test_name, statistic, p_value, ci_lower, ci_upper, status_json, "
        "is_pre_registered, provenance_json, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (STUDY_ID, plan.version, json.dumps([]),
         result["test_name"], result["statistic"], result["p_value"],
         result.get("ci_lower"), result.get("ci_upper"),
         json.dumps({"status": "completed", "reason": None}),
         json.dumps({"plan_version": plan.version}), now),
    )
    conn.commit()
    conn.close()

    # Write draft so it's included in the bundle
    write_draft(STUDY_ID)


def test_composite_hash_validates():
    """A bundle's composite hash must verify correctly against its own contents."""
    _setup_completed_study()
    result = create_bundle(STUDY_ID)
    assert result["bundle_path"].exists()

    verify = verify_bundle(str(result["bundle_path"]))
    assert verify["valid"], f"Bundle should be valid: {format_verification_report(verify)}"
    assert verify["composite_match"]
    assert verify["raw_data_match"]
    assert verify["locked_plan_match"]
    assert verify["results_match"]


def test_tampered_raw_data_detected():
    """Tampering with raw_data.json must make the bundle fail verification."""
    _setup_completed_study()
    result = create_bundle(STUDY_ID)
    bundle_path = result["bundle_path"]

    # Extract, modify raw_data.json, repack, verify
    import tarfile
    tmpdir = Path(tempfile.mkdtemp())
    with tarfile.open(str(bundle_path), "r:gz") as tar:
        tar.extractall(str(tmpdir))

    # Tamper raw_data.json
    raw_path = tmpdir / "raw_data.json"
    orig = raw_path.read_text()
    raw_path.write_text(orig.replace("P001", "P999"))

    # Repack
    tampered_path = tmpdir / "tampered.tar.gz"
    with tarfile.open(str(tampered_path), "w:gz") as tar:
        for f in tmpdir.iterdir():
            if f.name == "tampered.tar.gz":
                continue
            tar.add(str(f), arcname=f.name)

    verify = verify_bundle(str(tampered_path))
    assert not verify["valid"]
    assert not verify["raw_data_match"]
    assert verify["locked_plan_match"]  # plan unchanged
    assert verify["results_match"]      # results unchanged
    shutil.rmtree(tmpdir)


def test_tampered_plan_detected():
    """Tampering with the locked plan must break verification and identify plan hash."""
    _setup_completed_study()
    result = create_bundle(STUDY_ID)
    bundle_path = result["bundle_path"]

    import tarfile
    tmpdir = Path(tempfile.mkdtemp())
    with tarfile.open(str(bundle_path), "r:gz") as tar:
        tar.extractall(str(tmpdir))

    # Tamper the locked plan (e.g. change a covariate)
    plan_path = tmpdir / "study_plan.locked.json"
    plan_data = json.loads(plan_path.read_text())
    plan_data["covariates"] = [99]
    # Regenerate content_hash for the tampered plan
    from core.planning.lock import _compute_hash
    plan_data["content_hash"] = _compute_hash(plan_data)
    plan_path.write_text(json.dumps(plan_data, indent=2))

    tampered_path = tmpdir / "tampered.tar.gz"
    with tarfile.open(str(tampered_path), "w:gz") as tar:
        for f in tmpdir.iterdir():
            if f.name == "tampered.tar.gz":
                continue
            tar.add(str(f), arcname=f.name)

    verify = verify_bundle(str(tampered_path))
    assert not verify["valid"]
    assert not verify["locked_plan_match"]
    assert verify["raw_data_match"]
    assert verify["results_match"]
    shutil.rmtree(tmpdir)


def test_bundle_rejects_no_analysis():
    """Bundling must fail with a clear error if the study hasn't been analyzed."""
    _setup_completed_study(with_analysis=False)
    with pytest.raises(RuntimeError, match="not been unmasked"):
        create_bundle(STUDY_ID)


def test_verify_bundle_works_independently():
    """verify_bundle must work from a bundle file alone, with no original DB."""
    _setup_completed_study()
    result = create_bundle(STUDY_ID)
    bundle_path = result["bundle_path"]

    # Copy bundle to a temp dir with NO study DB
    isolated_dir = Path(tempfile.mkdtemp())
    isolated_bundle = isolated_dir / bundle_path.name
    shutil.copy2(str(bundle_path), str(isolated_bundle))

    # Delete original study data
    shutil.rmtree(DATA_ROOT / STUDY_ID)

    verify = verify_bundle(str(isolated_bundle))
    assert verify["valid"], "Bundle must verify independently of original DB"
    assert verify["composite_match"]
    shutil.rmtree(isolated_dir)


def test_missing_file_reported_correctly():
    """A bundle with a file missing entirely should say 'missing', not 'altered'."""
    _setup_completed_study()
    result = create_bundle(STUDY_ID)
    bundle_path = result["bundle_path"]

    import tarfile
    tmpdir = Path(tempfile.mkdtemp())
    with tarfile.open(str(bundle_path), "r:gz") as tar:
        tar.extractall(str(tmpdir))

    # Repack WITHOUT analysis_results.json
    tampered_path = tmpdir / "tampered.tar.gz"
    with tarfile.open(str(tampered_path), "w:gz") as tar:
        for f in tmpdir.iterdir():
            if f.name in ("tampered.tar.gz", "analysis_results.json"):
                continue
            tar.add(str(f), arcname=f.name)

    verify = verify_bundle(str(tampered_path))
    assert not verify["valid"]
    assert "analysis_results.json" in verify.get("missing_files", [])
    report = format_verification_report(verify)
    assert "is missing from the archive" in report
    assert "analysis_results.json is missing from the archive" in report
    # Should NOT say "content has been altered" for a missing file
    assert "content has been altered" not in report
    shutil.rmtree(tmpdir)


def test_altered_file_says_altered():
    """A bundle with altered content should say 'altered', not 'missing'."""
    _setup_completed_study()
    result = create_bundle(STUDY_ID)
    bundle_path = result["bundle_path"]

    import tarfile
    tmpdir = Path(tempfile.mkdtemp())
    with tarfile.open(str(bundle_path), "r:gz") as tar:
        tar.extractall(str(tmpdir))

    # Tamper raw_data.json
    raw_path = tmpdir / "raw_data.json"
    raw_path.write_text("[]")

    tampered_path = tmpdir / "tampered.tar.gz"
    with tarfile.open(str(tampered_path), "w:gz") as tar:
        for f in tmpdir.iterdir():
            if f.name == "tampered.tar.gz":
                continue
            tar.add(str(f), arcname=f.name)

    verify = verify_bundle(str(tampered_path))
    report = format_verification_report(verify)
    assert "content has been altered" in report
    assert "raw_data.json content has been altered" in report
    assert "is missing from the archive" not in report
    shutil.rmtree(tmpdir)
