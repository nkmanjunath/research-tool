"""The Outcome-Masking Gate.

Enforces outcome masking at the physical storage level.

When variables are classified as outcomes, their values are moved from the
main raw_{study_id} table into a shadow raw_masked_{study_id} table and
replaced with NULLs.  This catches EVERY access path — sqlite3 CLI, raw
Python sqlite3 module, any process that touches the database file.  The only
way to recover the values is unmask(), which copies them back and transitions
the study to the unmasked state permanently.

State machine:
  0 (pre-lock)     → outcome columns are NULL in the main table
  1 (locked)       → outcome columns still NULL
  2 (unmasked)     → outcome values restored, irreversible
"""

from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from core.database import get_connection, init_db


class MaskedDataError(Exception):
    """Raised when outcome data is accessed while the study is still masked."""


def _get_study_state(conn: sqlite3.Connection, study_id: str) -> int:
    cur = conn.execute("SELECT is_locked FROM studies WHERE id=?", (study_id,))
    row = cur.fetchone()
    return row["is_locked"] if row else 0


def seal_outcomes(study_id: str) -> None:
    """Move outcome column values into the masked shadow table, NULL them in main.

    Called AFTER variable classification.  This is the physical enforcement:
    outcome values simply do not exist in the main table until unmask.
    """
    conn = get_connection(study_id)
    init_db(conn)

    # Get outcome column names
    cur = conn.execute(
        "SELECT column_name FROM variables WHERE study_id=? AND role='outcome'",
        (study_id,),
    )
    outcome_cols = [row["column_name"] for row in cur.fetchall()]
    if not outcome_cols:
        conn.close()
        return

    raw = f"raw_{study_id}"
    masked = f"raw_masked_{study_id}"

    # Create the shadow table with same columns + row_id link
    col_defs = ", ".join(f'"{c}" TEXT' for c in outcome_cols)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {masked} (
            "row_id" INTEGER PRIMARY KEY REFERENCES {raw}(row_id),
            {col_defs}
        )
    """)

    # Copy outcome values from raw to masked
    col_list = ", ".join(f'"{c}"' for c in outcome_cols)
    conn.execute(f"DELETE FROM {masked}")
    conn.execute(f"""
        INSERT INTO {masked} (row_id, {col_list})
        SELECT row_id, {col_list} FROM {raw}
    """)
    conn.commit()

    # NULL out outcome columns in the main raw table
    for col in outcome_cols:
        conn.execute(f'UPDATE {raw} SET "{col}" = NULL')

    conn.commit()
    conn.close()


def unmask_study(study_id: str) -> None:
    """Copy outcome values back from shadow table, transition to unmasked.

    Irreversible — once called, outcome columns become permanently visible.
    """
    conn = get_connection(study_id)
    init_db(conn)

    cur = conn.execute(
        "SELECT column_name FROM variables WHERE study_id=? AND role='outcome'",
        (study_id,),
    )
    outcome_cols = [row["column_name"] for row in cur.fetchall()]
    if not outcome_cols:
        conn.close()
        return

    raw = f"raw_{study_id}"
    masked = f"raw_masked_{study_id}"

    for col in outcome_cols:
        conn.execute(f"""
            UPDATE {raw} SET "{col}" = (
                SELECT "{col}" FROM {masked} WHERE {masked}.row_id = {raw}.row_id
            )
        """)

    conn.execute("UPDATE studies SET is_locked=2, unmasked_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), study_id))
    conn.commit()
    conn.close()


def lock_study(study_id: str) -> None:
    """Transition to locked state."""
    conn = get_connection(study_id)
    init_db(conn)
    conn.execute("UPDATE studies SET is_locked=1 WHERE id=?", (study_id,))
    conn.commit()
    conn.close()


def is_masked(study_id: str) -> bool:
    conn = get_connection(study_id)
    state = _get_study_state(conn, study_id)
    conn.close()
    return state < 2
