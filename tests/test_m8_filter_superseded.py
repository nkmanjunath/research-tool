"""M8 regression: _filter_superseded must be a single shared implementation.

Previously duplicated in strobe_checklist.py and manuscript_draft.py.
Divergence risk if one is updated and the other isn't.
"""

from __future__ import annotations

import pytest

from core.reporting import filter_superseded


class TestM8FilterSuperseded:
    def test_shared_implementation_available(self):
        """filter_superseded must be importable from core.reporting."""
        assert callable(filter_superseded)

    def test_removes_superseded_rows(self):
        """Rows whose id appears as another's superseded_previous_result_id
        must be filtered out."""
        rows = [
            {"id": 1, "superseded_previous_result_id": None},
            {"id": 2, "superseded_previous_result_id": 1},  # supersedes id=1
            {"id": 3, "superseded_previous_result_id": None},
        ]
        result = filter_superseded(rows)
        ids = [r["id"] for r in result]
        assert ids == [2, 3]

    def test_no_superseded_rows(self):
        """When no rows are superseded, all rows are returned."""
        rows = [
            {"id": 1, "superseded_previous_result_id": None},
            {"id": 2, "superseded_previous_result_id": None},
        ]
        result = filter_superseded(rows)
        assert len(result) == 2

    def test_strobe_uses_shared(self):
        """strobe_checklist must import from core.reporting, not define its own."""
        import inspect
        from core.reporting import strobe_checklist
        source = inspect.getsource(strobe_checklist)
        assert "def _filter_superseded" not in source, (
            "strobe_checklist still defines local _filter_superseded. "
            "Must import from core.reporting. See DECISIONS.md §11 M8."
        )

    def test_manuscript_uses_shared(self):
        """manuscript_draft must import from core.reporting, not define its own."""
        import inspect
        from core.reporting import manuscript_draft
        source = inspect.getsource(manuscript_draft)
        assert "def _filter_superseded" not in source, (
            "manuscript_draft still defines local _filter_superseded. "
            "Must import from core.reporting. See DECISIONS.md §11 M8."
        )
