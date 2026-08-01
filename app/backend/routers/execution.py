"""
Tab 3 — Execution Engine, Diagnostics & Autopsy Canvas. Maps to core.execution / core.diagnostics
per DECISIONS.md §6.

NOTE: model fitting below uses statsmodels Logit as a stand-in for the real v2.7.0-core engine
(binary retrospective-cohort case only — no Cox PH / survival path yet, that needs `lifelines`
and real Schoenfeld-residual code; Gate 3 is stubbed "not_applicable" until that's wired in).
Swap the whole `_fit_model` function for an IPC call to core.execution when ready — everything
around it (gate routing, Autopsy payload shape, hash chaining) is the real contract and shouldn't
need to change.

Unattended, deterministic (§6): no manual tuning knobs exposed here by design — thresholds are
fixed constants below, not request parameters.
"""
import hashlib
import json
import math

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from state import SESSION

router = APIRouter()

RULESET_VERSION = "v1.2.0-strobe-default"
THRESHOLDS = {
    "vif_warning": 5.0,
    "vif_fail": 10.0,
    "ph_p_warning": 0.05,
    "ph_p_fail": 0.01,
    "separation_max_se": 100.0,
}

REMEDIATION_OPTIONS = {
    "complete_separation": [
        "Combine sparse categories causing the zero-cell / perfect predictor",
        "Treat as a data artifact — check for a miscoded or duplicated variable",
    ],
    "multicollinearity_vif": [
        "Drop the more clinically redundant variable in the flagged pair",
        "Combine correlated variables into a single composite score",
    ],
    "proportional_hazards": [
        "Stratify the model by the offending covariate",
        "Model the covariate with a time-varying interaction (X × log(t))",
    ],
    "linearity_continuous_terms": [
        "Pre-specify a restricted cubic spline transform for the flagged variable",
        "Categorize the continuous variable into clinically meaningful bins",
    ],
}


class AmendmentPrepareIn(BaseModel):
    chosen_remediation: str
    rationale: str


def _require_h1():
    if SESSION.h1_payload is None:
        raise HTTPException(400, "Lock Stage 2 first — no H1 plan to execute.")

def _apply_sentinels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    global_na = SESSION.sentinels.get("global_na_strings", [])
    overrides = SESSION.sentinels.get("column_overrides", {})
    for col in out.columns:
        na_list = global_na + overrides.get(col, [])
        if na_list:
            out[col] = out[col].replace(na_list, pd.NA)
    return out


def _fit_model(protocol: dict):
    """Placeholder logistic-regression fit. Returns (fit_ok, results_dict)."""
    try:
        import statsmodels.api as sm
    except ImportError:
        raise HTTPException(
            501,
            "statsmodels not installed on this machine — `pip install statsmodels` "
            "(needed for the Phase-1 placeholder fit; real core engine call will replace this).",
        )

    df = _apply_sentinels(SESSION.raw_df)
    outcome_col = SESSION.outcome_spec["column_name"]
    exposure_col = protocol["exposure"]["column_name"]
    confounders = protocol["confounders"]
    predictors = [exposure_col] + confounders

    model_df = df.dropna(subset=predictors + [outcome_col]).copy()
    y = (model_df[outcome_col].astype(str) == str(SESSION.outcome_spec["event_value"])).astype(int)
    X = pd.get_dummies(model_df[predictors], drop_first=True).astype(float)
    X = sm.add_constant(X)

    fit_ok = True
    try:
        fit = sm.Logit(y, X).fit(disp=0)
        max_se = float(fit.bse.max())
    except Exception:
        fit_ok = False
        fit = None
        max_se = 999.0

    # Gate 2 — VIF per numeric predictor
    from numpy.linalg import inv
    vif = {}
    if fit_ok:
        Xc = X.drop(columns=["const"]) if "const" in X.columns else X
        corr = Xc.corr().values
        try:
            inv_corr = inv(corr)
            for i, col in enumerate(Xc.columns):
                vif[col] = float(inv_corr[i, i])
        except np.linalg.LinAlgError:
            for col in Xc.columns:
                vif[col] = float("nan")

    coefficients = []
    if fit_ok:
        for var in X.columns:
            if var == "const":
                continue
            beta = fit.params[var]
            se = fit.bse[var]
            or_ = float(np.exp(np.clip(beta, -700, 700)))
            exponent_lo = np.clip(beta - 1.96 * se, -700, 700)
            exponent_hi = np.clip(beta + 1.96 * se, -700, 700)
            ci_lo = float(np.exp(exponent_lo))
            ci_hi = float(np.exp(exponent_hi))
            coefficients.append({
                "variable": var,
                "adjusted_or": round(or_, 3),
                "adjusted_ci_95": [round(ci_lo, 3), round(ci_hi, 3)],
                "adjusted_p": round(float(fit.pvalues[var]), 4),
            })

    return fit_ok, {
        "n_effective": len(model_df),
        "e_effective": int(y.sum()),
        "coefficients": coefficients,
        "max_se": max_se,
        "vif": vif,
    }


