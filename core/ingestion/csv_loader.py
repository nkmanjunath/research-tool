"""Load CSV/Excel into a dynamic per-study raw data table."""

import json
from pathlib import Path

import pandas as pd

from core.database import get_connection, init_db


def _sanitize_col(name: str) -> str:
    """Make a column name SQL-safe."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name).strip("_") or "col"


def infer_dtype(series: pd.Series) -> str:
    """Heuristic: int/float → continuous, small cardinality → categorical."""
    if series.dtype == "object":
        return "categorical"
    uniq = series.nunique(dropna=False)
    if uniq <= 5:
        return "categorical"
    return "continuous"


def load_file(study_id: str, filepath: str, na_values: list[str] | None = None) -> list[str]:
    """Load a CSV (or Excel) file into the study's database.

    Creates a dynamic table named `raw_<study_id>` with one column per CSV column
    (all TEXT).  Returns the list of column names found.

    Parameters
    ----------
    study_id : str
    filepath : str
        Path to the CSV or Excel file.
    na_values : list[str], optional
        Additional strings to treat as NA/NaN during CSV parsing
        (e.g. ``["unknown", "missing"]``).  Added on top of pandas' defaults.
    """
    path = Path(filepath)
    if path.suffix in (".xls", ".xlsx"):
        df = pd.read_excel(str(path))
    else:
        kwargs = {"keep_default_na": True}
        if na_values:
            kwargs["na_values"] = na_values
        df = pd.read_csv(str(path), **kwargs)

    conn = get_connection(study_id)
    init_db(conn)

    # Strip leading/trailing whitespace from string columns
    str_cols = df.select_dtypes(include="object").columns
    for c in str_cols:
        df[c] = df[c].apply(lambda v: v.strip() if isinstance(v, str) else v)
    # Also convert whitespace-only strings to None so they register as missing
    df = df.replace(r"^\s*$", pd.NA, regex=True)

    # ── Duplicate patient_id detection ─────────────────────────────────
    pid_raw = next((c for c in df.columns if c.lower() in ("patient_id", "patientid", "id", "subject_id")), None)
    if pid_raw:
        pid_col = _sanitize_col(pid_raw)
        dupe_counts = df[pid_raw].value_counts()
        dupes = dupe_counts[dupe_counts > 1]
        if not dupes.empty:
            dupe_list = ", ".join(f"'{pid}' ({n}x)" for pid, n in dupes.items())
            print(f"Warning: duplicate patient identifiers found — {dupe_list}. "
                  f"These rows will be included in the analysis; "
                  f"verify they are genuine repeat records.",
                  file=__import__("sys").stderr)

    col_names = [_sanitize_col(c) for c in df.columns]
    col_defs = ", ".join(f'"{c}" TEXT' for c in col_names)
    table_name = f"raw_{study_id}"

    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.execute(f'CREATE TABLE {table_name} ("row_id" INTEGER PRIMARY KEY, {col_defs})')

    placeholders = ", ".join("?" for _ in col_names)
    col_list = ", ".join(f'"{c}"' for c in col_names)

    for _, row in df.iterrows():
        vals = tuple(None if pd.isna(v) else str(v) for v in row)
        conn.execute(f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})", vals)

    conn.commit()
    conn.close()
    return col_names


def find_duplicate_patient_ids(study_id: str) -> list[tuple[str, int]]:
    """Query the study's raw table for duplicate patient identifiers.

    Returns a list of ``(patient_id, count)`` tuples for every duplicated
    patient ID, ordered by count descending.  Returns an empty list when
    there is no patient_id column or no duplicates exist.
    """
    from core.database import get_connection

    conn = get_connection(study_id)
    raw_table = f"raw_{study_id}"

    # Probe column names for a patient-like column
    cur = conn.execute(f"PRAGMA table_info({raw_table})")
    col_names = [r["name"] for r in cur.fetchall()]
    pid_col = next(
        (c for c in col_names if c.lower() in ("patient_id", "patientid", "id", "subject_id")),
        None,
    )
    if not pid_col:
        conn.close()
        return []

    cur = conn.execute(
        f'SELECT "{pid_col}" AS pid, COUNT(*) AS cnt FROM {raw_table} '
        f'GROUP BY "{pid_col}" HAVING cnt > 1 ORDER BY cnt DESC'
    )
    dupes = [(r["pid"], r["cnt"]) for r in cur.fetchall()]
    conn.close()
    return dupes
