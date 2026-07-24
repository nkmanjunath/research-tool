"""Help the user classify variables as baseline/outcome and data types."""

from __future__ import annotations

from core.database import get_connection


_OUTCOME_KEYWORDS = frozenset({
    "outcome", "response", "survival", "pfs", "os", "status", "event",
    "death", "followup", "lab", "marker", "protein", "score", "level",
    "change", "fold", "day",
})


def _infer_data_type(study_id: str, column: str) -> str | None:
    """Infer data type by inspecting actual column values.

    Returns ``"continuous"``, ``"categorical"``, or ``None`` if the raw table
    cannot be queried (heuristic-only fallback).
    """
    conn = get_connection(study_id)
    try:
        raw_table = f"raw_{study_id}"
        cur = conn.execute(
            f'SELECT DISTINCT "{column}" FROM {raw_table} WHERE "{column}" IS NOT NULL LIMIT 100'
        )
        values = [row[0] for row in cur.fetchall()]
        if not values:
            return None

        # Attempt numeric parse
        numeric = []
        non_numeric = []
        for v in values:
            try:
                float(v)
                numeric.append(v)
            except (ValueError, TypeError):
                non_numeric.append(v)

        # If any values are non-numeric strings, it's categorical
        if non_numeric:
            return "categorical"

        # If all values are numeric, check distinct count
        n_distinct = len(numeric)
        if n_distinct > 5:
            return "continuous"
        return "categorical"
    except Exception:
        return None
    finally:
        conn.close()


def _has_low_cardinality_text_values(study_id: str, column: str) -> bool | None:
    """Check if a column's values are non-numeric text with few distinct values.

    Returns True for columns like ``high_risk_cytogenetics`` (yes/no),
    False for columns with >5 distinct non-numeric values,
    None when the table can't be queried.
    """
    conn = get_connection(study_id)
    try:
        raw_table = f"raw_{study_id}"
        cur = conn.execute(
            f'SELECT DISTINCT "{column}" FROM {raw_table} WHERE "{column}" IS NOT NULL LIMIT 100'
        )
        values = [row[0] for row in cur.fetchall()]
        if not values:
            return None

        # Check if any value is non-numeric
        numeric_count = 0
        for v in values:
            try:
                float(v)
                numeric_count += 1
            except (ValueError, TypeError):
                pass

        # If values are numeric, no opinion
        if numeric_count == len(values):
            return None

        # Text values with ≤5 distinct options → likely baseline/demographic
        return len(values) <= 5
    except Exception:
        return None
    finally:
        conn.close()


def classify_variables_interactive(study_id: str, columns: list[str]) -> list[dict]:
    """CLI-guided variable classification.

    Uses heuristic name patterns AND actual data inspection.
    Unrecognized column names get role="unclassified" to force explicit
    resolution before planning.
    """
    results = []
    for col in columns:
        col_lower = col.lower()
        tokens = set(col_lower.replace("-", "_").split("_"))

        # ── Role (baseline / outcome / unclassified) ────────────────────
        if tokens & _OUTCOME_KEYWORDS or any(
            kw in col_lower for kw in ("outcome", "response", "survival",
                                       "pfs", "os", "status", "event", "death",
                                       "followup", "lab", "protein", "marker",
                                       "score", "level", "change", "fold")
        ):
            default_role = "outcome"
        elif tokens & {"id", "name", "patient", "identifier"}:
            continue  # skip identifier columns
        elif tokens & {"age", "bmi", "weight", "height", "creatinine",
                        "count", "number", "value", "line", "lines", "prior",
                        "sex", "gender", "stage", "site", "location",
                        "center", "cohort", "group", "arm", "ecog"}:
            default_role = "baseline"
        else:
            # If the column has low-cardinality text values (yes/no, mild/mod/severe),
            # it's almost certainly baseline even if the name is unfamiliar.
            is_low_card_text = _has_low_cardinality_text_values(study_id, col)
            if is_low_card_text is True:
                default_role = "baseline"
            else:
                # Unknown keyword pattern — flag for explicit user resolution
                default_role = "unclassified"

        # ── Data type (from actual values if possible) ──────────────────
        # Start with a name-based guess
        if any(kw in col_lower for kw in ("time", "days", "months", "duration", "follow_up", "fu")):
            name_based_dtype = "time_to_event"
        elif tokens & {"age", "bmi", "weight", "height", "value", "count",
                       "number", "creatinine", "line", "lines", "prior"}:
            name_based_dtype = "continuous"
        else:
            name_based_dtype = "categorical"

        # Use data inspection to *refine* — only upgrade categorical→continuous,
        # never override a known name-based type like time_to_event.
        inferred = _infer_data_type(study_id, col)
        if name_based_dtype == "categorical" and inferred == "continuous":
            default_dtype = "continuous"
        elif name_based_dtype == "categorical" and inferred == "categorical":
            default_dtype = "categorical"
        else:
            default_dtype = name_based_dtype

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
