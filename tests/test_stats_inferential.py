"""Tests for inferential statistics."""

import numpy as np
import pandas as pd
import pytest

from core.stats.inferential import run_test


def _make_df():
    """Simple two-group data."""
    return pd.DataFrame({
        "group": ["A"] * 20 + ["B"] * 20,
        "outcome": [1] * 12 + [0] * 8 + [1] * 6 + [0] * 14,
        "continuous": [65 + i for i in range(20)] + [70 + i for i in range(20)],
        "time": [100 + i * 10 for i in range(40)],
        "event": [1 if i < 10 else 0 for i in range(20)] + [1 if i < 15 else 0 for i in range(20)],
    })


def test_chi_square():
    df = _make_df()
    result = run_test("chi_square", df, "outcome", "group")
    assert result["test_name"] == "chi_square"
    assert result["statistic"] is not None
    assert result["p_value"] is not None


def test_fishers_exact():
    df = _make_df()
    result = run_test("fishers_exact", df, "outcome", "group")
    assert result["test_name"] == "fishers_exact"
    assert result["statistic"] is not None


def test_t_test():
    df = _make_df()
    result = run_test("t_test", df, "outcome", "group")
    assert result["test_name"] == "t_test"
    assert result["statistic"] is not None
    assert result["ci_lower"] is not None
    assert result["ci_upper"] is not None
    assert result["ci_lower"] < result["ci_upper"]


def test_mann_whitney():
    df = _make_df()
    result = run_test("mann_whitney_u", df, "continuous", "group")
    assert result["test_name"] == "mann_whitney_u"
    assert result["statistic"] is not None


def test_anova():
    df = pd.DataFrame({
        "group": ["A"] * 10 + ["B"] * 10 + ["C"] * 10,
        "val": [60 + i for i in range(10)] + [70 + i for i in range(10)] + [80 + i for i in range(10)],
    })
    result = run_test("anova", df, "val", "group")
    assert result["test_name"] == "anova"
    assert result["statistic"] is not None


def test_kruskal_wallis():
    df = pd.DataFrame({
        "group": ["A"] * 10 + ["B"] * 10 + ["C"] * 10,
        "val": [60 + i for i in range(10)] + [70 + i for i in range(10)] + [80 + i for i in range(10)],
    })
    result = run_test("kruskal_wallis", df, "val", "group")
    assert result["test_name"] == "kruskal_wallis"
    assert result["statistic"] is not None


def test_kaplan_meier_logrank():
    df = _make_df()
    result = run_test("kaplan_meier_logrank", df, "outcome",
                      "group", time_col="time", event_col="event")
    assert result["test_name"] == "kaplan_meier_logrank"
    assert result["statistic"] is not None
    assert result["p_value"] is not None


def test_cox_ph():
    df = _make_df()
    result = run_test("cox_proportional_hazards", df, "outcome",
                      "group", time_col="time", event_col="event",
                      covariates=["continuous"])
    assert result["test_name"] == "cox_proportional_hazards"
    # May or may not converge on tiny data, but should not crash
    assert result is not None


def test_unknown_test():
    df = _make_df()
    result = run_test("imaginary_test", df, "outcome", "group")
    assert "error" in str(result.get("params", {}))


def test_kaplan_meier_rejects_missing_event_col():
    """Survival test on a duration-only column must raise a clear ValueError."""
    df = pd.DataFrame({
        "pfs_days": [100, 200, 150],
        "treatment_arm": ["A", "B", "A"],
    })
    with pytest.raises(ValueError, match="no linked event/censoring column"):
        run_test("kaplan_meier_logrank", df, outcome_col="pfs_days",
                 group_col="treatment_arm", time_col="pfs_days",
                 event_col="pfs_event")


def test_kaplan_meier_rejects_none_event_col():
    """Survival test with event_col=None must raise a clear ValueError."""
    df = pd.DataFrame({
        "pfs_days": [100, 200, 150],
        "treatment_arm": ["A", "B", "A"],
    })
    with pytest.raises(ValueError, match="no linked event/censoring column"):
        run_test("kaplan_meier_logrank", df, outcome_col="pfs_days",
                 group_col="treatment_arm", time_col="pfs_days",
                 event_col=None)


def test_cox_ph_rejects_missing_event_col():
    """Cox PH on a duration-only column must raise a clear ValueError."""
    df = pd.DataFrame({
        "pfs_days": [100, 200, 150],
        "treatment_arm": ["A", "B", "A"],
    })
    with pytest.raises(ValueError, match="no linked event/censoring column"):
        run_test("cox_proportional_hazards", df, outcome_col="pfs_days",
                 group_col="treatment_arm", time_col="pfs_days",
                 event_col="pfs_event", covariates=[])


