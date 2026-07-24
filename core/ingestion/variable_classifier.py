"""Help the user classify variables as baseline/outcome and data types."""

from core.database import get_connection


def classify_variables_interactive(study_id: str, columns: list[str]) -> list[dict]:
    """CLI-guided variable classification.

    In non-interactive/test contexts the returned list describes what would be
    asked.  Tests provide answers directly via _classify_batch.
    """
    # Pre-fill with heuristics from column names
    results = []
    for col in columns:
        col_lower = col.lower()
        # Heuristic for outcome-like column names
        if any(kw in col_lower for kw in ("outcome", "response", "survival", "pfs", "os", "status", "event", "death")):
            default_role = "outcome"

            if any(kw in col_lower for kw in ("time", "days", "months", "duration", "follow_up", "fu")):
                default_dtype = "time_to_event"

            else:
                default_dtype = "categorical"

        elif any(kw in col_lower for kw in ("id", "name", "patient")):
            continue  # skip identifier columns
        else:
            default_role = "baseline"

            tokens = set(col_lower.replace("-", "_").split("_"))
            if tokens & {"age", "bmi", "weight", "height", "value", "count", "number",
                          "line", "lines", "prior"}:
                default_dtype = "continuous"

            else:
                default_dtype = "categorical"

        results.append({"column": col, "role": default_role, "data_type": default_dtype})
    return results


def _classify_batch(study_id: str, variables: list[dict]) -> None:
    """Write classified variables to the database.

    Each dict: {column, role, data_type}
    """
    conn = get_connection(study_id)
    for v in variables:
        is_outcome = 1 if v["role"] == "outcome" else 0
        conn.execute(
            """INSERT INTO variables (study_id, column_name, role, data_type, is_masked)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(study_id, column_name) DO UPDATE SET
                 role=excluded.role, data_type=excluded.data_type, is_masked=excluded.is_masked""",
            (study_id, v["column"], v["role"], v["data_type"], is_outcome),
        )
    conn.commit()
    conn.close()
