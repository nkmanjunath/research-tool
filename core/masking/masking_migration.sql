-- ============================================================
-- Outcome Masking Migration: Conditional View Pattern
-- Turns "get_connection() wraps outcome access" from an
-- application convention into a database-level invariant that
-- holds even against a raw `sqlite3` CLI connection.
--
-- Design constraints honored:
--   1. Lock state is scoped PER STUDY, not a single global flag
--      (multiple studies must not leak into each other).
--   2. During an Autopsy amendment, the study re-enters an
--      unlocked state and outcome access re-masks — locking is
--      not a one-way switch, it tracks current plan status.
--   3. Event/censored polarity is stored explicitly, never
--      assumed from a bare 0/1 value.
-- ============================================================

-- ------------------------------------------------------------
-- STEP 0: Rename the existing raw table.
-- All prior application code that referenced `cohort` directly
-- must be repointed to the new `cohort` VIEW created in Step 3,
-- not this raw table. No code should ever query `_raw_cohort`
-- directly except the Stage 3 Rigor Engine after lock.
-- ------------------------------------------------------------
ALTER TABLE cohort RENAME TO _raw_cohort;


-- ------------------------------------------------------------
-- STEP 1: Study-scoped lock state table.
-- One row per study. `status` tracks the actual plan lifecycle,
-- not a boolean -- an amendment in progress is a distinct state
-- from both "never locked" and "locked and executed."
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS study_state (
    study_id            TEXT PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'unlocked'
                         CHECK (status IN ('unlocked', 'locked', 'amendment_in_progress')),
    current_plan_hash   TEXT,              -- H1, or latest Hn if amended
    updated_at_utc      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Seed row for an existing study if not already present.
-- (Run once per study_id at study creation time, not here --
--  shown for illustration.)
-- INSERT OR IGNORE INTO study_state (study_id, status) VALUES ('study_123', 'unlocked');


-- ------------------------------------------------------------
-- STEP 2: Outcome polarity metadata table.
-- Stores which raw value means "event" vs "censored" explicitly,
-- per study, per outcome column. Prevents the statistical engine
-- (or a human) from ever guessing 0/1 -> alive/dead direction.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcome_schema (
    study_id                TEXT NOT NULL,
    outcome_column          TEXT NOT NULL,
    event_indicator_value   TEXT NOT NULL,   -- e.g. "1" or "DECEASED"
    censored_indicator_value TEXT NOT NULL,  -- e.g. "0" or "ALIVE"
    time_column             TEXT,            -- e.g. "os_months", nullable if non-survival
    PRIMARY KEY (study_id, outcome_column)
);


-- ------------------------------------------------------------
-- STEP 3: The conditional masking view.
-- This is what ALL application code (Tab 1 browser, redundancy
-- checks, cross-tabs) and any raw `sqlite3` CLI session query
-- against. Outcome column returns NULL unless the study's
-- current state is 'locked'.
--
-- NOTE: replace `os_event` / `os_months` / covariate list below
-- with the actual per-study column set -- this is illustrative
-- for one study's schema. If column sets vary per study, this
-- view needs to be generated per-study at schema-mapping time
-- in Tab 1, not hand-written once.
-- ------------------------------------------------------------
CREATE VIEW IF NOT EXISTS cohort AS
SELECT
    r.pt_id,
    r.age,
    r.stage,
    r.treatment,
    -- outcome column: masked unless this row's study is locked
    CASE
        WHEN (SELECT status FROM study_state WHERE study_id = r.study_id) = 'locked'
            THEN r.os_event
        ELSE NULL
    END AS os_event,
    CASE
        WHEN (SELECT status FROM study_state WHERE study_id = r.study_id) = 'locked'
            THEN r.os_months
        ELSE NULL
    END AS os_months
FROM _raw_cohort r;


-- ------------------------------------------------------------
-- STEP 4: Lock / unlock transition helpers.
-- Application code (Tab 2 lock gate, Tab 3 amendment flow)
-- should call these rather than writing raw UPDATE statements,
-- so every lock-state transition is one auditable code path.
-- ------------------------------------------------------------

-- Called by Tab 2's Lock Gate on successful plan lock.
-- UPDATE study_state
-- SET status = 'locked', current_plan_hash = :plan_hash,
--     updated_at_utc = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
-- WHERE study_id = :study_id;

-- Called by Tab 3's Autopsy Canvas when a locked plan fails
-- execution and the user opens the Amendment Canvas.
-- Re-masks outcome access immediately -- the user must not see
-- raw outcome values again until the amended plan re-locks.
-- UPDATE study_state
-- SET status = 'amendment_in_progress',
--     updated_at_utc = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
-- WHERE study_id = :study_id;

-- Called when the amended plan (Hn) is itself locked.
-- Identical to the original lock transition, just fires again
-- with the new plan hash.
-- UPDATE study_state
-- SET status = 'locked', current_plan_hash = :new_plan_hash,
--     updated_at_utc = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
-- WHERE study_id = :study_id;


-- ============================================================
-- VERIFICATION (run manually after migration, via raw CLI --
-- NOT through get_connection() or any application wrapper):
--
--   $ sqlite3 data/studies/study_123.db
--   sqlite> SELECT status FROM study_state WHERE study_id = 'study_123';
--   -- expect: unlocked
--
--   sqlite> SELECT pt_id, age, os_event FROM cohort LIMIT 5;
--   -- expect: os_event column is NULL for every row
--
--   sqlite> SELECT pt_id, age, os_event FROM _raw_cohort LIMIT 5;
--   -- expect: os_event shows real 0/1 values -- this is EXPECTED,
--   -- _raw_cohort is the vaulted source table. The invariant is
--   -- that application code and casual inspection go through the
--   -- `cohort` VIEW, not the raw table. If real code anywhere
--   -- still queries `_raw_cohort` pre-lock, that is the bug to fix,
--   -- not this migration.
--
--   -- After locking:
--   sqlite> UPDATE study_state SET status = 'locked' WHERE study_id = 'study_123';
--   sqlite> SELECT pt_id, age, os_event FROM cohort LIMIT 5;
--   -- expect: os_event now shows real values
-- ============================================================
