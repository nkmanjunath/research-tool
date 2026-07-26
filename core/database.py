"""SQLite database connection and initialization.

Uses stdlib sqlite3 — no ORM.  Every study gets its own .db file under
data/studies/<study_id>/study.db.
"""

import os
import sqlite3
from pathlib import Path

import sqlite3

from core.models import SCHEMA_SQL, MIGRATIONS_SQL

DATA_ROOT = Path("data/studies")


def study_dir(study_id: str) -> Path:
    return DATA_ROOT / study_id


def get_connection(study_id: str) -> sqlite3.Connection:
    """Return a connection to the study's database, creating dirs if needed."""
    d = study_dir(study_id)
    d.mkdir(parents=True, exist_ok=True)
    db_path = d / "study.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Run CREATE TABLE IF NOT EXISTS for all tables."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def migrate_db(conn: sqlite3.Connection) -> None:
    """Apply migrations for schema additions (new columns, new tables).
    
    Safe to call on any version — catches "duplicate column" errors.
    Only runs if the target table already exists (skip on fresh DB before init_db).
    """
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_results'"
    ).fetchone()
    if not existing:
        return
    for stmt in MIGRATIONS_SQL.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                continue
            raise
    conn.commit()
