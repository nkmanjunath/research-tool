"""Canonical hashing utilities for data integrity verification.

Single source of truth for raw-data hashing across cmd_export, bundle, and
excel_export. Fixes M2: data hash inconsistency between code paths.
"""

from __future__ import annotations

import hashlib
import json

from core.database import get_connection


def sha256(content: str | bytes) -> str:
    """SHA-256 hex digest of a string or bytes object."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def canonical_json(obj) -> str:
    """Deterministic JSON serialization: sorted keys, no extra whitespace."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_raw_data_hash(study_id: str) -> str:
    """Compute SHA-256 hash of a study's raw data using canonical JSON.

    Reads from the raw_<study_id> ingestion table, exports all rows as a
    sorted-keys JSON array, and returns the hex digest.
    """
    conn = get_connection(study_id)
    raw_table = f"raw_{study_id}"
    cur = conn.execute(f"SELECT * FROM {raw_table} ORDER BY row_id")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return sha256(canonical_json(rows))
