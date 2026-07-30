"""Tests for the flowchart module."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from core.database import DATA_ROOT, get_connection, init_db
from core.ingestion.csv_loader import load_file
from core.reporting.flowchart import (
    FlowStage,
    FlowchartData,
    load_flowchart_data,
    render_ascii,
    render_svg,
)


def _make_study(study_id: str, with_duplicates: bool = False,
                with_nan_covariate: bool = False):
    study_dir = DATA_ROOT / study_id
    if study_dir.exists():
        shutil.rmtree(study_dir)
    study_dir.mkdir(parents=True, exist_ok=True)

    conn = get_connection(study_id)
    init_db(conn)
    conn.execute(
        """INSERT INTO studies (id, name, created_at, data_dir, is_locked, unmasked_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (study_id, "test", "2026-01-01T00:00:00+00:00", str(study_dir), 2, "2026-07-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    import pandas as pd
    if with_duplicates:
        df = pd.DataFrame({
            "patient_id": ["P001", "P002", "P001", "P003", "P004"],
            "age": [50, 55, 50, 62, 58],
            "treatment_arm": ["A", "B", "A", "A", "B"],
            "pfs_days": [150, 280, 180, 320, 240],
            "pfs_event": [1, 0, 1, 1, 0],
        })
    elif with_nan_covariate:
        # One row in arm B has NaN age — Cox PH excludes it
        df = pd.DataFrame({
            "patient_id": ["P001", "P002", "P003", "P004", "P005", "P006"],
            "age": [45, None, 58, 63, 70, 48],
            "treatment_arm": ["A", "B", "A", "B", "A", "B"],
            "pfs_days": [120, 250, 180, 350, 400, 220],
            "pfs_event": [1, 0, 1, 1, 0, 1],
        })
    else:
        df = pd.DataFrame({
            "patient_id": ["P001", "P002", "P003", "P004", "P005", "P006", "P007", "P008"],
            "age": [45, 52, 58, 63, 70, 48, 55, 68],
            "treatment_arm": ["A", "B", "A", "B", "A", "B", "A", "B"],
            "pfs_days": [120, 250, 180, 350, 400, 220, 150, 300],
            "pfs_event": [1, 0, 1, 1, 0, 1, 0, 1],
        })

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        df.to_csv(f.name, index=False)
        csv_path = f.name

    load_file(study_id, csv_path)

    # Write a minimal plan with cox model
    plan = {
        "study_id": study_id,
        "version": 1,
        "locked_at": "2026-07-01T00:00:00+00:00",
        "primary_comparison": "treatment_arm",
        "cox_ph_models": [{
            "model_name": "pfs_multivariable",
            "survival_time_col": "pfs_days",
            "event_col": "pfs_event",
            "primary_treatment_col": "treatment_arm",
            "covariate_cols": ["age"],
        }],
        "warnings": {},
    }
    (DATA_ROOT / study_id / "study_plan.v1.locked.json").write_text(json.dumps(plan))

    # Run a Cox PH model to populate analysis_results
    from core.stats.inferential import run_test
    conn = get_connection(study_id)
    raw = f"raw_{study_id}"
    df = pd.read_sql(f"SELECT * FROM {raw}", conn)
    conn.close()

    run_test("cox_ph_model", df,
             outcome_col="pfs_days", group_col="treatment_arm",
             time_col="pfs_days", event_col="pfs_event",
             covariates=["age"])

    return study_id


class TestFlowchart:
    def setup_method(self):
        self.sid_clean = "test_flowchart_clean"
        self.sid_dupes = "test_flowchart_dupes"
        self.sid_nan_cov = "test_flowchart_nan_cov"
        _make_study(self.sid_clean, with_duplicates=False)
        _make_study(self.sid_dupes, with_duplicates=True)
        _make_study(self.sid_nan_cov, with_nan_covariate=True)

    def teardown_method(self):
        for sid in [self.sid_clean, self.sid_dupes, self.sid_nan_cov]:
            shutil.rmtree(DATA_ROOT / sid, ignore_errors=True)

    def test_clean_study_loads_stages(self):
        """Clean study loads all 6 stages with correct counts."""
        data = load_flowchart_data(self.sid_clean)
        assert len(data.stages) == 6
        assert data.stages[0].name == "Assessed for eligibility"
        assert data.stages[0].total == 8
        assert data.stages[0].excluded == 0
        assert data.stages[1].name == "Excluded at ingest (duplicate patient IDs)"
        assert data.stages[1].excluded == 0
        assert data.stages[2].name == "Eligible cohort"  # internal name, SVG collapses it
        assert data.stages[3].name == "Allocated to each arm/group"
        assert "A" in data.stages[3].details.get("arm_counts", {})
        assert data.stages[4].name.startswith("Analyzed")
        assert data.stages[5].name.startswith("Final analyzed")

    def test_duplicate_study_shows_excluded(self):
        """Study with duplicates shows exclusion at ingest stage."""
        data = load_flowchart_data(self.sid_dupes)
        assert len(data.stages) >= 2
        ingest_stage = data.stages[1]
        assert ingest_stage.excluded == 1
        assert ingest_stage.remaining == 4  # 5 total - 1 duplicate = 4 remaining
        assert "Duplicates are retained" in (ingest_stage.note or "")

    def test_ascii_renders_all_stages(self):
        """ASCII renderer produces output for all stages."""
        data = load_flowchart_data(self.sid_clean)
        text = render_ascii(data)
        lines = text.splitlines()
        assert "Assessed for eligibility" in text
        assert "Excluded at ingest" in text
        assert "Eligible cohort" in text
        assert "Allocated to each arm" in text
        assert "Analyzed" in text
        assert "Final analyzed" in text
        assert "N = 8" in text

    def test_svg_renders(self):
        """SVG renderer produces valid output with new layout."""
        data = load_flowchart_data(self.sid_clean)
        out = "/tmp/test_flowchart.svg"
        render_svg(data, out, show_title=True, show_watermark=False)
        svg = Path(out).read_text()
        assert "STROBE Participant Flow Diagram" in svg
        assert "Assessed for eligibility" in svg
        assert "Eligible cohort" not in svg  # collapsed into enrollment
        assert "Excluded" in svg
        assert "Duplicate patient ID (n = 0)" in svg
        assert "A (n = 4)" in svg  # raw arm label, no "Arm " prefix
        assert "B (n = 4)" in svg
        assert "Primary Endpoints (n = 4)" in svg  # two-line analyzed subtext
        assert "Final analyzed set" not in svg
        assert "Generated by" not in svg
        assert "test" not in svg
        Path(out).unlink(missing_ok=True)

    def test_svg_with_study_name(self):
        """Study name subtitle is opt-in via show_study_name."""
        data = load_flowchart_data(self.sid_clean)
        out = "/tmp/test_flowchart_studyname.svg"
        render_svg(data, out, show_title=True, show_study_name=True)
        svg = Path(out).read_text()
        assert "test" in svg  # dataset name in subtitle
        Path(out).unlink(missing_ok=True)

    def test_svg_with_watermark(self):
        """SVG watermark is opt-in."""
        data = load_flowchart_data(self.sid_clean)
        out = "/tmp/test_flowchart_wm.svg"
        render_svg(data, out, show_title=True, show_watermark=True)
        svg = Path(out).read_text()
        assert "Generated by" in svg
        Path(out).unlink(missing_ok=True)

    def test_svg_no_title(self):
        """SVG title is omittable."""
        data = load_flowchart_data(self.sid_clean)
        out = "/tmp/test_flowchart_notitle.svg"
        render_svg(data, out, show_title=False)
        svg = Path(out).read_text()
        assert "CONSORT Participant Flow Diagram" not in svg
        Path(out).unlink(missing_ok=True)

    def test_exclusion_with_nan_covariate(self):
        """When a row has missing covariate, analyzed < allocated per arm, both boxes render."""
        data = load_flowchart_data(self.sid_nan_cov)
        assert sum(data.arm_counts.values()) == 6
        arm_analyzed = data.arm_analyzed_counts
        assert arm_analyzed.get("A") == 3, f"Arm A analyzed is {arm_analyzed.get('A')}"
        assert arm_analyzed.get("B") == 2, f"Arm B analyzed is {arm_analyzed.get('B')}"
        assert sum(arm_analyzed.values()) == 5

        out = "/tmp/test_flowchart_nan.svg"
        render_svg(data, out, show_title=True)
        svg = Path(out).read_text()
        assert "Primary Endpoints (n = 3)" in svg
        assert "Primary Endpoints (n = 2)" in svg
        assert "Final analyzed set" in svg
        Path(out).unlink(missing_ok=True)

    def test_per_arm_analyzed_counts(self):
        """Per-arm analyzed counts sum to total and are not both equal to total."""
        data = load_flowchart_data(self.sid_clean)
        arm_col = data.arm_column
        assert arm_col is not None
        total_analyzed = data.stages[4].remaining  # global analyzed_n
        per_arm = data.arm_analyzed_counts
        assert sum(per_arm.values()) == total_analyzed, (
            f"per-arm sum {sum(per_arm.values())} != total {total_analyzed}"
        )
        for arm, cnt in per_arm.items():
            assert cnt != total_analyzed, (
                f"Arm {arm} analyzed count {cnt} equals total {total_analyzed} — not filtered by arm"
            )
        svg_out = "/tmp/test_flowchart_armcheck.svg"
        render_svg(data, svg_out, show_title=True)
        svg = Path(svg_out).read_text()
        for arm, cnt in per_arm.items():
            assert f"n = {cnt}" in svg, (
                f"SVG should contain 'n = {cnt}' for Arm {arm}'s analyzed box"
            )
        Path(svg_out).unlink(missing_ok=True)

    def test_arm_counts_match_plan(self):
        """Arm counts in flowchart match plan's treatment arm column."""
        data = load_flowchart_data(self.sid_clean)
        arm_stage = data.stages[3]
        assert arm_stage.details.get("arm_counts") is not None
        total_arms = sum(arm_stage.details["arm_counts"].values())
        assert total_arms == 8  # clean study has 8 patients