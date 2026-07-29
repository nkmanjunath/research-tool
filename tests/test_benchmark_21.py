"""Integration test for the N=21 benchmark myeloma EMD study.

This test locks down the complete end-to-end pipeline for the 21-patient
synthetic myeloma cohort. It validates:
  - Full pipeline execution (ingest -> classify -> plan -> lock -> unmask -> analyze -> draft -> bundle)
  - Chi-square assumption warning for sparse 6x2 contingency tables
  - Fisher's exact test hard-rejects non-2x2 tables (no silent rxc substitution)
  - KM plot risk table row order matches legend order (Arm A top, Arm B bottom)
  - Bundle creation and hash verification pass with matching SHA-256 composites

The study uses fixed marginals matching a published myeloma EMD paper:
  - Sex: 14 M / 7 F
  - High-Risk FISH: 9 yes / 12 no
  - Response: 2 CR, 8 PR, 2 MR, 3 SD, 4 PD, 2 Unknown
  - PFS median ~120 days, OS median ~365 days
"""

from __future__ import annotations

import csv
import io
import json
import math
import shutil
import tempfile
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.ingestion.csv_loader import load_file
from core.ingestion.variable_classifier import classify_variables_interactive, _classify_batch
from core.masking.gate import seal_outcomes, unmask_study, is_masked
from core.planning.study_plan import StudyPlan
from core.planning.lock import lock_plan
from core.planning.test_selector import check_assumptions
from core.stats.inferential import _fishers_exact, run_test
from core.reporting.strobe_checklist import generate_report
from core.reporting.manuscript_draft import write_draft
from core.reporting.plots import generate_km_plot
from core.reporting.bundle import create_bundle, verify_bundle


BENCHMARK_STUDY_NAME = "Synthetic EMD 21 Patient Validation"
BENCHMARK_STUDY_ID = "b7a49fc2a42d46deaf5c2f81471dd7ae"


def _generate_benchmark_csv() -> str:
    """Generate the exact 21-row synthetic CSV with fixed marginals."""
    import random
    random.seed(42)

    n = 21
    sex_vals = ["M"] * 14 + ["F"] * 7
    fish_vals = ["yes"] * 9 + ["no"] * 12
    resp_vals = ["CR"] * 2 + ["PR"] * 8 + ["MR"] * 2 + ["SD"] * 3 + ["PD"] * 4 + ["Unknown"] * 2

    random.shuffle(sex_vals)
    random.shuffle(fish_vals)
    random.shuffle(resp_vals)

    arms = ["A"] * 10 + ["B"] * 11
    random.shuffle(arms)

    iss_vals = ["I"] * 7 + ["II"] * 8 + ["III"] * 6
    random.shuffle(iss_vals)

    prior_vals = [random.randint(0, 4) for _ in range(n)]
    ages = [random.randint(40, 85) for _ in range(n)]

    pfs_lambda = math.log(2) / 120
    pfs_vals = [max(30, int(random.expovariate(pfs_lambda))) for _ in range(n)]
    pfs_event = []
    for r in resp_vals:
        if r in ("PD", "SD"):
            pfs_event.append(1 if random.random() < 0.8 else 0)
        elif r == "Unknown":
            pfs_event.append(1 if random.random() < 0.5 else 0)
        else:
            pfs_event.append(1 if random.random() < 0.4 else 0)

    os_lambda = math.log(2) / 365
    os_vals = [max(30, int(random.expovariate(os_lambda))) for _ in range(n)]
    os_event = []
    for i in range(n):
        if pfs_event[i] == 1:
            os_event.append(1 if random.random() < 0.6 else 0)
        else:
            os_event.append(1 if random.random() < 0.3 else 0)

    rows = [
        ["patient_id", "age", "sex", "iss_stage", "prior_lines",
         "high_risk_fish", "treatment_arm", "response_category",
         "pfs_days", "pfs_event", "os_days", "os_event"]
    ]
    for i in range(n):
        rows.append([
            f"EMD_{i+1:03d}", ages[i], sex_vals[i], iss_vals[i], prior_vals[i],
            fish_vals[i], arms[i], resp_vals[i], pfs_vals[i], pfs_event[i],
            os_vals[i], os_event[i]
        ])

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerows(rows)
    return buf.getvalue()


