"""Tests for provenance lineage assembly and rendering."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.database import DATA_ROOT, get_connection, init_db
from core.reporting.lineage import assemble_events, render_text, render_svg


def _setup_minimal_study(study_id: str) -> None:
    """Create a minimal study: ingest + plan lock only."""
    study_dir = DATA_ROOT / study_id
    if study_dir.exists():
        shutil.rmtree(study_dir)

    conn = get_connection(study_id)
    init_db(conn)
    conn.execute(
        """INSERT INTO studies (id, name, created_at, data_dir, is_locked)
           VALUES (?, ?, ?, ?, ?)""",
        (study_id, "Minimal study", "2026-01-01T00:00:00.000000+00:00",
         str(study_dir), 1),
    )
    conn.execute(
        """INSERT INTO variables (study_id, column_name, role, data_type)
           VALUES (?, ?, ?, ?)""",
        (study_id, "age", "baseline", "continuous"),
    )
    conn.commit()

    # Write plan lock file
    plan_v1 = {
        "study_id": study_id,
        "version": 1,
        "locked_at": "2026-01-02T00:00:00.000000+00:00",
        "content_hash": "abc123def456",
    }
    lock_path = study_dir / "study_plan.v1.locked.json"
    lock_path.write_text(json.dumps(plan_v1))

    conn.close()


def _setup_full_lifecycle(study_id: str) -> int:
    """Create a study with full lifecycle: ingest→lock→amend→unmask→post-hoc→analyze→rerun→bundle.

    Returns the ID of the first analysis result (for supersession).
    """
    study_dir = DATA_ROOT / study_id
    if study_dir.exists():
        shutil.rmtree(study_dir)

    conn = get_connection(study_id)
    init_db(conn)
    conn.execute(
        """INSERT INTO studies (id, name, created_at, data_dir, is_locked, unmasked_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (study_id, "Full lifecycle study", "2026-01-01T00:00:00.000000+00:00",
         str(study_dir), 2, "2026-03-01T00:00:00.000000+00:00"),
    )
    conn.execute(
        """INSERT INTO variables (study_id, column_name, role, data_type)
           VALUES (?, ?, ?, ?)""",
        (study_id, "age", "baseline", "continuous"),
    )
    conn.commit()

    # Plan v1 — lock
    plan_v1 = {
        "study_id": study_id,
        "version": 1,
        "locked_at": "2026-01-02T00:00:00.000000+00:00",
        "amendment_reason": "",
        "content_hash": "hash_v1_base",
    }
    lock_path = study_dir / "study_plan.v1.locked.json"
    lock_path.write_text(json.dumps(plan_v1))

    # Plan v2 — pre-unmask amendment
    plan_v2 = {
        "study_id": study_id,
        "version": 2,
        "locked_at": "2026-01-15T00:00:00.000000+00:00",
        "amendment_reason": "Added Cox PH model",
        "content_hash": "hash_v2_amend",
    }
    lock_path = study_dir / "study_plan.v2.locked.json"
    lock_path.write_text(json.dumps(plan_v2))

    # Plan v3 — post-hoc amendment (after unmask)
    plan_v3 = {
        "study_id": study_id,
        "version": 3,
        "locked_at": "2026-03-02T00:00:00.000000+00:00",
        "amendment_reason": "Exploratory subgroup analysis",
        "post_hoc_tests": [{"test_name": "t_test", "variable_name": "age"}],
        "content_hash": "hash_v3_ph",
    }
    lock_path = study_dir / "study_plan.v3.locked.json"
    lock_path.write_text(json.dumps(plan_v3))

    # Analysis result 1 — pre-registered, will be superseded
    now = "2026-03-03T00:00:00.000000+00:00"
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, computed_at, p_value, statistic,
            status_json, is_pre_registered, study_plan_version,
            provenance_json, sample_counts_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (study_id, "chi_square", now, 0.03, 5.2,
         json.dumps({"status": "completed"}), 1, 1,
         json.dumps({"plan_version": 1}), json.dumps({"n_analyzed": 100})),
    )
    result_id_1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Analysis result 2 — cox_ph_model, pre-registered
    now2 = "2026-03-03T00:00:01.000000+00:00"
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, computed_at, p_value, statistic,
            ci_lower, ci_upper, effect_size_json, status_json,
            is_pre_registered, study_plan_version, provenance_json,
            sample_counts_json, lr_test_p, concordance_index)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (study_id, "cox_ph_model", now2, 0.05, 1.5,
         0.8, 2.5, json.dumps({"metric": "HR", "value": 1.5}),
         json.dumps({"status": "completed"}), 1, 2,
         json.dumps({"plan_version": 2}), json.dumps({"n_analyzed": 100}),
         0.01, 0.72),
    )
    result_id_2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    # Analysis result 3 — rerun of chi_square, supersedes result_id_1
    now3 = "2026-03-04T00:00:00.000000+00:00"
    conn.execute(
        """INSERT INTO analysis_results
           (study_id, test_name, computed_at, p_value, statistic,
            status_json, is_pre_registered, study_plan_version,
            provenance_json, sample_counts_json,
            superseded_previous_result_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (study_id, "chi_square", now3, 0.04, 4.8,
         json.dumps({"status": "completed"}), 1, 1,
         json.dumps({"plan_version": 1}), json.dumps({"n_analyzed": 100}),
         result_id_1),
    )
    conn.commit()

    # Add covariate rows for cox_ph_model result
    for cr in [
        ("treatment_arm", 1.5, 0.8, 2.5, 0.05, 0.4, 0.2, 2.0),
        ("age", 1.02, 0.95, 1.1, 0.6, 0.02, 0.04, 0.5),
    ]:
        conn.execute(
            """INSERT INTO analysis_covariate_results
               (result_id, covariate, hr, ci_lower, ci_upper,
                wald_p, coef, se, z)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result_id_2,) + cr,
        )
    conn.commit()

    # Build a fake bundle
    bundle_path = study_dir / f"{study_id}_bundle.tar.gz"
    import tarfile, io
    manifest = {
        "schema_version": "1.0.0",
        "study_id": study_id,
        "generated_at": "2026-03-05T00:00:00.000000+00:00",
        "composite_hash": "bundle_hash_xyz",
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="manifest.json")
        mdata = json.dumps(manifest).encode()
        info.size = len(mdata)
        tf.addfile(info, io.BytesIO(mdata))
    bundle_path.write_bytes(buf.getvalue())

    conn.close()
    return result_id_1