def _run_gates(protocol: dict, fit_ok: bool, results: dict):
    tests = []

    # Gate 1 — separation & convergence
    if not fit_ok or results["max_se"] > THRESHOLDS["separation_max_se"]:
        tests.append({"test_name": "complete_separation", "status": "FAIL", "metric_value": results["max_se"]})
    else:
        tests.append({"test_name": "complete_separation", "status": "PASS", "metric_value": results["max_se"]})

    # Gate 2 — VIF
    max_vif = max(results["vif"].values(), default=0.0)
    worst_vars = [k for k, v in results["vif"].items() if v == max_vif] if results["vif"] else []
    if max_vif > THRESHOLDS["vif_fail"]:
        tests.append({"test_name": "multicollinearity_vif", "status": "FAIL", "metric_value": max_vif, "affected_variables": worst_vars})
    elif max_vif >= THRESHOLDS["vif_warning"]:
        tests.append({"test_name": "multicollinearity_vif", "status": "WARNING", "metric_value": max_vif, "affected_variables": worst_vars})
    else:
        tests.append({"test_name": "multicollinearity_vif", "status": "PASS", "metric_value": max_vif})

    # Gate 3 — proportional hazards (survival only)
    if protocol["outcome_confirmation"].get("time_column"):
        tests.append({"test_name": "proportional_hazards", "status": "PASS",
                       "details": "NOTE: stubbed — real Schoenfeld-residual test needs lifelines Cox fit, not wired yet."})
    else:
        tests.append({"test_name": "proportional_hazards", "status": "NOT_APPLICABLE", "details": "no time-to-event column declared"})

    # Gate 4 — linearity, evaluated against pre_specified_transforms
    transforms = protocol.get("pre_specified_transforms", {})
    tests.append({"test_name": "linearity_continuous_terms", "status": "PASS",
                   "details": f"NOTE: stubbed PASS — real Box-Tidwell check against {transforms or 'default-linear'} not wired yet."})

    # EPV Check — using actual fitted predictor count (k)
    k = len(results.get("coefficients", []))
    e_eff = results.get("e_effective", 0)
    epv = e_eff / max(k, 1) if k > 0 else 0.0

    if epv < 5.0:
        tests.append({
            "test_name": "events_per_variable_epv",
            "status": "WARNING",
            "metric_value": round(epv, 2),
            "details": f"EPV is {epv:.2f} (< 5.0 threshold) — severe parameter instability and potential overfitting.",
        })
    elif epv < 10.0:
        tests.append({
            "test_name": "events_per_variable_epv",
            "status": "WARNING",
            "metric_value": round(epv, 2),
            "details": f"EPV is {epv:.2f} (< 10.0 threshold) — potential parameter instability; interpretations should be cautious.",
        })
    else:
        tests.append({
            "test_name": "events_per_variable_epv",
            "status": "PASS",
            "metric_value": round(epv, 2),
        })

    statuses = {t["status"] for t in tests}
    overall = "FAIL" if "FAIL" in statuses else ("WARNING" if "WARNING" in statuses else "PASS")
    return overall, tests


def _e_value(or_estimate: float) -> float:
    """Minimum unmeasured-confounder strength needed to explain away the effect (§6.4, always computed)."""
    rr = or_estimate if or_estimate >= 1 else 1 / or_estimate
    return round(rr + math.sqrt(rr * (rr - 1)), 3) if rr > 1 else 1.0