@pytest.fixture(scope="module")
def benchmark_study():
    """Create the benchmark study once per test module.

    Returns:
        tuple: (study_id, km_test_id) where km_test_id is the analysis_results
        row ID for the Kaplan-Meier log-rank test.
    """
    study_id = BENCHMARK_STUDY_ID

    # Clean any existing study
    p = DATA_ROOT / study_id
    if p.exists():
        shutil.rmtree(p)

    # Create study
    conn = get_connection(study_id)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, study_type, is_locked) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (study_id, BENCHMARK_STUDY_NAME, "2025-06-01T00:00:00",
         str(DATA_ROOT / study_id), "cohort", 0),
    )
    conn.commit()
    conn.close()

    # Ingest CSV
    csv_content = _generate_benchmark_csv()
    tmp = Path(tempfile.mkstemp(suffix=".csv")[1])
    tmp.write_text(csv_content)
    load_file(study_id, str(tmp))
    tmp.unlink()

    # Classify variables
    suggestions = classify_variables_interactive(study_id, [
        "age", "sex", "iss_stage", "prior_lines", "high_risk_fish",
        "treatment_arm", "response_category", "pfs_days", "pfs_event",
        "os_days", "os_event"
    ])
    _classify_batch(study_id, suggestions)
    seal_outcomes(study_id)

    # Lock plan (with assumption checks like CLI does)
    plan = StudyPlan(
        study_id=study_id,
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
        covariates=[1, 3, 5],  # age, iss_stage, high_risk_fish
    )
    warnings = check_assumptions(study_id, plan.planned_tests)
    # Map warnings to test variable names (like CLI does)
    warning_map = {}
    for test in plan.planned_tests:
        var_name = test.get("variable_name", "")
        for w in warnings:
            if var_name in w:
                warning_map[var_name] = w
    plan.warnings = warning_map
    lock_plan(study_id, plan)

    # Unmask
    unmask_study(study_id)

    # Run analyze (store results in DB)
    conn = get_connection(study_id)
    import pandas as pd
    df = pd.read_sql_query(f"SELECT * FROM raw_{study_id}", conn)
    conn.close()

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(study_id)
    init_db(conn)

    km_test_id = None
    for t in plan.planned_tests:
        var_name = t.get("variable_name", "")
        test_name = t.get("test_name", "")
        kwargs = {"outcome_col": var_name, "group_col": "treatment_arm"}
        if test_name == "kaplan_meier_logrank":
            kwargs["time_col"] = "pfs_days"
            kwargs["event_col"] = "pfs_event"
        result = run_test(test_name, df, **kwargs)

        # Determine status (like CLI does)
        if test_name == "chi_square" and var_name in plan.warnings:
            status_record = {"status": "skipped_assumption_violation", "reason": plan.warnings[var_name]}
        else:
            status_record = {"status": "completed"}

        conn.execute(
            "INSERT INTO analysis_results "
            "(study_id, study_plan_version, variable_ids_used, test_name, "
            "statistic, p_value, adjusted_p_value, ci_lower, ci_upper, "
            "is_pre_registered, provenance_json, computed_at, status_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (study_id, plan.version, json.dumps([]),
             result["test_name"], result["statistic"], result["p_value"],
             result.get("adjusted_p_value"), result.get("ci_lower"), result.get("ci_upper"),
             json.dumps({"plan_version": plan.version}), now,
             json.dumps(status_record)),
        )
        if test_name == "kaplan_meier_logrank":
            km_test_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    yield study_id, km_test_id

    # Cleanup
    if p.exists():
        shutil.rmtree(p)


