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
class CoxPHModel:
    """Multivariable Cox Proportional Hazards model declaration."""
    model_name: str
    survival_time_col: str
    event_col: str
    primary_treatment_col: str
    covariate_cols: list[str]
    rationale: str = ""
    interaction_terms: list[list[str]] = field(default_factory=list)


@dataclass
class StudyPlan:
    study_id: str
    version: int = 1
    locked_at: Optional[str] = None
    study_type: str = "cohort"  # cohort | case_control | cross_sectional
    primary_comparison: str = ""
    primary_treatment_col: str = ""  # actual column name for groupby (M9)
    primary_outcome_variable_ids: list[int] = field(default_factory=list)
    planned_tests: list[dict] = field(default_factory=list)
    covariates: list[int] = field(default_factory=list)
    matching_criteria: list[int] = field(default_factory=list)
    warnings: dict[str, str] = field(default_factory=dict)
    role_overrides: dict[int, str] = field(default_factory=dict)
    audit: dict = field(default_factory=dict)
    post_hoc_tests: list[dict] = field(default_factory=list)
    amendment_reason: str = ""
    cox_ph_models: list[CoxPHModel] = field(default_factory=list)
    diagnostic_results: list[dict] = field(default_factory=list)

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
        d.setdefault("primary_treatment_col", "")
        d.setdefault("post_hoc_tests", [])
        d.setdefault("amendment_reason", "")
        d.setdefault("cox_ph_models", [])
        d.setdefault("diagnostic_results", [])
        d.pop("content_hash", None)  # lock file artifact, not a model field
        d["role_overrides"] = {int(k): v for k, v in d["role_overrides"].items()}
        # Convert cox_ph_models dicts to CoxPHModel objects
        if "cox_ph_models" in d and d["cox_ph_models"]:
            d["cox_ph_models"] = [CoxPHModel(**m) if isinstance(m, dict) else m for m in d["cox_ph_models"]]
        return StudyPlan(**d)
