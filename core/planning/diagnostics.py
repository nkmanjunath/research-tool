"""Diagnostic result data structures for pre/post-unmask assumption checks."""

from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class DiagnosticResult:
    """Structured result of a single diagnostic/assumption check.

    Serialized as part of the StudyPlan JSON under ``diagnostic_results``.
    Backward-compatible: the legacy ``warnings: dict[str, str]`` on StudyPlan
    remains a separate field for existing code.
    """
    check_name: str
    status: str
    value: float | None = None
    threshold: float | None = None
    message: str = ""
    forceable: bool = True
    stage: str = "pre_unmask"
    recorded_at: Optional[str] = None

    def __post_init__(self):
        if self.recorded_at is None:
            self.recorded_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> DiagnosticResult:
        return DiagnosticResult(**d)


def check_violation(row: dict) -> tuple[bool, str, list[dict]]:
    """Inspect an analysis_results row for post-unmask diagnostic violations.

    Parameters
    ----------
    row : dict
        A row from analysis_results, typically including status_json and
        ph_diagnostics_json.

    Returns
    -------
    (has_violation, violation_summary, violation_details)
        has_violation: True if any post-unmask diagnostic flags a violation.
        violation_summary: Short human-readable sentence.
        violation_details: list of dicts with keys (check_name, p_value, description).
    """
    details: list[dict] = []
    status_data = json.loads(row.get("status_json") or "{}")
    status = status_data.get("status", "completed")

    # Check 1: Schoenfeld proportional hazards violation
    ph_raw = row.get("ph_diagnostics_json")
    if ph_raw:
        ph = json.loads(ph_raw) if isinstance(ph_raw, str) else ph_raw
        for cov in ph.get("covariates", []):
            p = cov.get("p_value", 1)
            if p < 0.05:
                details.append({
                    "check_name": "proportional_hazards",
                    "p_value": p,
                    "description": f"PH violated for {cov.get('covariate', 'unknown')} (p={p:.4f})",
                })

    # Check 2: Explicit assumption_violation status not yet captured by a specific check.
    # This is a fallback for future violation types (separation, linearity) that don't
    # have their own structured fields yet — only fires when no more-specific check
    # already populated details.
    if status == "assumption_violation" and not details:
        reason = status_data.get("reason", "Unknown assumption violation")
        details.append({
            "check_name": "post_unmask_diagnostic",
            "p_value": None,
            "description": reason,
        })

    if not details:
        return False, "", []

    summary = "; ".join(d["description"] for d in details)
    return True, summary, details

