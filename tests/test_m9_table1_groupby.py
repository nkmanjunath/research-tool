"""M9 regression: cmd_table1 must read exposure column from locked plan.

Previously hardcoded to "treatment_arm". Studies using different column
names got unstratified Table 1 with no warning.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from core.planning.study_plan import StudyPlan


class TestM9Table1Groupby:
    def test_study_plan_has_primary_treatment_col(self):
        """StudyPlan must have a primary_treatment_col field."""
        field_names = {f.name for f in dataclasses.fields(StudyPlan)}
        assert "primary_treatment_col" in field_names, (
            "StudyPlan missing primary_treatment_col field. See DECISIONS.md §11 M9."
        )

    def test_primary_treatment_col_defaults_empty(self):
        """primary_treatment_col must default to empty string."""
        plan = StudyPlan(study_id="test")
        assert plan.primary_treatment_col == ""

    def test_primary_treatment_col_round_trips(self):
        """primary_treatment_col must survive to_dict/from_dict."""
        plan = StudyPlan(study_id="test", primary_treatment_col="arm")
        d = plan.to_dict()
        restored = StudyPlan.from_dict(d)
        assert restored.primary_treatment_col == "arm"

    def test_cmd_table1_reads_from_plan(self):
        """cmd_table1 must not hardcode 'treatment_arm' — must read from plan."""
        from core.cli import main
        source = inspect.getsource(main.cmd_table1)
        # Should load plan to get primary_treatment_col
        assert "primary_treatment_col" in source, (
            "cmd_table1 doesn't read primary_treatment_col from plan. "
            "See DECISIONS.md §11 M9."
        )

    def test_old_plans_without_field_still_work(self):
        """Plans loaded from old lock files (without primary_treatment_col)
        must default gracefully."""
        old_data = {
            "study_id": "test",
            "version": 1,
            "primary_comparison": "test",
        }
        plan = StudyPlan.from_dict(old_data)
        assert plan.primary_treatment_col == ""
