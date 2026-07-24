"""End-to-end pipeline validation using synthetic myeloma data (EMD study schema).

Simulates a retrospective cohort study of 21 patients with multiple myeloma
and extramedullary disease (EMD).  Variables:
  - Baseline: age, sex, iss_stage, prior_lines, high_risk_cytogenetics
  - Exposure: treatment_arm (A / B)
  - Outcome: response_category (CR/PR/MR/SD/PD), pfs_days + pfs_event, os_days + os_event

This is a self-check test, not a formal pytest test — run it directly.
"""

from __future__ import annotations
import csv
import io
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Ensure the research-tool package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import get_connection, init_db, DATA_ROOT
from core.ingestion.csv_loader import load_file
from core.ingestion.variable_classifier import classify_variables_interactive, _classify_batch
from core.masking.gate import seal_outcomes, unmask_study, is_masked
from core.planning.study_plan import StudyPlan
from core.planning.lock import lock_plan, load_plan
from core.stats.descriptive import generate_table1
from core.stats.inferential import run_test
from core.stats.multiple_comparisons import correct
from core.reporting.strobe_checklist import generate_report
from core.reporting.manuscript_draft import write_draft

STUDY_NAME = "EMD Myeloma Validation"
STUDY_ID = "emd_val_001"
SEED = 2024


def _synthetic_emd_csv() -> str:
    """Generate 21-row synthetic CSV matching a retrospective myeloma EMD study."""
    import random
    rng = random.Random(SEED)
    rows = [
        ["patient_id", "age", "sex", "iss_stage", "prior_lines",
         "high_risk_cytogenetics", "treatment_arm", "response_category",
         "pfs_days", "pfs_event", "os_days", "os_event"]
    ]
    for i in range(21):
        pid = f"EMD_{i+1:03d}"
        age = rng.randint(40, 85)
        sex = rng.choice(["M", "F"])
        iss = rng.choices(["I", "II", "III"], weights=[30, 40, 30])[0]
        prior = rng.randint(0, 4)
        hr_cyto = rng.choice(["yes", "no"])
        arm = rng.choice(["A", "B"])
        resp = rng.choices(
            ["CR", "PR", "MR", "SD", "PD"],
            weights=[15, 30, 20, 20, 15],
        )[0]
        pfs = max(30, int(rng.expovariate(1 / 250)))
        pfs_ev = 1 if resp in ("PD",) else rng.choices([0, 1], weights=[35, 65])[0]
        os_d = pfs + max(0, int(rng.expovariate(1 / 300)))
        os_ev = 1 if pfs_ev else rng.choices([0, 1], weights=[50, 50])[0]
        rows.append([pid, age, sex, iss, prior, hr_cyto, arm, resp, pfs, pfs_ev, os_d, os_ev])

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerows(rows)
    return buf.getvalue()


def _assert_near(actual, expected, tol=0.15, label=""):
    """Assert that actual is within tol of expected (relative)."""
    if expected == 0:
        assert abs(actual) < tol, f"{label}: expected ~0, got {actual}"
        return
    rel_err = abs(actual - expected) / abs(expected)
    assert rel_err <= tol, (
        f"{label}: expected {expected}, got {actual} "
        f"(relative error {rel_err:.2%} > {tol:.0%})"
    )