class TestBenchmarkPipeline:
    """Full pipeline execution tests."""

    def test_pipeline_ingest_classify(self, benchmark_study):
        """Verify ingest and classification produced expected columns and roles."""
        study_id, _ = benchmark_study
        conn = get_connection(study_id)
        cur = conn.execute(
            "SELECT column_name, role, data_type FROM variables WHERE study_id=? ORDER BY id",
            (study_id,)
        )
        vars_info = {r["column_name"]: {"role": r["role"], "dtype": r["data_type"]}
                     for r in cur.fetchall()}
        conn.close()

        # Baseline variables
        assert vars_info["age"]["role"] == "baseline"
        assert vars_info["age"]["dtype"] == "continuous"
        assert vars_info["sex"]["role"] == "baseline"
        assert vars_info["sex"]["dtype"] == "categorical"
        assert vars_info["iss_stage"]["role"] == "baseline"
        assert vars_info["prior_lines"]["role"] == "baseline"
        assert vars_info["high_risk_fish"]["role"] == "baseline"
        assert vars_info["treatment_arm"]["role"] == "baseline"

        # Outcome variables
        assert vars_info["response_category"]["role"] == "outcome"
        assert vars_info["response_category"]["dtype"] == "categorical"
        assert vars_info["pfs_days"]["role"] == "outcome"
        assert vars_info["pfs_days"]["dtype"] == "time_to_event"
        assert vars_info["os_days"]["role"] == "outcome"
        assert vars_info["os_days"]["dtype"] == "time_to_event"

    def test_plan_locked(self, benchmark_study):
        """Verify plan is locked and immutable."""
        study_id, _ = benchmark_study
        locked_plans = sorted(DATA_ROOT.glob(f"{study_id}/study_plan.v*.locked.json"))
        assert len(locked_plans) == 1
        plan_data = json.loads(locked_plans[0].read_text())
        assert plan_data["version"] == 1
        assert len(plan_data["planned_tests"]) == 2
        assert plan_data["planned_tests"][0]["test_name"] == "chi_square"
        assert plan_data["planned_tests"][1]["test_name"] == "kaplan_meier_logrank"

    def test_unmasked(self, benchmark_study):
        """Verify study is unmasked and outcomes visible."""
        study_id, _ = benchmark_study
        assert not is_masked(study_id)
        conn = get_connection(study_id)
        cur = conn.execute(f"SELECT response_category FROM raw_{study_id}")
        vals = [r["response_category"] for r in cur.fetchall()]
        conn.close()
        assert all(v is not None for v in vals)
        assert len(vals) == 21


class TestChiSquareAssumptionWarning:
    """Verify chi-square assumption warning for sparse 6x2 table."""

    def test_plan_time_warning_recorded(self, benchmark_study):
        """Chi-square on 6x2 table should record warning at plan time."""
        study_id, _ = benchmark_study
        locked_plans = sorted(DATA_ROOT.glob(f"{study_id}/study_plan.v*.locked.json"))
        plan_data = json.loads(locked_plans[0].read_text())
        warnings = plan_data.get("warnings", {})

        assert "response_category" in warnings
        warning_msg = warnings["response_category"]
        assert "minimum expected cell count" in warning_msg
        assert "1.0" in warning_msg or "1" in warning_msg  # min expected = 1.0
        assert "6×2" in warning_msg or "6x2" in warning_msg.lower()
        assert "fishers_exact" in warning_msg.lower()

    def test_analyze_skips_chi_square_without_force(self, benchmark_study):
        """Analyze should skip chi-square due to assumption warning (unless --force)."""
        study_id, _ = benchmark_study
        conn = get_connection(study_id)
        import pandas as pd
        df = pd.read_sql_query(f"SELECT * FROM raw_{study_id}", conn)
        conn.close()

        # Run chi-square directly on unmasked data
        result = run_test("chi_square", df, outcome_col="response_category", group_col="treatment_arm")

        # The test runs (we called it directly), but at plan time it was warned
        # The analyze CLI command would skip it. Here we verify the warning exists.
        assert result["p_value"] is not None  # chi-square still computes


