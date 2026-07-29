"""M1 regression: _cascade_clear must delete analysis_covariate_results
before analysis_results to avoid FK violation.

Previously, deleting from analysis_results while covariate results existed
raised sqlite3.IntegrityError with PRAGMA foreign_keys=ON.
"""

from __future__ import annotations

import json

import pytest

from core.database import get_connection, init_db, DATA_ROOT
from core.ingestion.csv_loader import _cascade_clear

STUDY_ID = "test_m1_fk"


@pytest.fixture(autouse=True)
def _setup(tmp_path):
    """Create a minimal study with analysis_results and covariate results."""
    conn = get_connection(STUDY_ID)
    init_db(conn)

    conn.execute(
        "INSERT OR REPLACE INTO studies (id, name, created_at, data_dir, is_locked) "
        "VALUES (?, ?, datetime('now'), ?, 0)",
        (STUDY_ID, "M1 test", str(DATA_ROOT / STUDY_ID)),
    )
    # Create an analysis result
    cur = conn.execute(
        "INSERT INTO analysis_results "
        "(study_id, study_plan_version, variable_ids_used, test_name, "
        " statistic, p_value, computed_at, status_json) "
        "VALUES (?, 1, ?, 'chi_square', 5.0, 0.03, datetime('now'), ?)",
        (STUDY_ID, json.dumps(["test_var"]),
         json.dumps({"status": "completed"})),
    )
    result_id = cur.lastrowid

    # Create a covariate result referencing the analysis result
    conn.execute(
        "INSERT INTO analysis_covariate_results "
        "(result_id, covariate, hr, ci_lower, ci_upper, wald_p) "
        "VALUES (?, 'age', 1.5, 1.1, 2.0, 0.01)",
        (result_id,),
    )
    conn.commit()
    conn.close()

    yield

    # Cleanup
    try:
        conn = get_connection(STUDY_ID)
        conn.execute("DELETE FROM analysis_covariate_results")
        conn.execute("DELETE FROM analysis_results WHERE study_id=?", (STUDY_ID,))
        conn.execute("DELETE FROM variables WHERE study_id=?", (STUDY_ID,))
        conn.execute("DELETE FROM studies WHERE id=?", (STUDY_ID,))
        conn.commit()
        conn.close()
    except Exception:
        pass


class TestM1CascadeClear:
    def test_cascade_clear_with_covariate_results(self):
        """_cascade_clear must not raise FK violation when covariate results exist."""
        # This would raise sqlite3.IntegrityError before the M1 fix
        _cascade_clear(STUDY_ID)

        # Verify both tables are empty
        conn = get_connection(STUDY_ID)
        ar_count = conn.execute(
            "SELECT COUNT(*) FROM analysis_results WHERE study_id=?",
            (STUDY_ID,),
        ).fetchone()[0]
        acr_count = conn.execute(
            "SELECT COUNT(*) FROM analysis_covariate_results",
        ).fetchone()[0]
        conn.close()

        assert ar_count == 0, "analysis_results not cleared"
        assert acr_count == 0, "analysis_covariate_results not cleared"