class TestLineageAssembly:
    """Tests for lineage event assembly."""

    def test_minimal_study_no_crash(self):
        """Minimal study (ingest + lock only) must not crash or fabricate events."""
        study_id = "test_lineage_minimal"
        _setup_minimal_study(study_id)

        events = assemble_events(study_id)
        assert len(events) > 0

        # Check for honest rendering — no fabricated stages
        event_types = [e.event_type for e in events]
        assert "ingest" in event_types
        assert "plan_lock" in event_types
        # No unmask if study never unmasked
        assert "unmask" not in event_types
        # No bundle if none exists
        assert "bundle" not in event_types
        # Seal present at ingest time (not synthesized from plan lock)
        seals = [e for e in events if e.event_type == "seal"]
        assert len(seals) == 1
        assert seals[0].label == "Outcome data sealed (ingest-time masking)"

        # Text render must not crash
        text = render_text(events)
        assert "Study provenance DAG" in text
        assert "Plan locked" in text
        assert "│" in text
        assert "ingest-time masking" in text

        # SVG render must not crash
        svg_path = str(DATA_ROOT / study_id / "test_lineage.svg")
        render_svg(events, svg_path)
        assert Path(svg_path).exists()
        assert Path(svg_path).stat().st_size > 0

        # Cleanup
        shutil.rmtree(DATA_ROOT / study_id, ignore_errors=True)

    def test_full_lifecycle_all_events_present(self):
        """Full lifecycle must include all expected event types with correct attributes."""
        study_id = "test_lineage_full"
        result_id_1 = _setup_full_lifecycle(study_id)

        events = assemble_events(study_id)
        event_types = [e.event_type for e in events]

        # All event types present
        assert "ingest" in event_types
        assert "variable_classification" in event_types
        assert "plan_lock" in event_types
        assert "amendment" in event_types
        assert "seal" in event_types
        assert "unmask" in event_types
        assert "analyze" in event_types
        assert "bundle" in event_types

        # Two amendments with correct branching
        amendments = [e for e in events if e.event_type == "amendment"]
        assert len(amendments) == 2
        pre_unmask = [e for e in amendments if e.branch == "pre_unmask_amend"]
        post_hoc = [e for e in amendments if e.branch == "post_hoc_amend"]
        assert len(pre_unmask) == 1
        assert len(post_hoc) == 1
        assert pre_unmask[0].detail.get("reason") == "Added Cox PH model"
        assert post_hoc[0].detail.get("reason") == "Exploratory subgroup analysis"

        # Plan lock present
        plan_locks = [e for e in events if e.event_type == "plan_lock"]
        assert len(plan_locks) == 1
        assert plan_locks[0].detail.get("version") == 1
        assert plan_locks[0].detail.get("content_hash") == "hash_v1_base"

        # Three analysis results (chi_square, cox_ph_model, chi_square rerun)
        analyzes = [e for e in events if e.event_type == "analyze"]
        assert len(analyzes) == 3
        assert analyzes[0].detail.get("test_name") in ("chi_square", "cox_ph_model")
        assert analyzes[2].detail.get("supersedes_result_id") == result_id_1

        # Bundle present
        bundles = [e for e in events if e.event_type == "bundle"]
        assert len(bundles) == 1
        assert bundles[0].detail.get("composite_hash") == "bundle_hash_xyz"

        # Chronological order
        timestamps = [e.timestamp for e in events if e.timestamp]
        assert timestamps == sorted(timestamps)

        # Seal appears at ingest time, before any plan lock
        seal_idx = next(i for i, e in enumerate(events) if e.event_type == "seal")
        plan_lock_idx = next(i for i, e in enumerate(events) if e.event_type == "plan_lock")
        classification_idx = next(
            i for i, e in enumerate(events) if e.event_type == "variable_classification"
        )
        # Seal must be between classification and plan lock
        assert classification_idx < seal_idx < plan_lock_idx, (
            f"Seal at position {seal_idx} should be between classification "
            f"({classification_idx}) and plan lock ({plan_lock_idx})"
        )

        # Text render includes expected content
        text = render_text(events)
        assert "CONFIRMATORY" in text
        assert "EXPLORATORY_POST_HOC" in text
        assert "supersedes" in text
        assert "Bundle created" in text
        assert "│" in text
        assert "├─" in text
        assert "ingest-time masking" in text

        # SVG render must not crash
        svg_path = str(DATA_ROOT / study_id / "test_lineage_full.svg")
        render_svg(events, svg_path)
        svg_content = Path(svg_path).read_text()
        assert "Study Provenance DAG" in svg_content
        assert "CONFIRMATORY" in svg_content
        assert "EXPLORATORY_POST_HOC" in svg_content
        # SVG should have branch connectors (dashed lines) for amendments
        assert 'stroke-dasharray="3,2"' in svg_content
        assert "ingest-time masking" in svg_content

        # Cleanup
        shutil.rmtree(DATA_ROOT / study_id, ignore_errors=True)

    def test_empty_study_returns_empty(self):
        """Non-existent study must return empty event list without crash."""
        events = assemble_events("nonexistent_study_id_12345")
        assert events == []

    def test_no_events_renders_sensibly(self):
        """Empty events list must render without crash in both formats."""
        text = render_text([])
        assert "No provenance data" in text

        svg_path = "/tmp/test_lineage_empty.svg"
        render_svg([], svg_path)
        assert Path(svg_path).exists()
        Path(svg_path).unlink(missing_ok=True)
