"""M4 regression: dedup queries must include variable_name in the key.

Previously, dedup used variable_ids_used=? with json.dumps([]) — always
empty. Two tests of the same type on different variables would collide,
causing the second to be incorrectly skipped.
"""

from __future__ import annotations

import inspect

import pytest


class TestM4DedupField:
    def test_dedup_queries_use_variable_name_not_empty_list(self):
        """All dedup queries in cmd_analyze must use [var_name] or
        [model_name] in the variable_ids_used parameter, not json.dumps([])."""
        from core.cli import main

        source = inspect.getsource(main.cmd_analyze)

        # Find all json.dumps([]) usages — there should be none in dedup context
        # The only acceptable json.dumps([]) would be in non-dedup contexts
        lines = source.split("\n")
        empty_dumps_in_dedup = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if 'json.dumps([])' in stripped:
                # Check surrounding context for dedup indicators
                context = "\n".join(lines[max(0, i-5):i+5])
                if "variable_ids_used" in context:
                    empty_dumps_in_dedup.append((i + 1, stripped))

        assert not empty_dumps_in_dedup, (
            f"Found json.dumps([]) in dedup context at lines: "
            f"{empty_dumps_in_dedup}. Must use [var_name] or [model_name]. "
            f"See DECISIONS.md §11 M4."
        )

    def test_insert_stores_variable_name(self):
        """The INSERT INTO analysis_results must store [variable_name]
        in variable_ids_used, not an empty list."""
        from core.cli import main

        source = inspect.getsource(main.cmd_analyze)
        # The INSERT should use r.get("variable_name") not json.dumps([])
        insert_section_start = source.find("INSERT INTO analysis_results")
        insert_section = source[insert_section_start:]

        assert 'json.dumps([])' not in insert_section, (
            "INSERT still uses json.dumps([]) for variable_ids_used — "
            "must use json.dumps([r.get('variable_name', '')]) instead. "
            "See DECISIONS.md §11 M4."
        )
        assert "variable_name" in insert_section, (
            "INSERT does not reference variable_name — dedup will still collide. "
            "See DECISIONS.md §11 M4."
        )
