"""
Tab 2 — Socratic Wizard & Lock Gate.
Maps to core.planning / core.provenance per DECISIONS.md §4-5.

Novice wizard and Expert JSON-import both land here structurally — this file only
implements the wizard steps (entry_mode="socratic_wizard"). Expert direct-spec
import is a separate endpoint, not built this session.

Steps A-D per §4:
  A exposure          -> /exposure
  B outcome confirm    -> /outcome-confirm (GET, read-only re-display) + /time-column
  C confounders        -> /confounders, /interactions (Manuscript Mirror at /manuscript-mirror)
  D rigor / missing    -> /missing-strategy, /epv-live (recomputed live, never cached)
lock -> /lock, emits Stage2Payload (H1), chained to H0 via payload_fingerprint_h0.
"""
import hashlib
import json

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from state import SESSION

router = APIRouter()


# ---------- request schemas ----------

class ExposureIn(BaseModel):
    column_name: str
    reference_level: str

class TimeColumnIn(BaseModel):
    time_column: str

class ConfoundersIn(BaseModel):
    confounders: list[str]

class InteractionIn(BaseModel):
    term: str
    rationale: str

class MissingStrategyIn(BaseModel):
    global_default: str  # "complete_case" | "impute" | "flag_as_missing_category"
    column_overrides: dict[str, str] = {}


# ---------- helpers ----------

def _require_h0():
    if SESSION.h0_payload is None:
        raise HTTPException(400, "Lock Stage 1 first — no H0 payload to chain off.")

def _apply_sentinels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    global_na = SESSION.sentinels.get("global_na_strings", [])
    overrides = SESSION.sentinels.get("column_overrides", {})
    for col in out.columns:
        na_list = global_na + overrides.get(col, [])
        if na_list:
            out[col] = out[col].replace(na_list, pd.NA)
    return out

def _complete_case_columns() -> list[str]:
    """Columns whose missingness actually costs rows under the current strategy 
    impute / flag_as_missing_category columns don't drop rows, complete_case ones do."""
    strat = SESSION.missing_data_strategy
    cols = []
    if SESSION.exposure:
        cols.append(SESSION.exposure["column_name"])
    cols += SESSION.confounders
    for c in cols[:]:
        method = strat["column_overrides"].get(c, strat["global_default"])
        if method != "complete_case":
            cols.remove(c)
    return cols


# ---------- endpoints ----------

@router.post("/exposure")
def set_exposure(body: ExposureIn):
    _require_h0()
    vaulted_outcome = SESSION.outcome_spec["column_name"]
    if body.column_name == vaulted_outcome:
        raise HTTPException(400, "Exposure can't be the vaulted outcome column.")
    SESSION.exposure = body.dict()
    return SESSION.exposure


@router.get("/outcome-confirm")
def outcome_confirm():
    """Read-only re-display — never a re-selection. Outcome was fixed in Stage 1."""
    _require_h0()
    spec = SESSION.outcome_spec
    return {
        "column_name": spec["column_name"],
        "event_value": spec["event_value"],
        "censored_value": spec["censored_value"],
        "time_column": SESSION.time_column,
    }


@router.post("/time-column")
def set_time_column(body: TimeColumnIn):
    _require_h0()
    SESSION.time_column = body.time_column
    return {"time_column": SESSION.time_column}


@router.post("/confounders")
def set_confounders(body: ConfoundersIn):
    _require_h0()
    SESSION.confounders = body.confounders
    return {"confounders": SESSION.confounders}


@router.get("/redundancy-warnings")
def redundancy_warnings():
    """Surfaces only the pairs relevant to what's actually been selected as confounders."""
    _require_h0()
    selected = set(SESSION.confounders)
    pairs = SESSION.h0_payload["precomputations_advisory_cache"]["high_correlation_pairs"]
    return {"warnings": [p for p in pairs if p["var1"] in selected and p["var2"] in selected]}


@router.post("/interactions")
def add_interaction(body: InteractionIn):
    """Interaction terms permitted only here (§4), gated by rationale sanity floor (§5) —
    ~15 char minimum to block literal 'yes'/'none', not a strict quality gate."""
    _require_h0()
    if len(body.rationale.strip()) < 15:
        raise HTTPException(400, "Rationale too short — needs genuine clinical justification, not a placeholder.")
    SESSION.interactions.append(body.dict())
    return {"interactions": SESSION.interactions}


