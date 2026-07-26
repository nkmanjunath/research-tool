"""Tests for forensics anomaly detection."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.database import DATA_ROOT, get_connection, init_db
from core.reporting.forensics import (
    _find_time_pairs,
    _is_benford_eligible,
    _first_digit_dist,
    _get_numeric_values,
    _check_impossible_timelines,
    run_forensics,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_study(study_id: str, rows: list[dict], col_info: dict | None = None):
    """Create a study with a raw table and variable metadata."""
    study_dir = DATA_ROOT / study_id
    if study_dir.exists():
        shutil.rmtree(study_dir)

    conn = get_connection(study_id)
    init_db(conn)

    conn.execute(
        """INSERT INTO studies (id, name, created_at, data_dir, is_locked, unmasked_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (study_id, "test", "2026-01-01T00:00:00+00:00", str(study_dir), 2,
         "2026-07-01T00:00:00+00:00"),
    )

    # Create raw table
    if rows:
        cols = list(rows[0].keys())
        col_defs = ", ".join(f'"{c}" TEXT' for c in cols)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS raw_{study_id} (
                "row_id" INTEGER PRIMARY KEY,
                {col_defs}
            )
        """)
        col_list = ", ".join(f'"{c}"' for c in cols)
        for r in rows:
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO raw_{study_id} ({col_list}) "
                f"VALUES ({placeholders})",
                [str(r[c]) for c in cols],
            )

    # Variable metadata
    if col_info:
        for col_name, info in col_info.items():
            conn.execute(
                """INSERT INTO variables (study_id, column_name, role, data_type, is_masked)
                   VALUES (?, ?, ?, ?, ?)""",
                (study_id, col_name, info.get("role", "baseline"),
                 info.get("data_type", "continuous"), info.get("is_masked", 0)),
            )

    conn.commit()
    conn.close()
    return study_dir


# ── Tests ────────────────────────────────────────────────────────────────────

class TestImpossibleTimelines:
    """Tier 1: PFS > OS detection."""

    def test_catches_pfs_exceeds_os(self):
        """A deliberately broken row where PFS > OS must be flagged."""
        study_id = "test_forensics_timeline_break"
        col_info = {
            "pfs_days": {"role": "outcome", "data_type": "time_to_event"},
            "os_days": {"role": "outcome", "data_type": "time_to_event"},
        }
        rows = [
            {"pfs_days": "100", "os_days": "500"},
            {"pfs_days": "600", "os_days": "200"},
            {"pfs_days": "300", "os_days": "400"},
        ]
        _make_study(study_id, rows, col_info)

        conn = get_connection(study_id)
        lines: list[str] = []
        _check_impossible_timelines(
            conn, f"raw_{study_id}", f"raw_masked_{study_id}",
            col_info, 3, True, lines,
        )
        conn.close()

        combined = "\n".join(lines)
        assert "⚠" in combined, "Should flag PFS > OS"
        assert "pfs_days=600" in combined or "600" in combined
        assert "os_days=200" in combined or "200" in combined

        shutil.rmtree(DATA_ROOT / study_id, ignore_errors=True)

    def test_no_false_positive_when_valid(self):
        """All PFS ≤ OS must not raise flags."""
        study_id = "test_forensics_timeline_ok"
        col_info = {
            "pfs_days": {"role": "outcome", "data_type": "time_to_event"},
            "os_days": {"role": "outcome", "data_type": "time_to_event"},
        }
        rows = [
            {"pfs_days": "100", "os_days": "500"},
            {"pfs_days": "200", "os_days": "300"},
            {"pfs_days": "50", "os_days": "600"},
        ]
        _make_study(study_id, rows, col_info)

        conn = get_connection(study_id)
        lines: list[str] = []
        _check_impossible_timelines(
            conn, f"raw_{study_id}", f"raw_masked_{study_id}",
            col_info, 3, True, lines,
        )
        conn.close()

        combined = "\n".join(lines)
        assert "✓" in combined
        assert "⚠" not in combined

        shutil.rmtree(DATA_ROOT / study_id, ignore_errors=True)

    def test_no_time_pair_returns_early(self):
        """No time_to_event columns must produce a clean skip."""
        col_info = {
            "age": {"role": "baseline", "data_type": "continuous"},
            "sex": {"role": "baseline", "data_type": "categorical"},
        }
        pairs = _find_time_pairs(col_info)
        assert pairs == []


class TestBenfordEligibility:
    """Tier 2: Benford's Law eligibility gate."""

    def test_eligible_multiple_orders(self):
        """Values spanning >2 orders with sufficient N are eligible."""
        vals = [10 ** (i / 10) * 100 for i in range(60)]  # 100 to ~1M
        eligible, reason = _is_benford_eligible(vals, len(vals))
        assert eligible, f"Should be eligible: {reason}"

    def test_refuses_bounded_column(self):
        """Bounded narrow-range column (<2 orders) must be rejected."""
        vals = [float(i) for i in range(40, 81)]  # 40–80, <2 orders
        eligible, reason = _is_benford_eligible(vals, len(vals))
        assert not eligible, f"Should be rejected: {reason}"
        assert "100" in reason or "order" in reason

    def test_refuses_small_n(self):
        """N < 30 must be rejected."""
        vals = [10.0, 50.0, 200.0, 800.0, 3000.0]
        eligible, reason = _is_benford_eligible(vals, len(vals))
        assert not eligible
        assert "Sample too small" in reason

    def test_refuses_negative_values(self):
        """Non-positive values must be rejected."""
        vals = [1.0, 10.0, 100.0, -5.0, 1000.0] * 10  # N=50, wide range
        eligible, reason = _is_benford_eligible(vals, len(vals))
        assert not eligible
        assert "non-positive" in reason

    def test_refuses_too_few_unique(self):
        """Too few unique values must be rejected."""
        vals = [10.0] * 5 + [100.0] * 5 + [200.0] * 5 + [500.0] * 5 + [1000.0] * 5
        eligible, reason = _is_benford_eligible(vals, len(vals))
        # N=25 is already too small, so this will fail the N check
        assert not eligible

    def test_marginal_n_below_50_caveated(self):
        """30 ≤ N < 50 should be eligible but caveated."""
        vals = [10 ** (i / 10) * 10 for i in range(40)]  # 10–10^4, 40 vals
        eligible, reason = _is_benford_eligible(vals, len(vals))
        assert eligible, f"Should be eligible: {reason}"
        assert "Marginal" in reason


