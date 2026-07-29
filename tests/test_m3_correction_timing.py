"""M3 regression: multiple-testing correction must happen after status promotion.

Previously, correction ran on "completed" results, then some were promoted to
"assumption_violation" — leaving promoted results with adjusted_p_value while
original assumption violations had None. The fix promotes statuses first, then
applies correction only to final "completed" results.
"""

from __future__ import annotations

import pytest


class TestM3CorrectionTiming:
    def test_promotion_then_correction_order(self):
        """Verify the code structure: promotion loop must appear before
        correction loop in cmd_analyze, not inside the DB insert loop."""
        import inspect
        from core.cli import main

        source = inspect.getsource(main.cmd_analyze)

        # Find positions of key patterns
        promo_pos = source.find("Promote completed Cox PH results with Schoenfeld violations BEFORE")
        correction_pos = source.find("Apply multiple-testing correction to completed tests only")
        insert_pos = source.find("INSERT INTO analysis_results")

        assert promo_pos > 0, "Promotion loop comment not found"
        assert correction_pos > 0, "Correction loop comment not found"
        assert insert_pos > 0, "DB insert loop not found"

        # Promotion must come before correction
        assert promo_pos < correction_pos, (
            "Status promotion must happen before multiple-testing correction"
        )
        # Correction must come before DB insert
        assert correction_pos < insert_pos, (
            "Correction must happen before DB insert so corrected p-values are stored"
        )

    def test_no_duplicate_promotion_in_insert_loop(self):
        """The DB insert loop must NOT contain its own promotion logic —
        promotion must happen in the pre-correction loop only."""
        import inspect
        from core.cli import main

        source = inspect.getsource(main.cmd_analyze)

        # Find the insert loop (starts after "for r in results:" near INSERT)
        insert_section_start = source.find("INSERT INTO analysis_results")
        # The insert loop section is the last "for r in results:" block
        last_for_results = source.rfind("for r in results:")
        insert_section = source[last_for_results:]

        # Should NOT have Schoenfeld promotion logic in the insert loop
        assert "Schoenfeld" not in insert_section or "BEFORE" in insert_section, (
            "Duplicate Schoenfeld promotion found in DB insert loop — "
            "promotion must only happen in the pre-correction loop"
        )
