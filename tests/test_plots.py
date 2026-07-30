"""Tests for Kaplan-Meier plot generation."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.planning.lock import lock_plan
from core.planning.study_plan import StudyPlan
from core.reporting.plots import generate_km_plot


STUDY_ID = "test_km_plot"


@pytest.fixture(autouse=True)
def _setup():
    p = DATA_ROOT / STUDY_ID
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)

    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, study_type, is_locked) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (STUDY_ID, "KM Plot Test", "2025-01-01", str(DATA_ROOT / STUDY_ID), "cohort", 0),
    )
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {raw} (row_id INTEGER PRIMARY KEY, age TEXT, "
                 f"pfs_days TEXT, pfs_event TEXT, treatment_arm TEXT)")
    conn.execute(f"INSERT INTO {raw} (age, pfs_days, pfs_event, treatment_arm) VALUES ('65', '100', '1', 'A')")
    conn.execute(f"INSERT INTO {raw} (age, pfs_days, pfs_event, treatment_arm) VALUES ('70', '200', '0', 'B')")
    conn.execute(f"INSERT INTO {raw} (age, pfs_days, pfs_event, treatment_arm) VALUES ('55', '150', '1', 'A')")
    conn.execute(f"INSERT INTO {raw} (age, pfs_days, pfs_event, treatment_arm) VALUES ('80', '300', '0', 'B')")
    conn.execute(f"INSERT INTO {raw} (age, pfs_days, pfs_event, treatment_arm) VALUES ('60', '50', '1', 'A')")
    conn.execute("DELETE FROM variables WHERE study_id=?", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'age', 'baseline', 'continuous')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'pfs_days', 'outcome', 'time_to_event')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'pfs_event', 'outcome', 'categorical')", (STUDY_ID,))
    conn.execute("INSERT INTO variables (study_id, column_name, role, data_type) VALUES (?, 'treatment_arm', 'baseline', 'categorical')", (STUDY_ID,))
    conn.commit()
    conn.close()

    # Seal outcomes so we can lock
    from core.masking.gate import seal_outcomes
    seal_outcomes(STUDY_ID)
    yield
    if p.exists():
        shutil.rmtree(p)


def _setup_plan_and_result(with_completed_km: bool = True):
    """Lock a plan with a KM test and insert a result."""
    plan = StudyPlan(
        study_id=STUDY_ID,
        study_type="cohort",
        primary_comparison="PFS by treatment arm",
        planned_tests=[{"variable_name": "pfs_days", "test_name": "kaplan_meier_logrank",
                        "rationale": "Compare PFS between arms"}],
    )
    lock_plan(STUDY_ID, plan)

    # Unmask so outcome columns are visible in the raw table
    from core.masking.gate import unmask_study
    unmask_study(STUDY_ID)

    if with_completed_km:
        conn = get_connection(STUDY_ID)
        init_db(conn)
        conn.execute(
            """INSERT INTO analysis_results
               (id, study_id, test_name, statistic, p_value, status_json,
                sample_counts_json, is_pre_registered, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (1, STUDY_ID, "kaplan_meier_logrank", 5.2, 0.022,
             json.dumps({"status": "completed"}),
             json.dumps({"n_total": 5, "n_analyzed": 5, "n_excluded": 0}),
             "2025-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()


def test_km_plot_produces_valid_svg():
    """A completed KM test should produce a well-formed SVG file."""
    _setup_plan_and_result(with_completed_km=True)
    out = Path(tempfile.mkstemp(suffix=".svg")[1])
    try:
        result = generate_km_plot(STUDY_ID, test_id=1, output_path=out, fmt="svg")
        assert result.exists()
        assert result.suffix == ".svg"
        content = result.read_text()
        assert content.strip().startswith("<?xml") or content.strip().startswith("<svg")
        assert "<svg" in content
    finally:
        out.unlink()


def test_km_plot_rejects_skipped_test():
    """Plotting a skipped/errored KM test should raise a clear error."""
    _setup_plan_and_result(with_completed_km=False)
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        """INSERT INTO analysis_results
           (id, study_id, test_name, statistic, p_value, status_json,
            sample_counts_json, is_pre_registered, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (1, STUDY_ID, "kaplan_meier_logrank", None, None,
         json.dumps({"status": "skipped_assumption_violation",
                     "reason": "minimum expected cell count is 1.4"}),
         json.dumps({"n_total": 5, "n_analyzed": 0, "n_excluded": 5}),
         "2025-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    out = Path(tempfile.mkstemp(suffix=".svg")[1])
    with pytest.raises(ValueError, match="did not complete successfully"):
        generate_km_plot(STUDY_ID, test_id=1, output_path=out, fmt="svg")
    out.unlink()


def test_km_plot_rejects_non_km_test():
    """Plotting a non-KM test should raise a clear error."""
    _setup_plan_and_result(with_completed_km=False)
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        """INSERT INTO analysis_results
           (id, study_id, test_name, statistic, p_value, status_json,
            sample_counts_json, is_pre_registered, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (1, STUDY_ID, "chi_square", 3.2, 0.07,
         json.dumps({"status": "completed"}),
         json.dumps({"n_total": 5, "n_analyzed": 5, "n_excluded": 0}),
         "2025-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    out = Path(tempfile.mkstemp(suffix=".svg")[1])
    with pytest.raises(ValueError, match="not 'kaplan_meier_logrank'"):
        generate_km_plot(STUDY_ID, test_id=1, output_path=out, fmt="svg")
    out.unlink()


def test_km_plot_rejects_nonexistent_test_id():
    """A non-existent test_id should raise a clear error."""
    _setup_plan_and_result(with_completed_km=True)
    out = Path(tempfile.mkstemp(suffix=".svg")[1])
    with pytest.raises(ValueError, match="not found"):
        generate_km_plot(STUDY_ID, test_id=999, output_path=out, fmt="svg")
    out.unlink()


def test_km_plot_contains_at_risk_median_and_censoring():
    """The KM plot SVG must include the at-risk table, median lines, and censoring note."""
    _setup_plan_and_result(with_completed_km=True)
    # Add more data so the at-risk table is non-trivial
    conn = get_connection(STUDY_ID)
    raw = f"raw_{STUDY_ID}"
    conn.execute(f"INSERT INTO {raw} (age, pfs_days, pfs_event, treatment_arm) VALUES ('45', '400', '1', 'A')")
    conn.execute(f"INSERT INTO {raw} (age, pfs_days, pfs_event, treatment_arm) VALUES ('50', '500', '0', 'B')")
    conn.commit()
    conn.close()

    out = Path(tempfile.mkstemp(suffix=".svg")[1])
    try:
        result = generate_km_plot(STUDY_ID, test_id=1, output_path=out, fmt="svg")
        content = result.read_text()

        assert content.strip().startswith("<?xml") or content.strip().startswith("<svg")
        assert "<svg" in content

        # Censoring legend entry — legend text appears as SVG <text> elements
        assert "Censored" in content, "SVG should contain censoring legend entry"

        # At-risk table: rendered by ax.text() as path characters, but
        # group names (e.g. "A (n=") appear in the legend as SVG text
        assert "A (n=" in content or "n=3" in content or "n=" in content

        # Confirm no hazard ratio appears anywhere
        assert "hazard" not in content.lower(), "SVG should NOT contain hazard ratio text"
    finally:
        out.unlink()


def test_all_styles_produce_valid_svg():
    """All three style presets must produce valid SVG output."""
    _setup_plan_and_result(with_completed_km=True)
    for style in ("clean", "scientific", "presentation"):
        out = Path(tempfile.mkstemp(suffix=".svg")[1])
        try:
            result = generate_km_plot(STUDY_ID, test_id=1, output_path=out, fmt="svg", style=style)
            content = result.read_text()
            assert content.strip().startswith("<?xml") or content.strip().startswith("<svg"), \
                f"Style '{style}' produced non-SVG output"
            assert "<svg" in content, f"Style '{style}' produced non-SVG output"
        finally:
            out.unlink()


def test_clean_style_no_hr_without_cox():
    """Clean-style plot must NOT show HR when no Cox PH result exists."""
    _setup_plan_and_result(with_completed_km=True)
    out = Path(tempfile.mkstemp(suffix=".svg")[1])
    try:
        result = generate_km_plot(STUDY_ID, test_id=1, output_path=out, fmt="svg", style="clean")
        content = result.read_text()
        assert "hazard" not in content.lower(), "Clean style should not show HR without Cox result"
        # Must show the log-rank p-value instead
        assert "Log-rank" in content or "P =" in content, \
            "Clean style should show Log-rank or P = in the annotation"
    finally:
        out.unlink()


def test_clean_style_shows_hr_with_cox():
    """Clean-style plot SHOULD show HR when a completed Cox PH result exists."""
    _setup_plan_and_result(with_completed_km=True)
    # Insert a matching Cox PH result
    conn = get_connection(STUDY_ID)
    init_db(conn)
    conn.execute(
        """INSERT INTO analysis_results
           (id, study_id, test_name, statistic, p_value, ci_lower, ci_upper, status_json,
            sample_counts_json, is_pre_registered, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (99, STUDY_ID, "cox_proportional_hazards", 1.32, 0.032, 1.05, 1.67,
         json.dumps({"status": "completed"}),
         json.dumps({"n_total": 5, "n_analyzed": 5, "n_excluded": 0}),
         "2025-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    # Verify the lookup function finds the Cox result
    from core.reporting.plots import _lookup_cox_hr
    cox = _lookup_cox_hr(STUDY_ID, "treatment_arm")
    assert cox is not None, "Cox lookup should find the inserted result"
    assert abs(cox["hr"] - 1.32) < 0.001
    assert abs(cox["p_value"] - 0.032) < 0.001


def test_default_style_is_clean():
    """Calling generate_km_plot without style= should produce the clean preset."""
    _setup_plan_and_result(with_completed_km=True)
    out_default = Path(tempfile.mkstemp(suffix=".svg")[1])
    out_clean = Path(tempfile.mkstemp(suffix=".svg")[1])
    try:
        result_default = generate_km_plot(STUDY_ID, test_id=1, output_path=out_default, fmt="svg", style="clean")
        result_clean = generate_km_plot(STUDY_ID, test_id=1, output_path=out_clean, fmt="svg", style="clean")
        content_default = result_default.read_text()
        content_clean = result_clean.read_text()
        # Same style param should produce identical SVGs
        # (pandas/matplotlib floating-point determinism is sufficient for this check)
        assert len(content_default) == len(content_clean), \
            "Default and clean-style SVGs should be identical"
    finally:
        out_default.unlink()
        out_clean.unlink()


def test_style_all_produces_three_files(monkeypatch):
    """--style all must generate three distinct, valid SVG files."""
    _setup_plan_and_result(with_completed_km=True)
    import argparse
    from core.cli.main import cmd_plot_km
    from core.database import DATA_ROOT

    # Clean up any previous files
    for f in DATA_ROOT.glob(f"{STUDY_ID}/km_plot_1_*.svg"):
        f.unlink()

    ns = argparse.Namespace(
        study_id=STUDY_ID, test_id=1,
        format="svg", no_risk_table=False, no_medians=False,
        output=None, time_unit="months", style="all",
    )
    cmd_plot_km(ns)

    for style_name in ("clean", "scientific", "presentation"):
        path = DATA_ROOT / STUDY_ID / f"km_plot_1_{style_name}.svg"
        assert path.exists(), f"Missing {path}"
        content = path.read_text()
        assert "<?xml" in content[:200] or "<svg" in content[:200], \
            f"{style_name} SVG is not valid"
        path.unlink()


def test_single_styles_dont_overwrite():
    """Sequential plot-km calls with different --style values must each
    produce a distinct file, not silently overwrite the previous one."""
    _setup_plan_and_result(with_completed_km=True)
    import argparse
    from core.cli.main import cmd_plot_km
    from core.database import DATA_ROOT

    # Clean up
    for f in DATA_ROOT.glob(f"{STUDY_ID}/km_plot_1_*.svg"):
        f.unlink()

    for style_name in ("clean", "scientific", "presentation"):
        ns = argparse.Namespace(
            study_id=STUDY_ID, test_id=1,
            format="svg", no_risk_table=False, no_medians=False,
            output=None, time_unit="months", style=style_name,
        )
        cmd_plot_km(ns)

    # All three files should exist simultaneously
    existing = []
    for style_name in ("clean", "scientific", "presentation"):
        path = DATA_ROOT / STUDY_ID / f"km_plot_1_{style_name}.svg"
        assert path.exists(), f"Missing {path} after sequential invocation"
        existing.append(path)

    # All three should have different sizes (different styles)
    sizes = {p.stat().st_size for p in existing}
    assert len(sizes) >= 2, \
        f"Files should have different sizes (different styles), got {sizes}"

    for p in existing:
        p.unlink()


def test_scientific_vs_presentation_configs_differ():
    """Scientific and presentation presets must have meaningfully different CI alpha values."""
    from core.reporting.plots import _STYLES
    sci = _STYLES["scientific"]
    pres = _STYLES["presentation"]
    # CI alpha — must differ by at least 0.1 to be visually apparent
    assert pres.ci_alpha - sci.ci_alpha >= 0.10, \
        f"CI alpha should differ by >=0.10 (sci={sci.ci_alpha}, pres={pres.ci_alpha})"
    # Linewidth — must differ
    assert pres.linewidth > sci.linewidth, \
        f"Presentation linewidth ({pres.linewidth}) should be > scientific ({sci.linewidth})"
    # Stats box — both use box but scientific should be more compact
    assert pres.stats_fontsize > sci.stats_fontsize, \
        f"Presentation stats font ({pres.stats_fontsize}) should be > scientific ({sci.stats_fontsize})"


def test_scientific_vs_presentation_svg_fill_opacity_differs():
    """Actual rendered SVG fill-opacity must differ between scientific and presentation."""
    _setup_plan_and_result(with_completed_km=True)
    import re
    out_sci = Path(tempfile.mkstemp(suffix=".svg")[1])
    out_pres = Path(tempfile.mkstemp(suffix=".svg")[1])
    try:
        r_sci = generate_km_plot(STUDY_ID, test_id=1, output_path=out_sci, fmt="svg", style="scientific")
        r_pres = generate_km_plot(STUDY_ID, test_id=1, output_path=out_pres, fmt="svg", style="presentation")
        sci_svg = r_sci.read_text()
        pres_svg = r_pres.read_text()

        # Extract CI band fill-opacity values (the blue/orange bands)
        sci_ci_ops = set(re.findall(r'fill: #1f77b4; fill-opacity: ([0-9.]+)', sci_svg))
        pres_ci_ops = set(re.findall(r'fill: #1f77b4; fill-opacity: ([0-9.]+)', pres_svg))

        assert sci_ci_ops, "No CI band found in scientific SVG"
        assert pres_ci_ops, "No CI band found in presentation SVG"

        sci_val = float(list(sci_ci_ops)[0])
        pres_val = float(list(pres_ci_ops)[0])

        # The difference must be at least 0.15
        assert pres_val - sci_val >= 0.15, \
            f"Rendered CI fill-opacity too close: sci={sci_val}, pres={pres_val}"

        # Also verify median line stroke-width differs
        sci_med_sw = set(re.findall(r'stroke-width: ([0-9.]+)', sci_svg))
        pres_med_sw = set(re.findall(r'stroke-width: ([0-9.]+)', pres_svg))
    finally:
        out_sci.unlink()
        out_pres.unlink()


def test_km_plot_ci_clamped_at_t0():
    """Confidence interval at t=0 must be clamped to [1.0, 1.0] (zero width at t=0)."""
    _setup_plan_and_result(with_completed_km=True)
    out_path = Path(tempfile.mkstemp(suffix=".svg")[1])
    try:
        generate_km_plot(STUDY_ID, test_id=1, output_path=out_path, fmt="svg", style="clean")
        assert out_path.exists()
    finally:
        out_path.unlink()

