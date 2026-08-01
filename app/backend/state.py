"""
Single-user, single-session, in-memory state.
Phase 1 = one dataset at a time, no auth, no concurrency. A restart clears everything —
that's fine, Stage1Payload gets re-derivable from the raw file + declared rules, nothing
here is authoritative except what's written into the payload JSON at lock time.
"""
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class SessionState:
    raw_df: Optional[pd.DataFrame] = None
    dataset_fingerprint: Optional[str] = None
    raw_path: Optional[str] = None

    column_mappings: list = field(default_factory=list)   # [{name, type, has_duplicates}]
    outcome_spec: Optional[dict] = None                    # vaulted once set
    sentinels: dict = field(
        default_factory=lambda: {"global_na_strings": [], "column_overrides": {}}
    )
    h0_payload: Optional[dict] = None  # set once Tab1 /lock is called — Tab2 chains off this

    # --- Tab 2 (Socratic Wizard) draft state, hashed at /plan/lock into Stage2Payload ---
    exposure: Optional[dict] = None            # {column_name, reference_level}
    time_column: Optional[str] = None          # survival designs only
    confounders: list = field(default_factory=list)
    interactions: list = field(default_factory=list)   # [{term, rationale}]
    missing_data_strategy: dict = field(
        default_factory=lambda: {"global_default": "complete_case", "column_overrides": {}}
    )

    # --- Tab 3 (Execution) state ---
    h1_payload: Optional[dict] = None       # last locked plan (H1, or latest amendment Hn)
    plan_chain: list = field(default_factory=list)     # every locked plan version, in order
    pending_amendment: Optional[dict] = None            # set on a FAIL gate, cleared once re-locked
    hexec_payload: Optional[dict] = None    # only set on PASS/WARNING route

    def outcome_vaulted(self) -> bool:
        return self.outcome_spec is not None and self.outcome_spec.get("vaulted", False)


SESSION = SessionState()
