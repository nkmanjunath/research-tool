"""Study plan data model — structured declaration of analytical intent.

The plan must be fully declared *before* unmasking outcome data.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class PlannedTest:
    variable_id: int
    variable_name: str
    test_name: str
    rationale: str = ""


@dataclass
class StudyPlan:
    study_id: str
    version: int = 1
    locked_at: Optional[str] = None
    study_type: str = "cohort"  # cohort | case_control | cross_sectional
    primary_comparison: str = ""
    primary_outcome_variable_ids: list[int] = field(default_factory=list)
    planned_tests: list[dict] = field(default_factory=list)
    covariates: list[int] = field(default_factory=list)
    matching_criteria: list[int] = field(default_factory=list)
    warnings: dict[str, str] = field(default_factory=dict)
    role_overrides: dict[int, str] = field(default_factory=dict)
    audit: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["locked_at"] = self.locked_at or datetime.now(timezone.utc).isoformat()
        return d

    @staticmethod
    def from_dict(d: dict) -> StudyPlan:
        d.setdefault("warnings", {})
        d.setdefault("role_overrides", {})
        d.setdefault("audit", {})
        d.setdefault("matching_criteria", [])
        d["role_overrides"] = {int(k): v for k, v in d["role_overrides"].items()}
        return StudyPlan(**d)