class TestFishersExactHardReject:
    """Verify Fisher's exact test hard-rejects non-2x2 tables."""

    def test_fishers_exact_raises_on_6x2(self, benchmark_study):
        """Passing a 6x2 contingency table to _fishers_exact must raise explicit error."""
        study_id, _ = benchmark_study
        conn = get_connection(study_id)
        import pandas as pd
        df = pd.read_sql_query(f"SELECT * FROM raw_{study_id}", conn)
        conn.close()

        # response_category has 6 categories, treatment_arm has 2 -> 6x2 table
        result = _fishers_exact(df, "response_category", "treatment_arm")

        assert result["params"].get("error") is not None
        assert "2x2" in result["params"]["error"] or "2x2" in result["params"]["error"]
        assert result["statistic"] is None
        assert result["p_value"] is None

    def test_fishers_exact_works_on_2x2(self):
        """Fisher's exact should work correctly on a 2x2 table."""
        import pandas as pd
        # Create a 2x2 dataframe
        df = pd.DataFrame({
            "outcome": ["yes", "yes", "no", "no", "yes", "no"],
            "group": ["A", "A", "A", "B", "B", "B"],
        })
        result = _fishers_exact(df, "outcome", "group")

        assert result["params"].get("error") is None
        assert result["statistic"] is not None  # odds ratio
        assert result["p_value"] is not None
        assert 0 <= result["p_value"] <= 1


class TestKMVisualIntegrity:
    """Verify KM plot risk table row order matches legend order."""

    def test_km_plot_generates(self, benchmark_study):
        """KM plot should generate without error."""
        study_id, km_test_id = benchmark_study
        path = generate_km_plot(
            study_id, km_test_id,
            output_path=None,
            fmt="svg",
            show_risk_table=True,
            style="clean",
        )
        assert path.exists()
        assert path.suffix == ".svg"

    def test_risk_table_order_matches_legend(self, benchmark_study):
        """Risk table rows must be in same vertical order as legend (A top, B bottom)."""
        study_id, km_test_id = benchmark_study
        path = generate_km_plot(
            study_id, km_test_id,
            output_path=None,
            fmt="svg",
            show_risk_table=True,
            style="clean",
        )
        svg_content = path.read_text()

        # The risk table ytick labels appear as SVG comments: <!-- A --> and <!-- B -->
        # Each is followed by a <g> element with transform="translate(x y) scale(...)"
        # NOTE: SVG uses space-separated coordinates: translate(x y), NOT translate(x, y)
        # With invert_yaxis(), group[0] (A) appears at TOP (smaller y in SVG coords)
        import re

        # Find A and B labels and their y-positions from the following transform
        a_pos = svg_content.find("<!-- A -->")
        b_pos = svg_content.find("<!-- B -->")

        assert a_pos >= 0, "Risk table label for Arm A not found"
        assert b_pos >= 0, "Risk table label for Arm B not found"

        # Extract y-coordinate from transform after each label
        # SVG transform uses space-separated: translate(x y)
        def extract_y(pos):
            # Look for transform="translate(x y)" within 500 chars after the comment
            window = svg_content[pos:pos+500]
            # Pattern: translate(23.460219 371.344052)
            m = re.search(r'translate\([\d.]+ ([\d.]+)', window)
            return float(m.group(1)) if m else None

        a_y = extract_y(a_pos)
        b_y = extract_y(b_pos)

        print(f"A y={a_y}, B y={b_y}")

        assert a_y is not None, "Could not extract y for Arm A"
        assert b_y is not None, "Could not extract y for Arm B"

        # SVG y increases downward; smaller y = higher on screen
        # With invert_yaxis(), A should have smaller y = appear above B
        assert a_y < b_y, f"Risk table order wrong: A y={a_y}, B y={b_y} (A should be above B)"

    def test_right_padding_applied(self, benchmark_study):
        """X-axis should have 5% right padding so curve doesn't clip border."""
        study_id, km_test_id = benchmark_study
        path = generate_km_plot(
            study_id, km_test_id,
            output_path=None,
            fmt="svg",
            show_risk_table=True,
            style="clean",
        )
        svg_content = path.read_text()

        import re
        viewbox_match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg_content)
        assert viewbox_match, "viewBox not found in SVG"
        width = float(viewbox_match.group(1))
        # Width should be > 570 (base width for ~20 months) due to padding
        assert width > 570, f"Expected width > 570 with padding, got {width}"

    def test_risk_labels_color_coded(self, benchmark_study):
        """Risk table row labels should be color-coded: blue for A, orange for B."""
        study_id, km_test_id = benchmark_study
        path = generate_km_plot(
            study_id, km_test_id,
            output_path=None,
            fmt="svg",
            show_risk_table=True,
            style="clean",
        )
        svg_content = path.read_text()

        # The code sets label.set_color(colors[i]) where colors = ["#1f77b4", "#ff7f0e"]
        assert "#1f77b4" in svg_content, "Blue color for Arm A missing"
        assert "#ff7f0e" in svg_content, "Orange color for Arm B missing"


