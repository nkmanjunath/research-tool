"""M2 regression: data hash must use canonical JSON (sorted keys) everywhere.

Previously cmd_export used json.dumps (no sorted keys) and read from a
different table, producing a different hash than bundle.py for the same data.
"""

from __future__ import annotations

from core.provenance.hashing import sha256, canonical_json, compute_raw_data_hash


class TestM2HashConsistency:
    def test_canonical_json_is_sorted(self):
        """canonical_json must sort keys for deterministic hashing."""
        obj = {"z": 1, "a": 2, "m": 3}
        result = canonical_json(obj)
        assert result == '{"a":2,"m":3,"z":1}'

    def test_canonical_json_compact_separators(self):
        """canonical_json must use compact separators (no extra whitespace)."""
        obj = {"key": [1, 2]}
        result = canonical_json(obj)
        assert ": " not in result  # no space after colon
        assert ", " not in result  # no space after comma

    def test_sha256_string_and_bytes_match(self):
        """sha256 must produce the same hash whether given str or bytes."""
        data = "test data"
        assert sha256(data) == sha256(data.encode("utf-8"))

    def test_sha256_deterministic(self):
        """Same input must always produce the same hash."""
        h1 = sha256("deterministic test")
        h2 = sha256("deterministic test")
        assert h1 == h2

    def test_compute_raw_data_hash_available(self):
        """compute_raw_data_hash must be importable as the single entry point
        for raw data hashing in cmd_export, bundle, and excel_export."""
        assert callable(compute_raw_data_hash)
