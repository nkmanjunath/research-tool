"""M5 regression: Excel audit hash must include ph_diagnostics_json.

Previously, the results hash in excel_export.py decoded JSON fields but
omitted ph_diagnostics_json, causing the Excel audit hash to differ from
the bundle hash for Cox PH studies.
"""

from __future__ import annotations

import inspect

import pytest


class TestM5ExcelHashScope:
    def test_excel_export_decodes_ph_diagnostics_json(self):
        """The JSON field list in excel_export's hash computation must
        include ph_diagnostics_json to match the bundle hash."""
        from core.reporting import excel_export

        source = inspect.getsource(excel_export._build_tab4_audit)
        assert "ph_diagnostics_json" in source, (
            "ph_diagnostics_json missing from Excel audit hash JSON fields. "
            "See DECISIONS.md §11 M5."
        )

    def test_excel_and_bundle_decode_same_json_fields(self):
        """Excel export and bundle must decode the same set of JSON fields
        for hash computation to produce identical hashes."""
        from core.reporting import excel_export, bundle

        excel_src = inspect.getsource(excel_export._build_tab4_audit)
        bundle_src = inspect.getsource(bundle._export_analysis_results)

        required_fields = [
            "variable_ids_used", "effect_size_json", "sample_counts_json",
            "status_json", "provenance_json", "ph_diagnostics_json",
        ]
        for field_name in required_fields:
            assert field_name in excel_src, (
                f"{field_name} missing from excel_export hash computation"
            )
            assert field_name in bundle_src, (
                f"{field_name} missing from bundle export computation"
            )