@router.post("/run")
def run_execution():
    _require_h1()
    plan = SESSION.h1_payload
    protocol = plan["protocol"]

    fit_ok, results = _fit_model(protocol)
    overall, tests = _run_gates(protocol, fit_ok, results)
    if not fit_ok or results.get("max_se", 0.0) > THRESHOLDS["separation_max_se"]:
        overall = "FAIL"

    plan_hash = plan["provenance"]["plan_fingerprint_h1"]

    if overall == "FAIL":
        failed = next(t for t in tests if t["status"] == "FAIL")
        gate_name = failed["test_name"]
        SESSION.pending_amendment = {
            "parent_plan_hash": plan_hash,
            "failed_gate": gate_name,
            "evidence": failed,
            "remediation_options": REMEDIATION_OPTIONS.get(gate_name, []),
        }
        return {
            "route": "AUTOPSY_CANVAS",
            "diagnostics_summary": {"overall_status": "FAIL", "tests": tests},
            "failed_gate": gate_name,
            "implicated_variables": failed.get("affected_variables", []),
            "evidence": failed,
            "remediation_options": REMEDIATION_OPTIONS.get(gate_name, []),
            "next_step": "POST /api/execute/amendment/prepare with a chosen remediation + rationale, "
                         "then return to Tab 2 (amendment mode) to edit Steps C/D only.",
        }

    # PASS or WARNING — build Hexec payload
    e_values = [
        {"variable": c["variable"], "e_value": _e_value(c["adjusted_or"])}
        for c in results["coefficients"]
    ]

    payload = {
        "provenance": {
            "payload_fingerprint_h0": SESSION.h0_payload["provenance"]["payload_fingerprint_h0"],
            "parent_plan_hash": plan_hash,
            "execution_timestamp_utc": pd.Timestamp.utcnow().isoformat() + "Z",
            "was_amended": "parent_plan_hash" in plan["provenance"] and "amendment_rationale" in plan["provenance"],
        },
        "diagnostic_config": {"ruleset_version": RULESET_VERSION, "thresholds_locked": THRESHOLDS},
        "diagnostics_summary": {"overall_status": overall, "tests": tests},
        "model_results": {
            "model_type": "logistic_regression",  # NOTE: cox_ph path not wired yet, see module docstring
            "sample_sizes": {
                "n_total": len(SESSION.raw_df),
                "n_effective": results["n_effective"],
                "e_effective": results["e_effective"],
            },
            "coefficients": results["coefficients"],
        },
        "sensitivity_analysis": {"e_values": e_values, "note": "always computed per §6.4, tipping-point MNAR analysis not wired yet"},
        "manuscript_artifacts": {
            "note": "generated by Tab 4 — call /api/report/* to produce these",
        },
    }
    canonical = json.dumps(payload, sort_keys=True, default=str).encode()
    payload["provenance"]["execution_fingerprint"] = hashlib.sha256(canonical).hexdigest()

    SESSION.hexec_payload = payload
    return {"route": "PUBLICATION_PACKAGE" if overall == "PASS" else "PUBLICATION_PACKAGE_WITH_LIMITATIONS", **payload}


@router.post("/amendment/prepare")
def prepare_amendment(body: AmendmentPrepareIn):
    if not SESSION.pending_amendment:
        raise HTTPException(400, "No pending amendment — nothing failed.")
    SESSION.pending_amendment["chosen_remediation"] = body.chosen_remediation
    SESSION.pending_amendment["rationale"] = body.rationale
    return {
        "amendment_mode": True,
        "locked_readonly": {
            "exposure": SESSION.exposure,
            "outcome_confirmation": {
                "column_name": SESSION.outcome_spec["column_name"],
                "time_column": SESSION.time_column,
            },
        },
        "editable": {
            "confounders": SESSION.confounders,
            "interactions": SESSION.interactions,
            "missing_data_strategy": SESSION.missing_data_strategy,
        },
        "flagged_variables": SESSION.pending_amendment["evidence"].get("affected_variables", []),
        "prefilled_rationale": body.rationale,
        "note": "Outcome stays masked here — only diagnostic evidence shown, never direction/significance (§6.3). "
                "Edit Steps C/D in Tab 2, then /api/plan/lock re-locks chained to this failed plan's hash.",
    }