def test_cox_ph_model_treats_string_stored_numeric_as_continuous():
    """String-dtype column (simulating SQLite TEXT ingest) with 'continuous'
    in var_types must be converted to numeric, not treated as categorical.

    Without var_types, a string 'age' column produces one lifelines dummy
    per unique value (age[T.45], age[T.70], ...), causing singular Hessian.
    With var_types={'age': 'continuous'}, it becomes a single coefficient.
    """
    df = pd.DataFrame({
        "time":    ["100", "200", "150", "300"],
        "event":   ["1",   "0",   "1",   "0"],
        "arm":     ["A",   "A",   "B",   "B"],
        "age":     ["45",  "70",  "55",  "60"],
    })
    var_types = {"age": "continuous", "arm": "categorical",
                 "time": "time_to_event", "event": "categorical"}

    result = run_test(
        "cox_ph_model", df,
        outcome_col="time", group_col="arm",
        time_col="time", event_col="event",
        covariates=["age"],
        var_types=var_types,
    )
    # Should converge with a single age coefficient (HR > 0)
    assert result["statistic"] is not None, "HR must not be None"
    assert result["statistic"] > 0, "HR must be positive"
    assert result["p_value"] is not None

    # Per-covariate results: age should appear as one row (no per-value dummies)
    cov_results = result.get("params", {}).get("per_covariate_results", [])
    age_rows = [r for r in cov_results if r["covariate"] == "age"]
    assert len(age_rows) == 1, (
        f"Expected 1 coefficient for 'age', got {len(age_rows)} — "
        f"variable was likely treated as categorical: {cov_results}"
    )


def test_cox_ph_model_without_var_types_falls_back_to_dtype():
    """When var_types is None, fall back to pandas dtype check."""
    df = pd.DataFrame({
        "time":    [100.0, 200.0, 150.0, 300.0],
        "event":   [1,     0,     1,     0],
        "arm":     ["A",   "A",   "B",   "B"],
        "age":     [45.0,  70.0,  55.0,  60.0],
    })
    # Numeric dtypes should be detected as numeric even without var_types
    result = run_test(
        "cox_ph_model", df,
        outcome_col="time", group_col="arm",
        time_col="time", event_col="event",
        covariates=["age"],
    )
    assert result["statistic"] is not None
    assert result["statistic"] > 0


def test_cox_ph_model_returns_hr_scale_ci():
    """CI bounds must be on HR scale (positive), not log-HR (coefficient scale)."""
    df = pd.DataFrame({
        "time":    [100.0, 200.0, 150.0, 300.0, 180.0, 220.0, 90.0, 350.0],
        "event":   [1,     0,     1,     0,     1,     0,     1,    0],
        "arm":     ["A",   "A",   "B",   "B",   "A",   "A",   "B",  "B"],
        "age":     [45.0,  70.0,  55.0,  60.0,  50.0,  65.0,  58.0, 62.0],
        "marker":  [1.2,   3.4,   2.1,   4.5,   1.8,   2.9,   3.2,  4.1],
    })
    result = run_test(
        "cox_ph_model", df,
        outcome_col="time", group_col="arm",
        time_col="time", event_col="event",
        covariates=["age", "marker"],
    )
    assert result["statistic"] is not None

    # Primary treatment: CI must be on HR scale (both > 0)
    assert result["ci_lower"] > 0, f"ci_lower={result['ci_lower']} must be >0 (HR scale)"
    assert result["ci_lower"] < result["statistic"] < result["ci_upper"], \
        f"CI ({result['ci_lower']}, {result['ci_upper']}) must contain HR ({result['statistic']})"

    # Per-covariate: every CI must be on HR scale
    for cov in result.get("params", {}).get("per_covariate_results", []):
        assert cov["ci_lower"] > 0, \
            f"{cov['covariate']}: ci_lower={cov['ci_lower']} must be >0 (HR scale)"
        assert cov["ci_lower"] < cov["hr"] < cov["ci_upper"], \
            f"{cov['covariate']}: CI ({cov['ci_lower']}, {cov['ci_upper']}) must contain HR ({cov['hr']})"


def test_cox_ph_model_overflow_ci_upper_becomes_inf():
    """Near-complete separation produces inf upper CI — must not crash."""
    df = pd.DataFrame({
        "time":   [100.0, 200.0, 150.0, 300.0],
        "event":  [1,     0,     1,     0],
        "arm":    ["A",   "A",   "B",   "B"],
        "age":    [45.0,  70.0,  55.0,  60.0],
    })
    result = run_test(
        "cox_ph_model", df,
        outcome_col="time", group_col="arm",
        time_col="time", event_col="event",
        covariates=["age"],
    )
    ci_upper = result["ci_upper"]
    assert np.isinf(ci_upper), f"Expected inf upper CI for near-complete separation, got {ci_upper}"
    assert result["ci_lower"] is not None