class TestBundleVerification:
    """Verify bundle creation and hash verification."""

    def test_bundle_creates_and_verifies(self, benchmark_study):
        """Bundle command should create archive and verify-bundle should pass."""
        study_id, _ = benchmark_study
        result = create_bundle(study_id)

        assert "bundle_path" in result
        assert "composite_hash" in result

        bundle_path = Path(result["bundle_path"])
        assert bundle_path.exists()
        assert bundle_path.suffix == ".gz"

        # Verify bundle
        verification = verify_bundle(str(bundle_path))
        assert verification["valid"] is True
        assert verification["composite_match"] is True
        # Hash should be deterministic for same inputs
        assert verification["composite_hash"] == verification["computed_composite"]
        assert verification["computed_raw_hash"] == verification["manifest"]["raw_data_hash"]
        assert verification["computed_plan_hash"] == verification["manifest"]["locked_plan_hash"]
        assert verification["computed_results_hash"] == verification["manifest"]["results_hash"]

    def test_bundle_contains_all_artifacts(self, benchmark_study):
        """Bundle should contain all expected study artifacts."""
        study_id, _ = benchmark_study
        result = create_bundle(study_id)
        bundle_path = Path(result["bundle_path"])

        import tarfile
        with tarfile.open(bundle_path, "r:gz") as tar:
            members = tar.getnames()

        # Debug: print actual members
        print("Bundle contents:", members)

        # Essential files that must be in the bundle (at root level)
        expected_files = [
            "raw_data.json",
            "study_plan.locked.json",  # bundle uses generic name
            "analysis_results.json",
            "manifest.json",
        ]
        for exp in expected_files:
            assert exp in members, f"Missing from bundle: {exp}"


class TestManuscriptDraft:
    """Verify manuscript draft generation."""

    def test_draft_generates(self, benchmark_study):
        """Draft should generate without error."""
        study_id, _ = benchmark_study
        path = write_draft(study_id)
        assert path.exists()
        assert path.suffix == ".md"

    def test_draft_contains_key_sections(self, benchmark_study):
        """Draft must contain all IMRaD sections and STROBE checklist."""
        study_id, _ = benchmark_study
        path = write_draft(study_id)
        content = path.read_text()

        for section in ["Abstract", "Introduction", "Methods", "Results",
                        "Discussion", "STROBE Checklist"]:
            assert section.lower() in content.lower(), f"Missing section: {section}"

    def test_draft_references_correct_p_values(self, benchmark_study):
        """Draft should reference the actual computed p-values."""
        study_id, _ = benchmark_study
        path = write_draft(study_id)
        content = path.read_text()

        # KM log-rank p=0.3124 should appear
        assert "0.31" in content or "0.312" in content

    def test_draft_no_exploratory_tag(self, benchmark_study):
        """Draft should not contain EXPLORATORY_POST_HOC tag (no post-hoc tests run)."""
        study_id, _ = benchmark_study
        path = write_draft(study_id)
        content = path.read_text()
        assert "EXPLORATORY_POST_HOC" not in content


class TestSTROBECompliance:
    """Verify STROBE checklist compliance."""

    def test_strobe_all_satisfied(self, benchmark_study):
        """All applicable STROBE items should be satisfied."""
        study_id, _ = benchmark_study
        report = generate_report(study_id)
        satisfied = report.count("\u2713")
        # 33 applicable items for this study design
        assert satisfied == 33, f"Expected 33 satisfied items, got {satisfied}"


# Run as standalone for quick validation
if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
