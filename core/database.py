"""SQLite database connection and initialization.

Uses stdlib sqlite3 — no ORM.  Every study gets its own .db file under
data/studies/<study_id>/study.db.
"""

import os
import sqlite3
from pathlib import Path

from core.models import SCHEMA_SQL

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
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Run CREATE TABLE IF NOT EXISTS for all tables."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()
