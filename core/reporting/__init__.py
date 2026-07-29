"""Reporting package — shared utilities for analysis result rendering."""

from __future__ import annotations

import json
from pathlib import Path

from core.database import DATA_ROOT


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


# ── D1: Shared plan loader ─────────────────────────────────────────────────

def latest_locked_plan(study_id: str) -> dict:
    """Return the latest locked plan dict, or {} if none exists."""
    locked = sorted(DATA_ROOT.glob(f"{study_id}/study_plan.v*.locked.json"))
    if not locked:
        return {}
    return json.loads(locked[-1].read_text())


# ── D2: Shared SVG escape (superset: escapes & < > " ') ────────────────────

def svg_escape(s: str) -> str:
    """Escape special characters for safe embedding in SVG/XML."""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))


# ── D3: Shared label formatter ──────────────────────────────────────────────

_ACRONYMS = frozenset({"PFS", "OS", "HR", "CI", "DFS", "ORR", "CR", "PR", "SD", "PD",
                        "ISS", "ECOG", "LDH", "BMI", "IQR", "KM", "PH"})


def format_label(raw: str) -> str:
    """Acronym-aware label: pfs_multivariable → PFS Multivariable."""
    parts = raw.replace("_", " ").split()
    out: list[str] = []
    for p in parts:
        upper = p.upper()
        if upper in _ACRONYMS:
            out.append(upper)
        else:
            out.append(p[0].upper() + p[1:] if p else p)
    return " ".join(out)