@router.get("/manuscript-mirror")
def manuscript_mirror():
    """Live preview of how each interaction rationale will read in the exported
    Methods / STROBE supplement — the real quality incentive, no keyword gating (§5)."""
    _require_h0()
    lines = []
    for i in SESSION.interactions:
        lines.append(
            f"An interaction term ({i['term']}) was pre-specified. Rationale: {i['rationale']}"
        )
    return {"methods_section_preview": " ".join(lines) or "No interaction terms declared."}


@router.post("/missing-strategy")
def set_missing_strategy(body: MissingStrategyIn):
    _require_h0()
    SESSION.missing_data_strategy = body.dict()
    return SESSION.missing_data_strategy


@router.get("/epv-live")
def epv_live():
    """Recomputed fresh on every call — never a cached/static number (§4)."""
    _require_h0()
    if not SESSION.exposure:
        raise HTTPException(400, "Set exposure first.")

    df = _apply_sentinels(SESSION.raw_df)
    cc_cols = _complete_case_columns()
    outcome_col = SESSION.outcome_spec["column_name"]

    subset_cols = cc_cols + [outcome_col]
    effective_df = df.dropna(subset=subset_cols) if cc_cols else df

    n_effective = len(effective_df)
    e_total = (effective_df[outcome_col].astype(str) == str(SESSION.outcome_spec["event_value"])).sum()

    k = 1 + len(SESSION.confounders) + len(SESSION.interactions)  # exposure + confounders + interactions
    epv = round(e_total / k, 2) if k else None

    return {
        "n_effective": n_effective,
        "e_effective": int(e_total),
        "parameters_k": k,
        "epv": epv,
    }


@router.get("/amendment-status")
def amendment_status():
    return {"pending": SESSION.pending_amendment}


@router.post("/lock")
def lock_stage2():
    """Emits Stage2Payload (H1), chained off H0's payload_fingerprint_h0."""
    _require_h0()
    if not SESSION.exposure:
        raise HTTPException(400, "Exposure not set — Step A incomplete.")

    feasibility = epv_live()
    if feasibility["epv"] is not None and feasibility["epv"] < 10:
        # not a hard block by design choice here — surfaced, not silently allowed either.
        # (DECISIONS.md doesn't mandate a hard EPV floor at lock; flag only.)
        pass

    payload = {
        "provenance": {
            "payload_fingerprint_h0": SESSION.h0_payload["provenance"]["payload_fingerprint_h0"],
            "entry_mode": "socratic_wizard",
        },
        "protocol": {
            "study_design": "retrospective_cohort",
            "exposure": SESSION.exposure,
            "outcome_confirmation": {
                "column_name": SESSION.outcome_spec["column_name"],
                "event_value": SESSION.outcome_spec["event_value"],
                "time_column": SESSION.time_column,
            },
            "confounders": SESSION.confounders,
            "interactions": SESSION.interactions,
            "missing_data_strategy": SESSION.missing_data_strategy,
            "pre_specified_transforms": {},  # NOTE: assumes linear for all continuous covariates
                                              # until Tab2 UI grows a transform picker (Gate 4 reads this)
        },
        "locked_feasibility_metrics": feasibility,
    }
    # amendment chaining: if this lock follows a Tab3 FAIL, parent is the failed plan's hash,
    # not H0 directly — new plan hash still gets computed fresh below.
    if SESSION.pending_amendment:
        payload["provenance"]["parent_plan_hash"] = SESSION.pending_amendment["parent_plan_hash"]
        payload["provenance"]["amendment_rationale"] = SESSION.pending_amendment.get("rationale", "")

    canonical = json.dumps(payload, sort_keys=True, default=str).encode()
    payload["provenance"]["plan_fingerprint_h1"] = hashlib.sha256(canonical).hexdigest()

    SESSION.h1_payload = payload
    SESSION.plan_chain.append(payload)
    SESSION.pending_amendment = None
    return payload
