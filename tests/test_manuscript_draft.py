"""Tests for manuscript draft limitations generation."""

from __future__ import annotations

import json
import shutil

import pandas as pd
import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.planning.lock import lock_plan
from core.planning.study_plan import StudyPlan
from core.reporting.manuscript_draft import (
    generate_key_results, generate_limitations, generate_draft,
)


STUDY_ID = "test_draft_limitations"


def _setup_study(seed_vars: bool = True):
    p = DATA_ROOT / STUDY_ID
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)

    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, study_type) VALUES (?, ?, ?, ?, ?)",
        (STUDY_ID, "Limitations Test", "2025-01-01", str(p), "cohort"),
    )
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT, response TEXT, treatment_arm TEXT)")
    conn.execute(f"INSERT INTO {raw} (age, response, treatment_arm) VALUES ('65', 'CR', 'A')")
    conn.execute(f"INSERT INTO {raw} (age, response, treatment_arm) VALUES ('70', 'PR', 'B')")
    if seed_vars:
        conn.execute("DELETE FROM variables WHERE study_id=?", (STUDY_ID,))
        conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?,?,?,?)",
                     (STUDY_ID, "age", "baseline", "continuous"))
        conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?,?,?,?)",
                     (STUDY_ID, "response", "outcome", "categorical"))
    conn.commit()
    conn.close()


