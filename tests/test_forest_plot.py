from __future__ import annotations

import math
import shutil
from pathlib import Path

from core.database import DATA_ROOT, get_connection, init_db
from core.reporting.forest_plot import (
    CovariateRow,
    ForestPlotData,
    load_forest_data,
    render_svg,
    render_ascii,
    _extract_epv,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_study(study_id: str, covariates: list[dict],
                concordance: float = 0.72, lr_p: float = 0.01,
                n: int = 200, epv_text: str | None = None):
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
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, computed_at, concordance_index, lr_test_p,
            is_pre_registered, study_plan_version, status_json,
            sample_counts_json, provenance_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (study_id, "cox_ph_model", "2026-07-01T00:00:00+00:00",
         concordance, lr_p, 1, 1,
         '{"status": "completed"}',
         f'{{"n_total": {n}, "n_analyzed": {n}, "n_excluded": 0}}',
         '{}'),
    )
    result_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for i, c in enumerate(covariates):
        conn.execute(
            """INSERT INTO analysis_covariate_results
               (result_id, covariate, hr, ci_lower, ci_upper, wald_p, coef, se, z,
                reference_level, tested_level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result_id, c["covariate"], c["hr"], c["ci_lower"], c["ci_upper"],
             c["wald_p"], c.get("coef"), c.get("se"), c.get("z"),
             c.get("reference_level"), c.get("tested_level")),
        )
    conn.commit()
    conn.close()

    # Write plan with EPV warning
    plan = {}
    if epv_text:
        plan["warnings"] = {"cox_model": epv_text}
    import json
    (study_dir / "study_plan.v1.locked.json").write_text(json.dumps(plan))

    return study_id, result_id


# ── Tests ────────────────────────────────────────────────────────────────────

class TestExtractEPV:
    def test_extracts_epv_from_warning(self):
        epv, warn = _extract_epv('{"test": "EPV=2.5"}')
        assert epv == 2.5
        assert warn is True

    def test_high_epv_no_warning(self):
        epv, warn = _extract_epv('{"test": "EPV=25.0"}')
        assert epv == 25.0
        assert warn is False

    def test_no_epv_string(self):
        epv, warn = _extract_epv('{"test": "no epv here"}')
        assert epv is None
        assert warn is False

    def test_none_input(self):
        epv, warn = _extract_epv(None)
        assert epv is None
        assert warn is False


class TestCovariateRowProperties:
    def test_ci_crosses_one_detection(self):
        def make(hr, lo, hi, p):
            return CovariateRow("x", hr, lo, hi, p, coef=None, se=None, z=None)
        assert make(1.5, 0.8, 2.2, 0.1).ci_crosses_one is True    # crosses 1
        assert make(2.5, 1.2, 4.0, 0.01).ci_crosses_one is False  # entirely above 1
        assert make(0.5, 0.3, 0.9, 0.02).ci_crosses_one is False  # entirely below 1
        assert make(0.5, 0.3, 1.0, 0.01).ci_crosses_one is True   # upper bound = 1
        assert make(1.0, 0.8, 1.2, 0.05).ci_crosses_one is True   # HR exactly 1
        assert make(0.8, 0.6, 1.2, 0.1).ci_crosses_one is True    # crosses 1
        assert make(0.4, 0.3, 0.5, 0.001).ci_crosses_one is False # entirely below 1

    def test_unstable_flag(self):
        c = CovariateRow("x", 0, 0, 0, 1.0, coef=None, se=None, z=None, unstable=True)
        assert c.unstable is True


class TestAgainstRealStudy:
    """Test against a0368cf06b4444ca8a4118704b1edd2f — real data, no hardcoding."""

    def test_loads_four_covariates(self):
        """Must load exactly 4 covariates with correct display labels."""
        data = load_forest_data("a0368cf06b4444ca8a4118704b1edd2f")
        assert len(data.covariates) == 4
        labels = [c.display_label for c in data.covariates]
        assert any("treatment_arm" in l for l in labels)
        assert any("age" in l for l in labels)
        assert any("high_risk_fish" in l for l in labels)
        assert any("prior_lines" in l for l in labels)

    def test_values_match_db(self):
        """HR/CI/p values must match DB exactly — pull and compare."""
        data = load_forest_data("a0368cf06b4444ca8a4118704b1edd2f")
        conn = get_connection("a0368cf06b4444ca8a4118704b1edd2f")
        rows = conn.execute(
            "SELECT covariate, hr, ci_lower, ci_upper, wald_p, reference_level, tested_level "
            "FROM analysis_covariate_results "
            "WHERE result_id=(SELECT id FROM analysis_results "
            "  WHERE study_id=? AND test_name='cox_ph_model' "
            "  AND id NOT IN (SELECT COALESCE(superseded_previous_result_id, -1) "
            "    FROM analysis_results WHERE study_id=? AND test_name='cox_ph_model' "
            "    AND superseded_previous_result_id IS NOT NULL) "
            "  ORDER BY id DESC LIMIT 1)",
            ("a0368cf06b4444ca8a4118704b1edd2f",) * 2,
        ).fetchall()
        conn.close()
        db_map = {r["covariate"]: r for r in rows}
        for c in data.covariates:
            db = db_map[c.covariate]
            assert c.hr == db["hr"]
            assert c.ci_lower == db["ci_lower"]
            assert c.ci_upper == db["ci_upper"]
            assert c.wald_p == db["wald_p"]
            assert c.reference_level == db["reference_level"]
            assert c.tested_level == db["tested_level"]

    def test_epv_caveat_present(self):
        """Study a0368cf has EPV=2.5 — report must carry the caveat."""
        data = load_forest_data("a0368cf06b4444ca8a4118704b1edd2f")
        assert data.epv_warning is True
        assert data.epv is not None
        assert data.epv < 10

    def test_all_cis_cross_one(self):
        """All 4 covariates in this study have CI crossing 1."""
        data = load_forest_data("a0368cf06b4444ca8a4118704b1edd2f")
        for c in data.covariates:
            assert c.ci_crosses_one, f"{c.covariate} CI does not cross 1"

    def test_svg_renders_with_epv_caveat(self):
        """SVG output must contain the EPV caveat text."""
        data = load_forest_data("a0368cf06b4444ca8a4118704b1edd2f")
        out = "/tmp/test_forest_real.svg"
        render_svg(data, out)
        svg = Path(out).read_text()
        assert "EPV=2.5" in svg
        assert "Caution" in svg
        assert "unstable" in svg
        Path(out).unlink(missing_ok=True)

    def test_ascii_renders_with_epv_caveat(self):
        """ASCII output must contain the EPV caveat text."""
        data = load_forest_data("a0368cf06b4444ca8a4118704b1edd2f")
        text = render_ascii(data)
        assert "EPV=2.5" in text
        assert "Caution" in text


class TestHighEPVNoCaveat:
    """Constructed study with high EPV — caveat must NOT appear."""

    def setup_method(self):
        self.sid = "test_forest_high_epv"
        covariates = [
            {"covariate": "treatment_arm", "hr": 2.0, "ci_lower": 1.2,
             "ci_upper": 3.3, "wald_p": 0.008, "coef": 0.69, "se": 0.26, "z": 2.65,
             "reference_level": "A", "tested_level": "B"},
            {"covariate": "age", "hr": 1.05, "ci_lower": 1.01,
             "ci_upper": 1.09, "wald_p": 0.015, "coef": 0.05, "se": 0.02, "z": 2.43},
        ]
        _make_study(self.sid, covariates, n=500,
                    epv_text="EPV=50.0 — adequate")
        self.data = load_forest_data(self.sid)

    def teardown_method(self):
        shutil.rmtree(DATA_ROOT / self.sid, ignore_errors=True)

    def test_epv_warning_false(self):
        assert self.data.epv_warning is False
        assert self.data.epv == 50.0

    def test_svg_no_caveat(self):
        out = "/tmp/test_forest_high_epv.svg"
        render_svg(self.data, out)
        svg = Path(out).read_text()
        assert "Caution" not in svg
        Path(out).unlink(missing_ok=True)

    def test_ascii_no_caveat(self):
        text = render_ascii(self.data)
        assert "Caution" not in text

    def test_some_cis_dont_cross_one(self):
        assert self.data.covariates[0].ci_crosses_one is False
        assert self.data.covariates[1].ci_crosses_one is False

    def test_concordance_loaded(self):
        assert self.data.concordance_index == 0.72


class TestUnstableCovariate:
    """Row with inf/nan HR should render as 'did not converge'."""

    def setup_method(self):
        self.sid = "test_forest_unstable"
        covariates = [
            {"covariate": "stable_var", "hr": 1.5, "ci_lower": 0.8,
             "ci_upper": 2.8, "wald_p": 0.2, "coef": 0.4, "se": 0.3, "z": 1.33,
             "reference_level": "A", "tested_level": "B"},
            {"covariate": "unstable_var", "hr": float("inf"), "ci_lower": 0.5,
             "ci_upper": float("inf"), "wald_p": 0.5, "coef": None, "se": None, "z": None,
             "reference_level": "A", "tested_level": "B"},
        ]
        _make_study(self.sid, covariates, epv_text="EPV=25.0")
        self.data = load_forest_data(self.sid)

    def teardown_method(self):
        shutil.rmtree(DATA_ROOT / self.sid, ignore_errors=True)

    def test_unstable_flagged(self):
        assert self.data.covariates[1].unstable is True

    def test_stable_not_flagged(self):
        assert self.data.covariates[0].unstable is False

    def test_svg_shows_unstable_text(self):
        out = "/tmp/test_forest_unstable.svg"
        render_svg(self.data, out)
        svg = Path(out).read_text()
        assert "did not converge" in svg.lower()
        Path(out).unlink(missing_ok=True)



class TestWideHRAlignment:
    """When HR or CI bounds are ≥10.00, columns must stay aligned."""

    def setup_method(self):
        self.sid = "test_forest_wide_hr"
        covariates = [
            {
                "covariate": "treatment_arm",
                "hr": 12.45, "ci_lower": 1.02, "ci_upper": 145.30,
                "wald_p": 0.041, "coef": 2.52, "se": 1.20, "z": 2.10,
                "reference_level": "A", "tested_level": "B",
            },
            {
                "covariate": "age",
                "hr": 1.01, "ci_lower": 0.98, "ci_upper": 1.04,
                "wald_p": 0.33, "coef": 0.01, "se": 0.01, "z": 0.98,
            },
        ]
        _make_study(self.sid, covariates, n=500, epv_text="EPV=50.0")
        self.data = load_forest_data(self.sid)

    def teardown_method(self):
        shutil.rmtree(DATA_ROOT / self.sid, ignore_errors=True)

    def test_columns_aligned(self):
        """CI brackets must start at the same character position on every row."""
        text = render_ascii(self.data)
        lines = text.splitlines()
        # Skip header row which contains "aHR [95% CI]"
        data_lines = [l for l in lines if "[" in l and "Covariate" not in l]
        assert len(data_lines) == 2
        ci_starts = [l.index("[") for l in data_lines]
        assert ci_starts[0] == ci_starts[1], \
            f"CI brackets misaligned: {ci_starts}"

    def test_header_data_column_alignment(self):
        """Header columns (aHR, p-value) must align exactly with data row columns."""
        text = render_ascii(self.data)
        lines = text.splitlines()
        # Find header row (has "aHR" and "p-value")
        header = next(l for l in lines if "aHR" in l and "p-value" in l)
        # Find first data row (has "p=" and CI bracket)
        data_row = next(l for l in lines if "p=" in l and "[" in l)
        # Check alignment of key column positions
        # HR column: header "aHR" right-edge aligns with data HR numeric right-edge (both in 7-char field)
        # CI bracket: header "[95% CI]" left-edge aligns with data "[" left-edge
        assert header.index("[95% CI]") == data_row.index("[")
        # p-value header aligns with p= field
        assert header.index("p-value") == data_row.index("p=")

    def test_ref_line_at_computed_position(self):
        """The │ reference line must sit at exactly the character index _pos(1.0) computes."""
        text = render_ascii(self.data)
        lines = text.splitlines()
        # Find the axis tick line (has │ at tick positions including value 1.0).
        # Locate the │ that sits at the 1.0 tick position by searching the scale header structure.
        # The second │ after name_w chars marks the ref position.
        tick_line = [l for l in lines if "│" in l and "─" in l and "Forest" not in l
                     and "C-index" not in l and "EPV" not in l and "⚠" not in l
                     and "**" not in l and not l.startswith("─")][0]
        # The plot area starts after label column + separator │, at index name_w + 1.
        # The ref │ is somewhere in the tick line — find it as the ─┬ at the tick for 1.0,
        # then locate the │ immediately after it.
        # Tick marker for 1.0: the tick text "1" follows a "┬" character placed at the ref x.
        # In the scale header: "...┬" then below "...│" — the │ under each ┬ is at the same index.
        # So find the │ that's below "┬" in the scale header — that's the ref pos.
        # Easier: find the separator line ──┬── (which has ┬ at each tick position).
        # Actually, the simplest: compute lo/hi like render_ascii does, derive ref,
        # then assert │ in a data row is at name_w + 1 + ref.
        all_vals = [1.0]
        for c in self.data.covariates:
            if not c.unstable and c.hr > 0 and c.ci_lower > 0 and c.ci_upper > 0:
                all_vals.extend([c.hr, c.ci_lower, c.ci_upper])
        lo = min(all_vals) / 1.3
        hi = max(all_vals) * 1.3
        if lo <= 0:
            lo = 0.1
        plot_w = 30
        ref = int((math.log(1.0) - math.log(lo)) / (math.log(hi) - math.log(lo)) * plot_w)
        for l in lines:
            if "[" in l and "p=" in l:  # data row
                label_part = l.split("│")[0]
                # │ in data row is at index len(label_part)
                assert l.index("│", len(label_part)) == len(label_part)
                # Within the plot, ref is at index ref (0-based in 30-char buf).
                # So │ appears at len(label_part) + 1 (│ sep) + ref within plot... no.
                # Actually │ IS at len(label_part) — it's the label separator at position name_w+0.
                # The ref line │ inside the plot area is at a later index.
                # data_line format: {label}│{plot_buf}  {hr} {ci} {p}
                # The second │ in the line (reference line inside plot) is what we want.
                rest = l[len(label_part) + 1:]  # after first │
                ref_char_idx = rest.index("│")
                # ref_char_idx in rest should equal ref (plot_buf index)
                assert ref_char_idx == ref, \
                    f"ref │ at plot index {ref_char_idx}, expected {ref} (lo={lo:.2f}, hi={hi:.2f})"
