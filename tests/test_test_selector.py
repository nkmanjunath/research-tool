"""Tests for statistical test selector."""

from core.planning.test_selector import suggested_tests, check_assumptions


def test_categorical_two_group():
    tests = suggested_tests("categorical", "cohort", 2)
    names = [t["test_name"] for t in tests]
    assert "chi_square" in names
    assert "fishers_exact" in names


def test_continuous_two_group():
    tests = suggested_tests("continuous", "cohort", 2)
    names = [t["test_name"] for t in tests]
    assert "t_test" in names
    assert "mann_whitney_u" in names


def test_continuous_two_group_paired():
    tests = suggested_tests("continuous", "cohort", 2, is_paired=True)
    names = [t["test_name"] for t in tests]
    assert "paired_t_test" in names
    assert "wilcoxon_signed_rank" in names


def test_continuous_multigroup():
    tests = suggested_tests("continuous", "cohort", 3)
    names = [t["test_name"] for t in tests]
    assert "anova" in names
    assert "kruskal_wallis" in names


def test_time_to_event():
    tests = suggested_tests("time_to_event", "cohort", 2)
    names = [t["test_name"] for t in tests]
    assert "kaplan_meier_logrank" in names
    assert "cox_proportional_hazards" in names


def test_unknown_type_returns_empty():
    tests = suggested_tests("unknown", "cohort", 2)
    assert tests == []


def test_categorical_paired():
    tests = suggested_tests("categorical", "case_control", 2, is_paired=True)
    names = [t["test_name"] for t in tests]
    assert "mcnemar" in names


def test_small_n_flips_order():
    """Below 30 patients, fisher_exact should be listed first."""
    tests_small = suggested_tests("categorical", "cohort", 2, n_total=21)
    assert tests_small[0]["test_name"] == "fishers_exact"

    tests_large = suggested_tests("categorical", "cohort", 2, n_total=200)
    assert tests_large[0]["test_name"] == "chi_square"


def test_chi_square_sparse_warning():
    """21 patients, 2 arms, 5-level outcome → expected counts < 5 → warn."""
    from core.database import get_connection, init_db, DATA_ROOT
    import shutil

    study_id = "test_sparse"
    if (DATA_ROOT / study_id).exists():
        shutil.rmtree(DATA_ROOT / study_id)

    conn = get_connection(study_id)
    init_db(conn)
    conn.execute("INSERT OR REPLACE INTO studies (id,name,created_at,data_dir,study_type) VALUES (?,?,?,?,?)",
                 (study_id, "Sparse", "2025-01-01", str(DATA_ROOT / study_id), "cohort"))
    raw = f"raw_{study_id}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, response TEXT, treatment_arm TEXT)")

    # 21 patients: 2 arms (10/11 split), 5 response categories (~4 each)
    import random
    rng = random.Random(42)
    arms = ["A"] * 10 + ["B"] * 11
    rng.shuffle(arms)
    outcomes = ["CR", "PR", "MR", "SD", "PD"]
    for i, arm in enumerate(arms):
        outcome = outcomes[i % 5]
        conn.execute(f"INSERT INTO {raw} (response, treatment_arm) VALUES (?, ?)", (outcome, arm))

    conn.execute("DELETE FROM variables WHERE study_id=?", (study_id,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?,?,?,?)",
                 (study_id, "response", "outcome", "categorical"))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?,?,?,?)",
                 (study_id, "treatment_arm", "baseline", "categorical"))
    conn.commit()

    # Create shadow table (seal_outcomes would do this, but we do it directly)
    masked = f"raw_masked_{study_id}"
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {masked} (
            "row_id" INTEGER PRIMARY KEY REFERENCES {raw}(row_id),
            "response" TEXT
        )
    """)
    conn.execute(f"DELETE FROM {masked}")
    conn.execute(f"INSERT INTO {masked} (row_id, response) SELECT row_id, response FROM {raw}")
    conn.commit()
    conn.close()

    tests = [{"variable_name": "response", "test_name": "chi_square"}]
    warnings = check_assumptions(study_id, tests)

    assert len(warnings) >= 1, f"Expected at least one warning, got: {warnings}"

    # Verify the warning message doesn't leak specific category or arm names
    msg = warnings[0]
    assert "response" in msg  # variable name is fine
    assert "CR" not in msg and "PR" not in msg and "MR" not in msg
    assert "A" not in msg.split(":")[1]  # arm labels should not appear after the colon
    assert "fisher_exact" in msg  # recommends alternative

    shutil.rmtree(DATA_ROOT / study_id)


def test_chi_square_sufficient_counts():
    """200 patients, 2 arms, 3-level outcome → all expected ≥ 5 → no warnings."""
    from core.database import get_connection, init_db, DATA_ROOT
    import shutil

    study_id = "test_sufficient"
    if (DATA_ROOT / study_id).exists():
        shutil.rmtree(DATA_ROOT / study_id)

    conn = get_connection(study_id)
    init_db(conn)
    conn.execute("INSERT OR REPLACE INTO studies (id,name,created_at,data_dir,study_type) VALUES (?,?,?,?,?)",
                 (study_id, "Sufficient", "2025-01-01", str(DATA_ROOT / study_id), "cohort"))
    raw = f"raw_{study_id}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, outcome TEXT, arm TEXT)")

    # 200 patients: 100 per arm, 3 outcome levels at ~33 each
    for i in range(200):
        arm = "A" if i < 100 else "B"
        outcome = ["X", "Y", "Z"][i % 3]
        conn.execute(f"INSERT INTO {raw} (outcome, arm) VALUES (?, ?)", (outcome, arm))

    conn.execute("DELETE FROM variables WHERE study_id=?", (study_id,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?,?,?,?)",
                 (study_id, "outcome", "outcome", "categorical"))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?,?,?,?)",
                 (study_id, "arm", "baseline", "categorical"))
    conn.commit()

    masked = f"raw_masked_{study_id}"
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {masked} (
            "row_id" INTEGER PRIMARY KEY REFERENCES {raw}(row_id),
            "outcome" TEXT
        )
    """)
    conn.execute(f"DELETE FROM {masked}")
    conn.execute(f"INSERT INTO {masked} (row_id, outcome) SELECT row_id, outcome FROM {raw}")
    conn.commit()
    conn.close()

    tests = [{"variable_name": "outcome", "test_name": "chi_square", "group_col": "arm"}]
    warnings = check_assumptions(study_id, tests, group_col="arm")

    assert len(warnings) == 0, f"Expected no warnings, got: {warnings}"

    shutil.rmtree(DATA_ROOT / study_id)
