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


def load_file(study_id: str, filepath: str) -> list[str]:
    """Load a CSV (or Excel) file into the study's database.

    Creates a dynamic table named `raw_<study_id>` with one column per CSV column
    (all TEXT).  Returns the list of column names found.
    """
    path = Path(filepath)
    if path.suffix in (".xls", ".xlsx"):
        df = pd.read_excel(str(path))
    else:
        df = pd.read_csv(str(path))

    conn = get_connection(study_id)
    init_db(conn)

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