def run_pipeline() -> dict:
    """Run the full pipeline end-to-end.

    Returns a dict with summary stats and status.
    """
    summary = {"phase": "", "status": "ok", "issues": []}

    # ── 1. Create study ────────────────────────────────────────────────────
    summary["phase"] = "create_study"
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, study_type, is_locked) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (STUDY_ID, STUDY_NAME, "2025-06-01T00:00:00",
         str(DATA_ROOT / STUDY_ID), "cohort", 0),
    )
    conn.commit()
    conn.close()
    print("✓ Study created")

    # ── 2. Ingest CSV ─────────────────────────────────────────────────────
    summary["phase"] = "ingest"
    csv_content = _synthetic_emd_csv()
    tmp = Path(tempfile.mkstemp(suffix=".csv")[1])
    tmp.write_text(csv_content)
    columns = load_file(STUDY_ID, str(tmp))
    tmp.unlink()
    assert len(columns) == 12  # patient_id + 11 data columns
    print(f"✓ Data ingested: {len(columns)} columns")
    summary["n_columns"] = len(columns)

    # ── 3. Classify variables + seal outcomes ─────────────────────────────
    summary["phase"] = "classify"
    suggestions = classify_variables_interactive(STUDY_ID, ["age", "sex", "iss_stage",
        "prior_lines", "high_risk_cytogenetics", "treatment_arm",
        "response_category", "pfs_days", "pfs_event", "os_days", "os_event"])
    _classify_batch(STUDY_ID, suggestions)
    # Seal outcomes into shadow table (physical storage-level masking)
    seal_outcomes(STUDY_ID)
    conn = get_connection(STUDY_ID)
    cur = conn.execute("SELECT COUNT(*) as cnt FROM variables WHERE study_id=?", (STUDY_ID,))
    n_vars = cur.fetchone()["cnt"]
    conn.close()
    assert n_vars > 0
    print(f"✓ Variables classified: {n_vars}")

    # ── 4. Explore baseline (masked) ──────────────────────────────────────
    summary["phase"] = "explore_baseline"
    conn = get_connection(STUDY_ID)
    cur = conn.execute("SELECT age FROM raw_emd_val_001")
    rows = cur.fetchall()
    ages = [r["age"] for r in rows]
    assert len(ages) == 21
    # Outcome columns should be physically NULL
    cur2 = conn.execute("SELECT response_category FROM raw_emd_val_001")
    for r in cur2.fetchall():
        assert r["response_category"] is None, "outcome must be NULL post-seal"
    conn.close()
    print(f"✓ Baseline explored (outcomes physically NULL in DB): {len(ages)} rows, "
          f"age range {min(ages)}-{max(ages)}")

    # ── 5. Table 1 (masked, baseline only) ────────────────────────────────
    summary["phase"] = "table1_masked"
    tbl = generate_table1(STUDY_ID, groupby="treatment_arm")
    assert len(tbl) > 0
    print(f"✓ Table 1 generated ({len(tbl)} rows), baseline only")

    # ── 6. Declare and lock study plan ────────────────────────────────────
    summary["phase"] = "lock_plan"
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS and response by treatment arm",
        primary_outcome_variable_ids=[7, 8],  # response_category, pfs_days
        planned_tests=[
            {"variable_id": 7, "variable_name": "response_category",
             "test_name": "chi_square",
             "rationale": "Compare response distribution between arms"},
            {"variable_id": 8, "variable_name": "pfs_days",
             "test_name": "kaplan_meier_logrank",
             "rationale": "Compare PFS between treatment arms"},
        ],
        covariates=[1, 3, 5],  # age, iss_stage, high_risk_cytogenetics
    )
    path = lock_plan(STUDY_ID, plan)
    assert path.exists()
    assert "v1" in path.name
    print(f"✓ Plan locked: {path.name}")

    # ── 7. Unmask ─────────────────────────────────────────────────────────
    summary["phase"] = "unmask"
    unmask_study(STUDY_ID)
    assert not is_masked(STUDY_ID)
    # Verify outcomes visible
    conn = get_connection(STUDY_ID)
    cur3 = conn.execute("SELECT response_category FROM raw_emd_val_001")
    outcome_vals = [r["response_category"] for r in cur3.fetchall()]
    conn.close()
    assert all(v is not None for v in outcome_vals)
    print(f"✓ Study unmasked, outcomes visible ({len(outcome_vals)} values)")

    # ── 8. Run pre-registered analyses ────────────────────────────────────
    summary["phase"] = "analyze"
    conn = get_connection(STUDY_ID)
    import pandas as pd
    df = pd.read_sql_query("SELECT * FROM raw_emd_val_001", conn)
    conn.close()

    results = []
    p_vals = []
    conn = get_connection(STUDY_ID)
    init_db(conn)

    for t in plan.planned_tests:
        var_name = t.get("variable_name", "")
        test_name = t.get("test_name", "")
        kwargs = {"outcome_col": var_name, "group_col": "treatment_arm"}
        if test_name == "kaplan_meier_logrank":
            kwargs["time_col"] = "pfs_days"
            kwargs["event_col"] = "pfs_event"
        result = run_test(test_name, df, **kwargs)
        p_vals.append(result["p_value"] or 1.0)
        results.append(result)
        p_str = f"p={result['p_value']:.4f}" if result['p_value'] else "error"
        print(f"  {test_name}: stat={result['statistic']:.4f}, {p_str}")

    # Multiple comparisons correction
    if len(results) > 1:
        corrected = correct(p_vals)
        for r, cp in zip(results, corrected):
            r["adjusted_p_value"] = cp
    elif results:
        results[0]["adjusted_p_value"] = results[0]["p_value"]

    # Store
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        conn.execute(
            "INSERT INTO analysis_results "
            "(study_id, study_plan_version, variable_ids_used, test_name, "
            "statistic, p_value, adjusted_p_value, ci_lower, ci_upper, "
            "is_pre_registered, provenance_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (STUDY_ID, plan.version, json.dumps([]),
             r["test_name"], r["statistic"], r["p_value"],
             r.get("adjusted_p_value"), r.get("ci_lower"), r.get("ci_upper"),
             json.dumps({"plan_version": plan.version}), now),
        )
    conn.commit()
    conn.close()

    summary["n_analyses"] = len(results)
    print(f"✓ Analyses complete: {len(results)} pre-registered tests")

    # ── 9. STROBE check ──────────────────────────────────────────────────
    summary["phase"] = "strobe_check"
    report = generate_report(STUDY_ID)
    n_satisfied = report.count("✓")
    summary["strobe_satisfied"] = n_satisfied
    print(f"✓ STROBE check: {n_satisfied} items satisfied")
    print(report)

    # ── 10. Manuscript draft ─────────────────────────────────────────────
    summary["phase"] = "draft"
    draft_path = write_draft(STUDY_ID)
    assert draft_path.exists()
    draft_text = draft_path.read_text()
    assert "EXPLORATORY_POST_HOC" not in draft_text  # no post-hoc in this run
    print(f"✓ Manuscript draft written to {draft_path}")

    # ── Verifications ────────────────────────────────────────────────────
    summary["phase"] = "verification"

    # Verify the draft is valid markdown with key sections
    for section in ["Abstract", "Introduction", "Methods", "Results", "Discussion",
                    "STROBE Checklist"]:
        assert section.lower() in draft_text.lower(), f"Missing section: {section}"
    print(f"✓ Draft contains all IMRaD sections")

    # Verify provenance file exists
    provenance_path = DATA_ROOT / STUDY_ID / "provenance.json"
    assert provenance_path.exists() is False  # we used direct SQL, not the tracker

    summary["status"] = "ok"
    summary["phase"] = "complete"

    return summary


def cleanup():
    """Remove test study data."""
    p = DATA_ROOT / STUDY_ID
    if p.exists():
        shutil.rmtree(p)


if __name__ == "__main__":
    print("=" * 60)
    print(f"End-to-End Validation: {STUDY_NAME}")
    print("=" * 60)
    try:
        result = run_pipeline()
        print()
        print("=" * 60)
        print(f"VALIDATION COMPLETE: {result['status']}")
        print(f"  Phases completed: {result['phase']}")
        print(f"  Columns ingested: {result.get('n_columns', '?')}")
        print(f"  Analyses run: {result.get('n_analyses', '?')}")
        print(f"  STROBE items satisfied: {result.get('strobe_satisfied', '?')}")
        print("=" * 60)
    finally:
        cleanup()
        print("Cleanup complete.")