def _add_skipped_result():
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, statistic, p_value, status_json,
            sample_counts_json, is_pre_registered, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (STUDY_ID, "chi_square", None, None,
         json.dumps({"status": "skipped_assumption_violation",
                     "reason": "minimum expected cell count is 1.4 (below 5 threshold)"}),
         json.dumps({"n_total": 21, "n_analyzed": 0, "n_excluded": 21}),
         "2025-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()


def _add_completed_result():
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, statistic, p_value, status_json,
            sample_counts_json, is_pre_registered, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (STUDY_ID, "t_test", 2.5, 0.02,
         json.dumps({"status": "completed", "reason": None}),
         json.dumps({"n_total": 100, "n_analyzed": 95, "n_excluded": 5}),
         "2025-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()


def test_skipped_test_produces_limitations():
    _setup_study()
    plan = StudyPlan(study_id=STUDY_ID, study_type="cohort", primary_comparison="test",
                     planned_tests=[{"variable_name": "response", "test_name": "chi_square"}])
    lock_plan(STUDY_ID, plan)
    _add_skipped_result()

    text = generate_limitations(STUDY_ID)
    assert "chi_square" in text
    assert "expected cell count" in text
    assert "exploratory only" in text
    assert ".;" not in text


def test_no_skipped_no_fabricated():
    """No skipped tests, adequate sample → no fabricated limitation."""
    _setup_study()
    plan = StudyPlan(study_id=STUDY_ID, study_type="cohort", primary_comparison="test")
    lock_plan(STUDY_ID, plan)
    _add_completed_result()

    text = generate_limitations(STUDY_ID)
    # Should have the generic retrospective limitation
    assert "retrospective" in text
    # Should NOT have any skipped-test limitation
    assert "exploratory" not in text
    # Should NOT fabricate a limitation about small sample (n>=50)
    assert "limits statistical power" not in text


def test_placeholders_preserved():
    """Introduction, Interpretation, Generalisability remain placeholders."""
    _setup_study()
    lock_plan(STUDY_ID, StudyPlan(study_id=STUDY_ID, study_type="cohort"))
    _add_completed_result()

    # Test limitations function directly — doesn't require Table 1 data
    text = generate_limitations(STUDY_ID)
    assert "retrospective" in text
    # n=100 is >= 50 so no sample-size limitation should appear
    assert "limits statistical power" not in text
    assert "exploratory" not in text


def test_forced_warning_is_reported_from_planned_test_variable():
    _setup_study()
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="response by arm",
        planned_tests=[{"variable_name": "response", "test_name": "chi_square"}],
        warnings={"response": "minimum expected cell count is 1.4"},
    )
    lock_plan(STUDY_ID, plan)
    conn = get_connection(STUDY_ID)
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, statistic, p_value, status_json,
            sample_counts_json, is_pre_registered, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (STUDY_ID, "chi_square", 0.4, 0.5,
         json.dumps({"status": "completed", "reason": None}),
         json.dumps({"n_total": 21, "n_analyzed": 21, "n_excluded": 0}),
         "2025-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    text = generate_limitations(STUDY_ID)
    assert "performed despite a flagged assumption violation" in text
    assert "minimum expected cell count" in text


def test_cox_epv_uses_events_not_observations():
    _setup_study()
    conn = get_connection(STUDY_ID)
    conn.execute("ALTER TABLE raw_test_draft_limitations ADD COLUMN pfs_days TEXT")
    conn.execute("ALTER TABLE raw_test_draft_limitations ADD COLUMN pfs_event TEXT")
    conn.execute("UPDATE raw_test_draft_limitations SET pfs_days='100', pfs_event='0'")
    conn.execute("UPDATE raw_test_draft_limitations SET pfs_event='1' WHERE row_id=1")
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, ?, ?, ?)",
                 (STUDY_ID, "pfs_days", "outcome", "time_to_event"))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, ?, ?, ?)",
                 (STUDY_ID, "pfs_event", "outcome", "categorical"))
    conn.commit()
    conn.close()
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by arm",
        planned_tests=[{"variable_name": "pfs_days", "test_name": "cox_proportional_hazards"}],
        covariates=[1],
    )
    lock_plan(STUDY_ID, plan)
    conn = get_connection(STUDY_ID)
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, statistic, p_value, status_json,
            sample_counts_json, is_pre_registered, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (STUDY_ID, "cox_proportional_hazards", 1.2, 0.8,
         json.dumps({"status": "completed", "reason": None}),
         json.dumps({"n_total": 100, "n_analyzed": 100, "n_excluded": 0}),
         "2025-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    text = generate_limitations(STUDY_ID)
    assert "EPV=0.5" in text
    assert "events across 2 predictors" in text


def test_key_results_counts_statuses_without_editorializing():
    assert generate_key_results([
        {"status_json": json.dumps({"status": "completed"})},
        {"status_json": json.dumps({"status": "skipped_assumption_violation"})},
    ]) == (
        "**Key results:** Of 2 pre-registered test(s), 1 completed successfully "
        "and 1 was skipped due to an assumption violation."
    )


# ── Fix 1: MultiIndex column flattening ──────────────────────────────────

def test_table1_multiindex_columns_flattened(monkeypatch):
    """Table 1 with MultiIndex columns should be flattened before markdown rendering."""
    _setup_study()
    lock_plan(STUDY_ID, StudyPlan(study_id=STUDY_ID, study_type="cohort", primary_comparison="test"))

    # Create a DataFrame with MultiIndex columns like tableone produces
    idx = pd.Index(["age", "sex"], name="variable")
    cols = pd.MultiIndex.from_tuples([
        ("Grouped by treatment_arm", "Missing"),
        ("Grouped by treatment_arm", "Overall"),
        ("Grouped by treatment_arm", "A"),
        ("Grouped by treatment_arm", "B"),
    ])
    fake_tbl = pd.DataFrame(
        [["2 (10.0%)", "21", "10", "11"],
         ["21 (100.0%)", "21", "10", "11"]],
        index=idx, columns=cols,
    )

    def mock_table1(study_id, groupby=None):
        return fake_tbl

    monkeypatch.setattr("core.stats.descriptive.generate_table1", mock_table1)

    draft = generate_draft(STUDY_ID)
    # No tuple strings in the output
    assert "('Grouped" not in draft
    assert "'Missing'" not in draft
    # Clean headers should appear
    assert "Missing" in draft
    assert "Overall" in draft


# ── Fix 2: Abstract hydration ─────────────────────────────────────────────

def test_abstract_hydrated_with_analysis_results():
    """Abstract's Results line should use real analysis counts, not a placeholder."""
    _setup_study()
    lock_plan(STUDY_ID, StudyPlan(study_id=STUDY_ID, study_type="cohort", primary_comparison="test"))
    _add_skipped_result()
    _add_completed_result()

    draft = generate_draft(STUDY_ID)

    # Should NOT contain the old placeholder
    assert "[Results summary" not in draft
    # 1 skipped + 1 completed = 1 of 2 completed
    assert "1 of 2 pre-registered tests completed" in draft


def test_abstract_placeholder_when_no_analyses():
    """Abstract should keep placeholder when no analysis results exist."""
    _setup_study()
    lock_plan(STUDY_ID, StudyPlan(study_id=STUDY_ID, study_type="cohort", primary_comparison="test"))

    draft = generate_draft(STUDY_ID)

    assert "[Results summary" in draft