class TestFullReportOnSynthetic:
    """Full report against synthetic_21.csv constraints."""

    def test_synthetic_21_caveats_benford(self):
        """synthetic_21.csv has N=21 — Benford section must caveat sample size."""
        # Build the synthetic_21 schema
        col_info = {
            "patient_id": {"role": "baseline", "data_type": "categorical"},
            "age": {"role": "baseline", "data_type": "continuous"},
            "sex": {"role": "baseline", "data_type": "categorical"},
            "iss_stage": {"role": "baseline", "data_type": "categorical"},
            "prior_lines": {"role": "baseline", "data_type": "continuous"},
            "high_risk_fish": {"role": "baseline", "data_type": "categorical"},
            "treatment_arm": {"role": "baseline", "data_type": "categorical"},
            "response_category": {"role": "outcome", "data_type": "categorical"},
            "pfs_days": {"role": "outcome", "data_type": "time_to_event"},
            "pfs_event": {"role": "outcome", "data_type": "categorical"},
            "os_days": {"role": "outcome", "data_type": "time_to_event"},
            "os_event": {"role": "outcome", "data_type": "categorical"},
        }
        rows = [
            {"patient_id": f"SUBJ_{i:03d}", "age": str(40 + (i * 2) % 44),
             "sex": "M" if i % 2 == 0 else "F",
             "iss_stage": ["I", "II", "III"][i % 3],
             "prior_lines": str(i % 5),
             "high_risk_fish": "yes" if i % 3 == 0 else "no",
             "treatment_arm": "A" if i % 2 == 0 else "B",
             "response_category": ["CR", "PR", "SD", "PD"][i % 4],
             "pfs_days": str(30 + i * 30),
             "pfs_event": "1" if i % 3 != 0 else "0",
             "os_days": str(30 + i * 60),
             "os_event": "1" if i % 2 == 0 else "0"}
            for i in range(21)
        ]
        study_id = "test_forensics_synth21"
        _make_study(study_id, rows, col_info)

        report = run_forensics(study_id)
        assert report.exists()
        text = report.read_text()

        # Benford column should say "too small"
        assert "Sample too small" in text
        # Should still produce a complete report
        assert "Forensics Report" in text
        assert "Tier 1" in text
        assert "Tier 2" in text
        assert "## Summary" in text or "Summary of Checks" in text

        shutil.rmtree(DATA_ROOT / study_id, ignore_errors=True)


class TestOutOfRange:
    """Tier 1: out-of-range value detection."""

    def test_age_out_of_range(self):
        """Age > 120 must be flagged."""
        study_id = "test_forensics_oor"
        col_info = {
            "age": {"role": "baseline", "data_type": "continuous"},
        }
        rows = [{"age": str(v)} for v in [45, 130, 80, -5, 55]]
        _make_study(study_id, rows, col_info)

        report = run_forensics(study_id)
        text = report.read_text()
        assert "⚠" in text
        assert "age" in text
        assert "130" in text or "130" in text
        assert "-5" in text

        shutil.rmtree(DATA_ROOT / study_id, ignore_errors=True)


class TestDuplicates:
    """Tier 1: duplicate detection."""

    def test_exact_duplicate_flagged(self):
        study_id = "test_forensics_dup_exact"
        col_info = {
            "patient_id": {"role": "baseline", "data_type": "categorical"},
            "age": {"role": "baseline", "data_type": "continuous"},
            "sex": {"role": "baseline", "data_type": "categorical"},
        }
        rows = [
            {"patient_id": "A", "age": "50", "sex": "M"},
            {"patient_id": "A", "age": "50", "sex": "M"},
        ]
        _make_study(study_id, rows, col_info)

        report = run_forensics(study_id)
        text = report.read_text()
        assert "⚠" in text
        assert "exact duplicate" in text

        shutil.rmtree(DATA_ROOT / study_id, ignore_errors=True)

    def test_near_duplicate_conflict_flagged(self):
        study_id = "test_forensics_dup_near"
        col_info = {
            "age": {"role": "baseline", "data_type": "continuous"},
            "sex": {"role": "baseline", "data_type": "categorical"},
        }
        rows = [
            {"patient_id": "A", "age": "50", "sex": "M"},
            {"patient_id": "A", "age": "51", "sex": "M"},
        ]
        _make_study(study_id, rows, col_info)

        report = run_forensics(study_id)
        text = report.read_text()
        assert "conflicting" in text
        assert "age" in text

        shutil.rmtree(DATA_ROOT / study_id, ignore_errors=True)
