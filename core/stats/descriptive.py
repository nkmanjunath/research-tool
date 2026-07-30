"""Descriptive statistics — wraps tableone for baseline Table 1 generation.

Only runs on baseline/masked-permitted columns.
"""

from __future__ import annotations
from typing import Optional

import pandas as pd
from tableone import TableOne

from core.database import get_connection, DATA_ROOT


def generate_table1(
    study_id: str,
    groupby: Optional[str] = None,
    categorical: Optional[list[str]] = None,
    continuous: Optional[list[str]] = None,
    nonnormal: Optional[list[str]] = None,
) -> pd.DataFrame:
    """Generate Table 1 (baseline characteristics table).

    Parameters
    ----------
    study_id : str
    groupby : str, optional
        Column to group by (exposure/outcome status or treatment arm)
    categorical : list[str], optional
        Columns to treat as categorical. Auto-detected from variable registry if None.
    continuous : list[str], optional
        Columns to treat as continuous. Auto-detected if None.
    nonnormal : list[str], optional
        Continuous columns to report as median(IQR) instead of mean(SD).

    Returns
    -------
    pd.DataFrame
    """
    conn = get_connection(study_id)
    raw_table = f"raw_{study_id}"

    # Get classified variables
    var_df = pd.read_sql_query(
        "SELECT column_name, role, data_type FROM variables WHERE study_id=?",
        conn,
        params=(study_id,),
    )

    # Only baseline variables in Table 1
    baseline_vars = var_df[var_df["role"] == "baseline"]

    # Get the raw data, cast to numeric where possible
    col_list = list(baseline_vars["column_name"])
    if groupby and groupby not in col_list:
        col_list.append(groupby)

    if not col_list:
        conn.close()
        return pd.DataFrame()

    col_csv = ", ".join(f'"{c}"' for c in col_list)
    df = pd.read_sql_query(f"SELECT {col_csv} FROM {raw_table}", conn)
    conn.close()

    # Determine categorical/continuous if not provided
    if categorical is None:
        cat_vars = baseline_vars[baseline_vars["data_type"] == "categorical"]["column_name"].tolist()
    else:
        cat_vars = categorical

    if continuous is None:
        cont_vars = baseline_vars[baseline_vars["data_type"] == "continuous"]["column_name"].tolist()
    else:
        cont_vars = continuous

    # Only include columns that actually exist in the data
    cat_vars = [c for c in cat_vars if c in df.columns]
    cont_vars = [c for c in cont_vars if c in df.columns]

    # Exclude stratification/grouping variable from row list (CONSORT / Table 1 standard)
    if groupby:
        cat_vars = [c for c in cat_vars if c != groupby]
        cont_vars = [c for c in cont_vars if c != groupby]

    if not cat_vars and not cont_vars:
        return pd.DataFrame()

    # Coerce continuous columns to numeric (sqlite3 stores as TEXT)
    for c in cont_vars:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in cat_vars:
        df[c] = df[c].astype(str)

    tbl = TableOne(
        df,
        columns=cont_vars + cat_vars,
        categorical=cat_vars,
        groupby=groupby,
        nonnormal=nonnormal or [],
        pval=False,
    )

    return tbl.tableone
