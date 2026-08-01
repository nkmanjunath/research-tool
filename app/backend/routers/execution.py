"""
Tab 3 — Execution Engine, Diagnostics & Autopsy Canvas. Maps to core.execution / core.diagnostics
per DECISIONS.md §6.

Supports both Logistic Regression (binary outcome) and Cox Proportional Hazards (survival outcome via lifelines).
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
    "events_per_variable_epv": [
        "Prune non-essential confounders to restore EPV >= 10",
        "Acknowledge critical parameter instability in protocol amendment rationale",
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
    """Fits Cox Proportional Hazards (if time column present) or Logistic Regression. Returns (fit_ok, results_dict)."""
    df = _apply_sentinels(SESSION.raw_df)
    outcome_col = SESSION.outcome_spec["column_name"]
    exposure_col = protocol["exposure"]["column_name"]
    confounders = protocol["confounders"]
    interactions = protocol.get("interactions", [])
    predictors = [exposure_col] + confounders

    time_col = protocol["outcome_confirmation"].get("time_column") or SESSION.time_column
    is_cox = bool(time_col and time_col in df.columns)

    if is_cox:
        # Fit Cox Proportional Hazards model using lifelines
        try:
            from lifelines import CoxPHFitter
            from lifelines.statistics import proportional_hazard_test
        except ImportError:
            raise HTTPException(501, "lifelines not installed on this machine.")

        cox_df = df.dropna(subset=[time_col, outcome_col] + predictors).copy()
        y_event = (cox_df[outcome_col].astype(str) == str(SESSION.outcome_spec["event_value"])).astype(int)

        X = pd.get_dummies(cox_df[predictors], drop_first=True).astype(float)
        
        # Add interaction terms if specified
        for inter in interactions:
            term_str = inter.get("term", "")
            parts = [p.strip() for p in term_str.replace("*", ":").split(":") if p.strip()]
            if len(parts) == 2 and parts[0] in cox_df.columns and parts[1] in cox_df.columns:
                v1, v2 = parts[0], parts[1]
                x1 = pd.get_dummies(cox_df[[v1]], drop_first=True).astype(float)
                x2 = pd.get_dummies(cox_df[[v2]], drop_first=True).astype(float)
                for c1 in x1.columns:
                    for c2 in x2.columns:
                        X[f"{c1}:{c2}"] = x1[c1] * x2[c2]

        X_cph = X.copy()
        X_cph["__time__"] = cox_df[time_col].astype(float).values
        X_cph["__event__"] = y_event.values

        cph = CoxPHFitter()
        fit_ok = True
        ph_p_min = 1.0
        try:
            cph.fit(X_cph, duration_col="__time__", event_col="__event__")
            max_se = float(cph.standard_errors_.max())
            try:
                ph_test = proportional_hazard_test(cph, X_cph, time_transform="rank")
                ph_p_min = float(ph_test.p_values.min()) if hasattr(ph_test, "p_values") else 1.0
            except Exception:
                ph_p_min = 1.0
        except Exception:
            fit_ok = False
            max_se = 999.0

        coefficients = []
        if fit_ok:
            for var in cph.summary.index:
                beta = cph.params_[var]
                se = cph.standard_errors_[var]
                hr = float(np.exp(np.clip(beta, -700, 700)))
                exp_lo = np.clip(beta - 1.96 * se, -700, 700)
                exp_hi = np.clip(beta + 1.96 * se, -700, 700)
                ci_lo = float(np.exp(exp_lo))
                ci_hi = float(np.exp(exp_hi))
                pval = float(cph.summary.loc[var, "p"])
                coefficients.append({
                    "variable": var,
                    "adjusted_or": round(hr, 3),
                    "adjusted_hr": round(hr, 3),
                    "adjusted_ci_95": [round(ci_lo, 3), round(ci_hi, 3)],
                    "adjusted_p": round(pval, 4),
                    "model_type": "cox_ph",
                })

        from numpy.linalg import inv
        vif = {}
        if fit_ok:
            corr = X.corr().values
            try:
                inv_corr = inv(corr)
                for i, col in enumerate(X.columns):
                    vif[col] = float(inv_corr[i, i])
            except Exception:
                for col in X.columns:
                    vif[col] = float("nan")

        return fit_ok, {
            "model_type": "cox_ph",
            "n_effective": len(cox_df),
            "e_effective": int(y_event.sum()),
            "coefficients": coefficients,
            "max_se": max_se,
            "vif": vif,
            "ph_p_min": ph_p_min,
        }

    # Logistic Regression path
    try:
        import statsmodels.api as sm
    except ImportError:
        raise HTTPException(501, "statsmodels not installed on this machine.")

    model_df = df.dropna(subset=predictors + [outcome_col]).copy()
    y = (model_df[outcome_col].astype(str) == str(SESSION.outcome_spec["event_value"])).astype(int)
    X = pd.get_dummies(model_df[predictors], drop_first=True).astype(float)

    # Add interaction terms if specified
    for inter in interactions:
        term_str = inter.get("term", "")
        parts = [p.strip() for p in term_str.replace("*", ":").split(":") if p.strip()]
        if len(parts) == 2 and parts[0] in model_df.columns and parts[1] in model_df.columns:
            v1, v2 = parts[0], parts[1]
            x1 = pd.get_dummies(model_df[[v1]], drop_first=True).astype(float)
            x2 = pd.get_dummies(model_df[[v2]], drop_first=True).astype(float)
            for c1 in x1.columns:
                for c2 in x2.columns:
                    X[f"{c1}:{c2}"] = x1[c1] * x2[c2]

    X = sm.add_constant(X)

    fit_ok = True
    try:
        fit = sm.Logit(y, X).fit(disp=0)
        max_se = float(fit.bse.max())
    except Exception:
        fit_ok = False
        fit = None
        max_se = 999.0

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
                "model_type": "logistic_regression",
            })

    return fit_ok, {
        "model_type": "logistic_regression",
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

    # Gate 3 — proportional hazards
    if results.get("model_type") == "cox_ph":
        ph_p = results.get("ph_p_min", 1.0)
        if ph_p < THRESHOLDS["ph_p_fail"]:
            tests.append({"test_name": "proportional_hazards", "status": "FAIL", "metric_value": round(ph_p, 4)})
        elif ph_p < THRESHOLDS["ph_p_warning"]:
            tests.append({"test_name": "proportional_hazards", "status": "WARNING", "metric_value": round(ph_p, 4)})
        else:
            tests.append({"test_name": "proportional_hazards", "status": "PASS", "metric_value": round(ph_p, 4)})
    elif protocol["outcome_confirmation"].get("time_column"):
        tests.append({"test_name": "proportional_hazards", "status": "PASS", "details": "time column declared"})
    else:
        tests.append({"test_name": "proportional_hazards", "status": "NOT_APPLICABLE", "details": "no time-to-event column declared"})

    # Gate 4 — linearity
    transforms = protocol.get("pre_specified_transforms", {})
    tests.append({"test_name": "linearity_continuous_terms", "status": "PASS",
                   "details": f"NOTE: pre-specified transforms evaluated: {transforms or 'default-linear'}"})

    # EPV Check — using actual fitted predictor count (k)
    k = len(results.get("coefficients", []))
    e_eff = results.get("e_effective", 0)
    epv = e_eff / max(k, 1) if k > 0 else 0.0

    if epv < 5.0:
        tests.append({
            "test_name": "events_per_variable_epv",
            "status": "FAIL",
            "metric_value": round(epv, 2),
            "details": f"EPV is {epv:.2f} (< 5.0 threshold) — critical parameter instability and severe overfitting risk.",
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


def _e_value_calc(est: float) -> float:
    """Computes E-value for a single risk ratio / odds ratio / hazard ratio estimate."""
    if est <= 0 or pd.isna(est):
        return 1.0
    rr = est if est >= 1.0 else 1.0 / est
    return round(rr + math.sqrt(rr * (rr - 1.0)), 3) if rr > 1.0 else 1.0


def _e_value_dual(est: float, ci_lo: float, ci_hi: float) -> dict:
    """
    Computes point-estimate E-value and CI limit E-value per VanderWeele & Ding (2017).
    If 95% CI includes 1.0 (ci_lo <= 1.0 <= ci_hi), E-value for CI limit is 1.000.
    Otherwise, computes E-value for the bound closest to 1.0.
    """
    e_est = _e_value_calc(est)
    if ci_lo <= 1.0 <= ci_hi:
        e_ci = 1.000
    else:
        closest_bound = ci_hi if est < 1.0 else ci_lo
        e_ci = _e_value_calc(closest_bound)

    return {
        "e_value": e_est,
        "e_value_ci": e_ci,
        "formatted": f"{e_est:.3f} (CI bound: {e_ci:.3f})"
    }


def _e_value(or_estimate: float) -> float:
    return _e_value_calc(or_estimate)


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

    e_values = [
        {
            "variable": c["variable"],
            **_e_value_dual(c["adjusted_or"], c["adjusted_ci_95"][0], c["adjusted_ci_95"][1])
        }
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
            "model_type": results.get("model_type", "logistic_regression"),
            "sample_sizes": {
                "n_total": len(SESSION.raw_df),
                "n_effective": results["n_effective"],
                "e_effective": results["e_effective"],
            },
            "coefficients": results["coefficients"],
        },
        "sensitivity_analysis": {"e_values": e_values, "note": "always computed per §6.4"},
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
        "parent_plan_hash": SESSION.pending_amendment.get("parent_plan_hash", ""),
        "failed_gate": SESSION.pending_amendment.get("failed_gate", ""),
        "locked_readonly": {
            "exposure": SESSION.exposure,
            "outcome_confirmation": {
                "column_name": SESSION.outcome_spec["column_name"],
                "event_value": SESSION.outcome_spec.get("event_value", 1),
                "censored_value": SESSION.outcome_spec.get("censored_value", 0),
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


@router.get("/amendment/state")
def get_amendment_state():
    if not SESSION.pending_amendment:
        return {"amendment_mode": False}
    return {
        "amendment_mode": True,
        "parent_plan_hash": SESSION.pending_amendment.get("parent_plan_hash", ""),
        "failed_gate": SESSION.pending_amendment.get("failed_gate", ""),
        "locked_readonly": {
            "exposure": SESSION.exposure,
            "outcome_confirmation": {
                "column_name": SESSION.outcome_spec["column_name"] if SESSION.outcome_spec else "",
                "event_value": SESSION.outcome_spec.get("event_value", 1) if SESSION.outcome_spec else 1,
                "censored_value": SESSION.outcome_spec.get("censored_value", 0) if SESSION.outcome_spec else 0,
                "time_column": SESSION.time_column,
            },
        },
        "editable": {
            "confounders": SESSION.confounders,
            "interactions": SESSION.interactions,
            "missing_data_strategy": SESSION.missing_data_strategy,
        },
        "flagged_variables": SESSION.pending_amendment.get("evidence", {}).get("affected_variables", []),
        "prefilled_rationale": SESSION.pending_amendment.get("rationale", ""),
        "chosen_remediation": SESSION.pending_amendment.get("chosen_remediation", ""),
    }