def test_cox_ph_model_persists_params_to_db():
    """Cox PH model-level fields and covariate rows must be persisted to DB."""
    import json, shutil
    from core.database import get_connection, init_db, migrate_db, DATA_ROOT

    study_id = "test_cox_ph_persist"
    study_dir = DATA_ROOT / study_id
    if study_dir.exists():
        shutil.rmtree(study_dir)

    conn = get_connection(study_id)
    init_db(conn)
    conn.execute(
        "INSERT INTO studies (id, name, created_at, data_dir) VALUES (?, ?, ?, ?)",
        (study_id, "test", "2025-01-01T00:00:00", str(study_dir)),
    )

    df = pd.DataFrame({
        "time":   [100.0, 200.0, 150.0, 300.0, 180.0, 220.0],
        "event":  [1,     0,     1,     0,     1,     0],
        "arm":    ["A",   "A",   "B",   "B",   "A",   "A"],
        "age":    [45.0,  70.0,  55.0,  60.0,  50.0,  65.0],
    })

    result = run_test(
        "cox_ph_model", df,
        outcome_col="time", group_col="arm",
        time_col="time", event_col="event",
        covariates=["age"],
        var_types={"age": "continuous", "arm": "categorical"},
    )

    now = "2025-01-01T00:00:00"
    params = result.get("params", {})
    cursor = conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, statistic, p_value, ci_lower, ci_upper,
            effect_size_json, sample_counts_json, status_json,
            is_pre_registered, provenance_json, computed_at,
            lr_test_p, concordance_index, ph_diagnostics_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
        (study_id, "cox_ph_model", result["statistic"], result["p_value"],
         result.get("ci_lower"), result.get("ci_upper"),
         json.dumps(result["effect_size"]) if result.get("effect_size") else None,
         json.dumps(result["sample_counts"]) if result.get("sample_counts") else None,
         json.dumps({"status": "completed"}),
         json.dumps({"plan_version": 1}), now,
         params.get("lr_test_p_value"), params.get("concordance_index"),
         json.dumps(params.get("assumption_diagnostics")) if params.get("assumption_diagnostics") else None),
    )
    result_id = cursor.lastrowid

    cov_results = params.get("per_covariate_results", [])
    for cr in cov_results:
        conn.execute(
            """INSERT INTO analysis_covariate_results
               (result_id, covariate, hr, ci_lower, ci_upper,
                wald_p, coef, se, z)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result_id,
             cr.get("covariate"), cr.get("hr"),
             cr.get("ci_lower"), cr.get("ci_upper"),
             cr.get("wald_p"), cr.get("coef"),
             cr.get("se"), cr.get("z")),
        )
    conn.commit()

    # Verify model-level fields persisted
    cur = conn.execute("SELECT * FROM analysis_results WHERE id=?", (result_id,))
    row = cur.fetchone()
    assert row is not None
    assert row["test_name"] == "cox_ph_model"
    if params.get("lr_test_p_value") is not None:
        assert abs(row["lr_test_p"] - params["lr_test_p_value"]) < 1e-6
    if params.get("concordance_index") is not None:
        assert abs(row["concordance_index"] - params["concordance_index"]) < 1e-6

    # Verify covariate rows queryable via SQL
    cur = conn.execute(
        "SELECT * FROM analysis_covariate_results WHERE result_id=? ORDER BY id", (result_id,)
    )
    db_rows = cur.fetchall()
    assert len(db_rows) == len(cov_results)
    for db_row, expected in zip(db_rows, cov_results):
        assert db_row["covariate"] == expected["covariate"]
        if expected.get("hr") is not None:
            assert abs(db_row["hr"] - expected["hr"]) < 1e-6

    conn.close()


def test_non_cox_result_inserts_with_null_new_columns():
    """Non-Cox-PH result types must insert cleanly with new columns as NULL."""
    import json, shutil
    from core.database import get_connection, init_db, DATA_ROOT

    study_id = "test_non_cox_null"
    study_dir = DATA_ROOT / study_id
    if study_dir.exists():
        shutil.rmtree(study_dir)

    conn = get_connection(study_id)
    init_db(conn)
    conn.execute(
        "INSERT INTO studies (id, name, created_at, data_dir) VALUES (?, ?, ?, ?)",
        (study_id, "test", "2025-01-01T00:00:00", str(study_dir)),
    )

    now = "2025-01-01T00:00:00"
    # Insert a chi-square result
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, statistic, p_value, status_json,
            is_pre_registered, provenance_json, computed_at)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
        (study_id, "chi_square", 3.84, 0.05,
         json.dumps({"status": "completed"}),
         json.dumps({"plan_version": 1}), now),
    )
    conn.commit()

    cur = conn.execute(
        "SELECT * FROM analysis_results WHERE study_id=? AND test_name=?", (study_id, "chi_square")
    )
    row = cur.fetchone()
    assert row is not None
    # New columns must be NULL for non-Cox results
    assert row["lr_test_p"] is None
    assert row["concordance_index"] is None
    assert row["ph_diagnostics_json"] is None

    conn.close()
