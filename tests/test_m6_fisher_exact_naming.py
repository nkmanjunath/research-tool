"""M6 regression: fisher_exact naming must be consistent everywhere.

Stats engine uses 'fishers_exact'. CLI suggestions and test_selector
messages must match, or users following suggestions get 'Unknown test'.
"""

from __future__ import annotations

import inspect

import pytest


class TestM6FisherExactNaming:
    def test_stats_engine_uses_fishers_exact(self):
        """The stats engine dispatch key must be 'fishers_exact'."""
        from core.stats.inferential import run_test
        source = inspect.getsource(run_test)
        assert '"fishers_exact"' in source or "'fishers_exact'" in source

    def test_cli_suggestion_uses_fishers_exact(self):
        """CLI alt_test mapping must suggest 'fishers_exact', not 'fisher_exact'."""
        from core.cli import main
        source = inspect.getsource(main.cmd_analyze)
        # Must NOT have bare "fisher_exact" (without 's') in alt_test mapping
        assert '"fisher_exact"' not in source, (
            "CLI still suggests 'fisher_exact' (without 's') — "
            "must use 'fishers_exact' to match stats engine. "
            "See DECISIONS.md §11 M6."
        )

    def test_test_selector_messages_use_fishers_exact(self):
        """test_selector suggestion text must use 'fishers_exact'."""
        from core.planning import test_selector

        # Check all functions in the module for bare "fisher_exact" (without 's')
        source = inspect.getsource(test_selector)
        # "fishers_exact" is correct — look for bare "fisher_exact" NOT preceded by 's'
        import re
        bare_fisher = re.findall(r'(?<![s])fisher_exact', source)
        assert not bare_fisher, (
            f"test_selector has bare 'fisher_exact' (without 's'): {bare_fisher}. "
            f"Must use 'fishers_exact'. See DECISIONS.md §11 M6."
        )
