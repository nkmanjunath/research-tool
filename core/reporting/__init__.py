"""Reporting package — shared utilities for analysis result rendering."""


def filter_superseded(rows: list) -> list:
    """Remove superseded analysis results (those replaced by a --rerun).

    A result is superseded if its ``id`` appears as another row's
    ``superseded_previous_result_id``.
    """
    superseded_ids = {
        r["superseded_previous_result_id"]
        for r in rows
        if r["superseded_previous_result_id"] is not None
    }
    return [r for r in rows if r["id"] not in superseded_ids]
