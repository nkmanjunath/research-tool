"""M7 regression: canonical StudyPlan lives in core.planning.study_plan only.

Prevents re-introduction of a stale StudyPlan in core/models.py that's missing
fields (study_type, matching_criteria, post_hoc_tests, amendment_reason,
cox_ph_models, diagnostic_results) and would cause silent data corruption.
"""

from __future__ import annotations
import dataclasses
import importlib
import inspect

import pytest


REQUIRED_FIELDS = {
    "study_type",
    "matching_criteria",
    "post_hoc_tests",
    "amendment_reason",
    "cox_ph_models",
    "diagnostic_results",
}


class TestM7SingleSourceOfTruth:
    def test_canonical_study_plan_has_all_required_fields(self):
        """The canonical StudyPlan in core.planning.study_plan must have all
        fields that were missing from the stale copy in core.models."""
        from core.planning.study_plan import StudyPlan

        field_names = {f.name for f in dataclasses.fields(StudyPlan)}
        missing = REQUIRED_FIELDS - field_names
        assert not missing, f"Canonical StudyPlan missing fields: {missing}"

    def test_core_models_does_not_define_study_plan(self):
        """core.models must not re-define a StudyPlan class.
        If someone adds one back, this test catches it immediately."""
        mod = importlib.import_module("core.models")
        assert not hasattr(mod, "StudyPlan"), (
            "StudyPlan must NOT be defined in core.models — "
            "use core.planning.study_plan.StudyPlan instead. "
            "See DECISIONS.md §11 M7."
        )

    def test_canonical_study_plan_round_trips_missing_fields(self):
        """from_dict must default the 6 fields that the stale copy was missing,
        ensuring data loaded from old lock files still works."""
        from core.planning.study_plan import StudyPlan

        minimal = {"study_id": "test", "version": 1}
        plan = StudyPlan.from_dict(minimal)
        d = plan.to_dict()
        for field_name in REQUIRED_FIELDS:
            assert field_name in d, f"Round-trip lost field: {field_name}"
