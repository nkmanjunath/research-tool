"""
Tab 1 — Data & Schema (Blinded Inspection).
Maps to core.ingestion / core.masking.blind per DECISIONS.md tab-to-CLI mapping.

Read-only inspection only. No drop/impute/interaction here (§3). Outcome column,
once declared, is vaulted from variable browser / redundancy / cross-tabs for the
rest of Tab 1 — everything below enforces that by filtering it out of responses,
except the one fixed cohort banner (explicitly allowed, §3).

NOTE: dtype inference and correlation/cross-tab logic below are simple pandas
implementations to get Tab 1 running end-to-end. Swap for the real
core.ingestion.variable_classifier / core stats calls when wiring the actual
v2.7.0-core engine in — kept as thin as possible so that swap is a drop-in.
"""
import hashlib
import io
import json
from itertools import combinations

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from state import SESSION

router = APIRouter()


# ---------- request schemas ----------

class ColumnMapping(BaseModel):
    name: str
    type: str  # "identifier" | "numeric_covariate" | "categorical_covariate" | "primary_outcome" | "time_to_event"

class SchemaIn(BaseModel):
    column_mappings: list[ColumnMapping]

class OutcomeIn(BaseModel):
    column_name: str
    event_value: str
    censored_value: str

class SentinelsIn(BaseModel):
    global_na_strings: list[str] = []
    column_overrides: dict[str, list[str]] = {}


# ---------- helpers ----------

def _require_data():
    if SESSION.raw_df is None:
        raise HTTPException(400, "No dataset uploaded yet. POST /api/ingest/upload first.")

def _outcome_col():
    return SESSION.outcome_spec["column_name"] if SESSION.outcome_vaulted() else None

def _apply_sentinels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    global_na = SESSION.sentinels.get("global_na_strings", [])
    overrides = SESSION.sentinels.get("column_overrides", {})
    for col in out.columns:
        na_list = global_na + overrides.get(col, [])
        if na_list:
            out[col] = out[col].replace(na_list, pd.NA)
    return out

def _visible_columns() -> list[str]:
    """Columns minus the vaulted outcome identity."""
    outcome = _outcome_col()
    return [c for c in SESSION.raw_df.columns if c != outcome]


# ---------- endpoints ----------

@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    raw_bytes = await file.read()
    SESSION.dataset_fingerprint = hashlib.sha256(raw_bytes).hexdigest()
    SESSION.raw_path = file.filename

    if file.filename.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw_bytes))
    else:
        df = pd.read_csv(io.BytesIO(raw_bytes))
    SESSION.raw_df = df

    # naive dtype guess — placeholder for core.ingestion.variable_classifier
    guessed = []
    for col in df.columns:
        if df[col].nunique() == len(df[col]) and df[col].is_unique:
            t = "identifier"
        elif pd.api.types.is_numeric_dtype(df[col]):
            t = "numeric_covariate"
        else:
            t = "categorical_covariate"
        guessed.append({"name": col, "guessed_type": t})

    return {
        "dataset_fingerprint": SESSION.dataset_fingerprint,
        "n_rows": len(df),
        "columns": guessed,
    }


@router.post("/schema")
def set_schema(body: SchemaIn):
    _require_data()
    mappings = []
    for m in body.column_mappings:
        entry = {"name": m.name, "type": m.type}
        if m.type == "identifier":
            entry["has_duplicates"] = bool(SESSION.raw_df[m.name].duplicated().any())
        mappings.append(entry)
    SESSION.column_mappings = mappings
    return {"column_mappings": mappings}


@router.post("/outcome")
def declare_outcome(body: OutcomeIn):
    """Direct declaration only — never auto-detected (§3). Vaults the column immediately."""
    _require_data()
    if body.column_name not in SESSION.raw_df.columns:
        raise HTTPException(400, f"Column '{body.column_name}' not found.")
    SESSION.outcome_spec = {
        "column_name": body.column_name,
        "event_value": body.event_value,
        "censored_value": body.censored_value,
        "vaulted": True,
    }
    return {"outcome_spec": {"vaulted": True}}  # column identity not echoed back either


