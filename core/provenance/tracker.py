"""Provenance tracker — maps every computed statistic to its source lineage.

Given a statistic (test_name, variable_ids), returns:
  - source row IDs from raw data that contributed
  - column names used
  - the exact function name and parameters
  - timestamp

Stored as JSON per analysis run.  Queryable by statistic name or variable.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

from core.database import DATA_ROOT


@dataclass
class ProvenanceEntry:
    function_name: str
    parameters: dict
    source_row_ids: list[int]
    column_names: list[str]
    test_name: str
    result_id: str
    study_id: str
    computed_at: str = ""
    is_pre_registered: bool = True

    def __post_init__(self):
        if not self.computed_at:
            self.computed_at = datetime.now(timezone.utc).isoformat()


class ProvenanceTracker:
    """Tracks provenance for a single study.

    Usage:
        tracker = ProvenanceTracker("study_id")
        entry = ProvenanceEntry(...)
        tracker.record(entry)
        lineage = tracker.get_lineage(test_name="chi_square")
    """

    def __init__(self, study_id: str):
        self.study_id = study_id
        self._entries: list[ProvenanceEntry] = []
        self._load()

    def _path(self) -> Path:
        return DATA_ROOT / self.study_id / "provenance.json"

    def _load(self):
        p = self._path()
        if p.exists():
            data = json.loads(p.read_text())
            self._entries = [ProvenanceEntry(**e) for e in data]

    def _save(self):
        self._path().write_text(
            json.dumps([asdict(e) for e in self._entries], indent=2)
        )

    def record(self, entry: ProvenanceEntry):
        self._entries.append(entry)
        self._save()

    def record_run(
        self,
        function_name: str,
        parameters: dict,
        source_row_ids: list[int],
        column_names: list[str],
        test_name: str,
        result_id: str,
        is_pre_registered: bool = True,
    ):
        entry = ProvenanceEntry(
            function_name=function_name,
            parameters=parameters,
            source_row_ids=source_row_ids,
            column_names=column_names,
            test_name=test_name,
            result_id=result_id,
            study_id=self.study_id,
            is_pre_registered=is_pre_registered,
        )
        self.record(entry)

    def get_lineage(
        self,
        test_name: str | None = None,
        variable_id: int | None = None,
    ) -> list[ProvenanceEntry]:
        """Return matching provenance entries."""
        results = self._entries
        if test_name:
            results = [e for e in results if e.test_name == test_name]
        return results

    def get_all(self) -> list[ProvenanceEntry]:
        return list(self._entries)
