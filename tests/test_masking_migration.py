"""
Standalone smoke test for the outcome-masking migration.
Builds an in-memory throwaway DB matching the migration's shape,
applies the migration, and asserts the masking invariant holds
for BOTH the sqlite3 stdlib driver directly (simulating a raw
CLI-equivalent connection) AND confirms it's not dependent on
any application wrapper.

Run: python3 test_masking_migration.py
"""
import sqlite3
import sys

SETUP_RAW_TABLE = """
CREATE TABLE cohort (
    study_id TEXT,
    pt_id TEXT,
    age INTEGER,
    stage TEXT,
    treatment TEXT,
    os_event INTEGER,
    os_months REAL
);
"""

SEED_DATA = """
INSERT INTO cohort (study_id, pt_id, age, stage, treatment, os_event, os_months)
VALUES ('study_123', 'P001', 55, 'II', 'ArmA', 1, 14.2),
       ('study_123', 'P002', 61, 'III', 'ArmB', 0, 22.7);
"""

MIGRATION = """
ALTER TABLE cohort RENAME TO _raw_cohort;

CREATE TABLE study_state (
    study_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'unlocked'
        CHECK (status IN ('unlocked', 'locked', 'amendment_in_progress')),
    current_plan_hash TEXT,
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO study_state (study_id, status) VALUES ('study_123', 'unlocked');

CREATE VIEW cohort AS
SELECT
    r.pt_id, r.age, r.stage, r.treatment,
    CASE WHEN (SELECT status FROM study_state WHERE study_id = r.study_id) = 'locked'
         THEN r.os_event ELSE NULL END AS os_event,
    CASE WHEN (SELECT status FROM study_state WHERE study_id = r.study_id) = 'locked'
         THEN r.os_months ELSE NULL END AS os_months
FROM _raw_cohort r;
"""

def run():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SETUP_RAW_TABLE)
    conn.executescript(SEED_DATA)
    conn.executescript(MIGRATION)
    conn.commit()

    failures = []

    # Test 1: unlocked -> view returns NULL outcome, no wrapper involved
    rows = conn.execute("SELECT pt_id, os_event, os_months FROM cohort").fetchall()
    for pt_id, os_event, os_months in rows:
        if os_event is not None or os_months is not None:
            failures.append(f"UNLOCKED test FAILED: {pt_id} returned os_event={os_event}, os_months={os_months} (expected NULL)")

    # Test 2: raw table still has real data (expected -- that's the vault, not the leak)
    raw_rows = conn.execute("SELECT pt_id, os_event FROM _raw_cohort").fetchall()
    if not any(r[1] is not None for r in raw_rows):
        failures.append("RAW TABLE test FAILED: expected real values in _raw_cohort, got all NULL")

    # Test 3: lock the study, view should now reveal real values
    conn.execute("UPDATE study_state SET status = 'locked' WHERE study_id = 'study_123'")
    conn.commit()
    rows_locked = conn.execute("SELECT pt_id, os_event, os_months FROM cohort").fetchall()
    if not any(r[1] is not None for r in rows_locked):
        failures.append("LOCKED test FAILED: expected real os_event values after lock, got all NULL")

    # Test 4: re-mask on amendment (simulating Autopsy Canvas re-entry)
    conn.execute("UPDATE study_state SET status = 'amendment_in_progress' WHERE study_id = 'study_123'")
    conn.commit()
    rows_amend = conn.execute("SELECT pt_id, os_event FROM cohort").fetchall()
    if any(r[1] is not None for r in rows_amend):
        failures.append("AMENDMENT test FAILED: expected re-masking during amendment_in_progress, got real values")

    # Test 5: a second study must not be affected by study_123's lock state
    conn.execute("INSERT INTO study_state (study_id, status) VALUES ('study_456', 'unlocked')")
    conn.execute("""INSERT INTO _raw_cohort (study_id, pt_id, age, stage, treatment, os_event, os_months)
                     VALUES ('study_456', 'Q001', 40, 'I', 'ArmA', 1, 5.0)""")
    conn.commit()
    cross_study = conn.execute(
        "SELECT c.pt_id, c.os_event FROM cohort c JOIN _raw_cohort r USING(pt_id) WHERE r.study_id='study_456'"
    ).fetchall()
    if any(r[1] is not None for r in cross_study):
        failures.append("CROSS-STUDY test FAILED: study_456 (unlocked) leaked real os_event values")

    conn.close()

    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    else:
        print("All masking invariant tests passed.")

if __name__ == "__main__":
    run()