@router.post("/sentinels")
def set_sentinels(body: SentinelsIn):
    _require_data()
    SESSION.sentinels = {
        "global_na_strings": body.global_na_strings,
        "column_overrides": body.column_overrides,
    }
    return SESSION.sentinels


@router.get("/missingness")
def missingness():
    _require_data()
    df = _apply_sentinels(SESSION.raw_df)
    cols = _visible_columns()
    return {c: round(float(df[c].isna().mean()), 4) for c in cols}


@router.get("/redundancy")
def redundancy():
    """Covariate-covariate correlation only — outcome excluded. |r| > 0.8 flagged (§3)."""
    _require_data()
    df = _apply_sentinels(SESSION.raw_df)
    numeric_cols = [
        c for c in _visible_columns()
        if pd.api.types.is_numeric_dtype(df[c])
    ]
    pairs = []
    for a, b in combinations(numeric_cols, 2):
        r = df[[a, b]].corr().iloc[0, 1]
        if pd.notna(r) and abs(r) > 0.8:
            pairs.append({"var1": a, "var2": b, "r": round(float(r), 3)})
    return {"high_correlation_pairs": pairs}


@router.get("/sparse-crosstabs")
def sparse_crosstabs(min_cell_threshold: int = 5):
    """Categorical-categorical sparse cell check — outcome excluded."""
    _require_data()
    df = _apply_sentinels(SESSION.raw_df)
    cat_cols = [
        c for c in _visible_columns()
        if not pd.api.types.is_numeric_dtype(df[c])
    ]
    flagged = []
    for a, b in combinations(cat_cols, 2):
        ct = pd.crosstab(df[a], df[b])
        if ct.size and ct.values.min() < min_cell_threshold:
            flagged.append({"var1": a, "var2": b, "min_cell_count": int(ct.values.min())})
    return {"sparse_cross_tabs": flagged}


@router.get("/cohort-banner")
def cohort_banner():
    """The one allowed exception: touches outcome once, fixed, not recomputed per action (§3)."""
    _require_data()
    if not SESSION.outcome_vaulted():
        raise HTTPException(400, "Declare outcome first — banner needs event count.")
    df = SESSION.raw_df
    spec = SESSION.outcome_spec
    n_total = len(df)
    e_total = int((df[spec["column_name"]].astype(str) == str(spec["event_value"])).sum())
    return {
        "n_total": n_total,
        "e_total": e_total,
        "event_rate": round(e_total / n_total, 4) if n_total else 0.0,
    }


@router.post("/lock")
def lock_stage1():
    """Emits Stage1Payload (H0) per DECISIONS.md §3. Hash excludes itself, computed then inserted."""
    _require_data()
    if not SESSION.outcome_vaulted():
        raise HTTPException(400, "Cannot lock Stage 1 without a declared outcome.")

    banner = cohort_banner()
    miss = missingness()
    redund = redundancy()
    sparse = sparse_crosstabs()

    payload = {
        "provenance": {
            "dataset_fingerprint": SESSION.dataset_fingerprint,
            "ephemeral_raw_path": SESSION.raw_path,
        },
        "cohort_facts": banner,
        "outcome_spec": SESSION.outcome_spec,
        "column_mappings": SESSION.column_mappings,
        "sentinels": SESSION.sentinels,
        "precomputations_advisory_cache": {
            "is_advisory": True,
            "missingness_pct": miss,
            "high_correlation_pairs": redund["high_correlation_pairs"],
            "sparse_cross_tabs": sparse["sparse_cross_tabs"],
        },
    }
    canonical = json.dumps(payload, sort_keys=True, default=str).encode()
    payload["provenance"]["payload_fingerprint_h0"] = hashlib.sha256(canonical).hexdigest()
    SESSION.h0_payload = payload  # Tab 2 chains plan_fingerprint_h1 off this
    return payload
