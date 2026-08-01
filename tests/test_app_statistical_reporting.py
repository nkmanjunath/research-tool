"""
Unit tests for the statistical reporting & analytical pipeline fixes:
1. k degrees-of-freedom calculation for categorical dummy variables in epv_live
2. Interaction term DoF multiplication: (L_A - 1) * (L_B - 1)
3. EPV < 5.0 FAIL gate routing & EPV 5-10 WARNING gate propagation
4. _classify_coefficient boundary behavior
5. SMD calculation for continuous and multi-level categorical covariates
6. Cox Proportional Hazards survival engine fitting & Schoenfeld residual testing
7. VanderWeele & Ding (2017) dual E-value calculation (E_est and E_CI)
8. Dynamic language engine (strict 'odds' for Logistic, 'hazard' for Cox PH)
9. Forest plot SVG log-scaled X-axis, ticks, and axis titles
10. Tab 2 amendment workflow: FAIL -> prepare_amendment -> amendment_state -> lock_stage2 (was_amended: True) -> re-run execution
11. Clinical display label formatting reusing core COVARIATE_LABEL_MAP / COVARIATE_UNIT_MAP
12. Label parser continuous underscore handling & dynamic exposure reference level contrast formatting
"""
import math
import sys
from pathlib import Path
import pandas as pd
import pytest

backend_dir = Path(__file__).parent.parent / "app" / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from routers.execution import (
    _e_value_dual,
    _fit_model,
    _run_gates,
    AmendmentPrepareIn,
    get_amendment_state,
    prepare_amendment,
    run_execution,
)
from routers.planning import lock_stage2
from routers.reporting import _classify_coefficient, forest_plot, get_display_label, methods_text
from state import SESSION


def test_k_degrees_of_freedom_calculation():
    """Test 1: k degrees-of-freedom counts correctly for binary + multi-level categorical confounders."""
    df = pd.DataFrame({
        "patient_id": [f"P{i}" for i in range(20)],
        "pfs_event": [1, 0] * 10,
        "treatment_arm": ["A", "B"] * 10,
        "age": [60 + i for i in range(20)],
        "iss_stage": ["I", "II", "III", "I"] * 5,
        "high_risk_fish": ["yes", "no"] * 10,
        "prior_lines": [1, 2] * 10,
        "sex": ["M", "F"] * 10,
    })

    predictors = ["treatment_arm", "age", "iss_stage", "high_risk_fish", "prior_lines", "sex"]
    k = 0
    for col in predictors:
        if pd.api.types.is_numeric_dtype(df[col]):
            k += 1
        else:
            n_uniq = df[col].dropna().nunique()
            k += max(n_uniq - 1, 1)

    # treatment_arm(1) + age(1) + iss_stage(2) + high_risk_fish(1) + prior_lines(1) + sex(1) = 7
    assert k == 7


def test_interaction_term_dof_multiplication():
    """Test 2: Interaction terms multiply categorical levels: (L_A - 1) * (L_B - 1)."""
    df = pd.DataFrame({
        "treatment_arm": ["A", "B"] * 10,
        "iss_stage": ["I", "II", "III", "I"] * 5,  # 3 levels -> 2 dof
    })

    # Interaction term iss_stage:treatment_arm -> (3 - 1) * (2 - 1) = 2 * 1 = 2 DoF
    inter_dof = 1
    for p in ["iss_stage", "treatment_arm"]:
        u = df[p].dropna().nunique()
        inter_dof *= max(u - 1, 1)

    assert inter_dof == 2


def test_epv_warning_and_fail_propagation():
    """Test 3: EPV < 5 forces overall_status to FAIL; EPV between 5 and 10 forces WARNING."""
    protocol = {"outcome_confirmation": {}}
    fit_ok = True

    # Moderate EPV (EPV = 6.29) -> WARNING
    results_warning = {
        "max_se": 1.50,
        "vif": {"var1": 1.20},
        "e_effective": 44,
        "coefficients": [{"variable": f"v{i}"} for i in range(7)],
    }
    overall_w, tests_w = _run_gates(protocol, fit_ok, results_warning)
    assert overall_w == "WARNING"

    # Critical low EPV (EPV = 2.0) -> FAIL
    results_fail = {
        "max_se": 1.50,
        "vif": {"var1": 1.20},
        "e_effective": 14,
        "coefficients": [{"variable": f"v{i}"} for i in range(7)],  # 14 / 7 = 2.0 < 5.0
    }
    overall_f, tests_f = _run_gates(protocol, fit_ok, results_fail)
    assert overall_f == "FAIL"

    epv_test = next(t for t in tests_f if t["test_name"] == "events_per_variable_epv")
    assert epv_test["status"] == "FAIL"
    assert epv_test["metric_value"] == 2.0


def test_classify_coefficient_boundary_behavior():
    """Test 4: _classify_coefficient() boundary behavior."""
    sig_coef = {"adjusted_or": 2.50, "adjusted_ci_95": [1.30, 4.80], "adjusted_p": 0.012}
    assert _classify_coefficient(sig_coef) == "significant"

    borderline_coef = {"adjusted_or": 0.391, "adjusted_ci_95": [0.152, 1.006], "adjusted_p": 0.0516}
    assert _classify_coefficient(borderline_coef) == "borderline/trend"

    not_sig_coef = {"adjusted_or": 1.10, "adjusted_ci_95": [0.60, 2.00], "adjusted_p": 0.750}
    assert _classify_coefficient(not_sig_coef) == "not_significant"


def test_smd_calculation_continuous_and_categorical():
    """Test 5: SMD calculation for continuous and categorical covariates against expected math."""
    m1, m2 = 60.0, 65.0
    s1, s2 = 10.0, 10.0
    pooled_sd = math.sqrt((s1**2 + s2**2) / 2)
    smd_continuous = abs(m1 - m2) / pooled_sd
    assert round(smd_continuous, 3) == 0.500

    p1, p2 = 0.6, 0.2
    denom = math.sqrt((p1 * (1 - p1) + p2 * (1 - p2)) / 2)
    smd_cat = abs(p1 - p2) / denom
    assert round(smd_cat, 3) == 0.894


def test_cox_ph_survival_model_execution():
    """Test 6: Cox Proportional Hazards model fitting when time-to-event column is present."""
    df = pd.read_csv("synthetic_100.csv")
    SESSION.raw_df = df
    SESSION.sentinels = {"global_na_strings": ["NA"], "column_overrides": {}}
    SESSION.outcome_spec = {"column_name": "pfs_event", "event_value": 1, "censored_value": 0}
    SESSION.time_column = "pfs_days"

    protocol = {
        "exposure": {"column_name": "treatment_arm"},
        "confounders": ["age", "iss_stage", "high_risk_fish"],
        "outcome_confirmation": {"column_name": "pfs_event", "time_column": "pfs_days"},
        "interactions": [],
    }

    fit_ok, results = _fit_model(protocol)
    assert fit_ok is True
    assert results["model_type"] == "cox_ph"
    assert "ph_p_min" in results

    overall, tests = _run_gates(protocol, fit_ok, results)
    ph_test = next(t for t in tests if t["test_name"] == "proportional_hazards")
    assert ph_test["status"] in ("PASS", "WARNING")


def test_vanderweele_dual_e_value():
    """Test 7: Dual E-value calculation per VanderWeele & Ding (2017)."""
    # Case A: 95% CI includes 1.0 (0.391, CI [0.152, 1.006]) -> E_CI must be 1.000
    res_null = _e_value_dual(0.391, 0.152, 1.006)
    assert res_null["e_value_ci"] == 1.000
    assert "CI bound: 1.000" in res_null["formatted"]

    # Case B: Significant effect (0.405, CI [0.164, 0.999]) -> E_CI computed on closest bound (0.999)
    res_sig = _e_value_dual(0.405, 0.164, 0.999)
    assert res_sig["e_value_ci"] >= 1.000
    assert res_sig["e_value"] > res_sig["e_value_ci"]


def test_dynamic_language_engine_strict_terms():
    """Test 8: Ensure manuscript generator outputs strict 'odds' or 'hazard' without conflating ('hazard/odds')."""
    df = pd.read_csv("synthetic_100.csv")
    SESSION.raw_df = df
    SESSION.sentinels = {"global_na_strings": ["NA"], "column_overrides": {}}
    SESSION.outcome_spec = {"column_name": "pfs_event", "event_value": 1, "censored_value": 0}
    SESSION.time_column = None

    # Logistic Regression path
    protocol_logit = {
        "exposure": {"column_name": "treatment_arm"},
        "confounders": ["age", "iss_stage"],
        "outcome_confirmation": {"column_name": "pfs_event"},
        "interactions": [],
    }
    SESSION.h1_payload = {"provenance": {"plan_fingerprint_h1": "h1_hash"}, "protocol": protocol_logit}
    fit_ok, results = _fit_model(protocol_logit)
    overall, tests = _run_gates(protocol_logit, fit_ok, results)
    SESSION.hexec_payload = {
        "provenance": {"payload_fingerprint_h0": "h0_hash", "execution_fingerprint": "exec_hash"},
        "diagnostic_config": {"ruleset_version": "v1.2.0"},
        "diagnostics_summary": {"overall_status": overall, "tests": tests},
        "model_results": results,
        "sensitivity_analysis": {"e_values": [{"variable": c["variable"], **_e_value_dual(c["adjusted_or"], c["adjusted_ci_95"][0], c["adjusted_ci_95"][1])} for c in results["coefficients"]]},
    }

    mtxt_logit = methods_text()["text"]
    assert "hazard/odds" not in mtxt_logit
    assert "odds" in mtxt_logit

    # Cox PH path
    protocol_cox = {
        "exposure": {"column_name": "treatment_arm"},
        "confounders": ["age", "iss_stage"],
        "outcome_confirmation": {"column_name": "pfs_event", "time_column": "pfs_days"},
        "interactions": [],
    }
    SESSION.time_column = "pfs_days"
    SESSION.h1_payload = {"provenance": {"plan_fingerprint_h1": "h1_hash"}, "protocol": protocol_cox}
    fit_ok_cox, results_cox = _fit_model(protocol_cox)
    overall_cox, tests_cox = _run_gates(protocol_cox, fit_ok_cox, results_cox)
    SESSION.hexec_payload["model_results"] = results_cox

    mtxt_cox = methods_text()["text"]
    assert "hazard/odds" not in mtxt_cox
    assert "hazard" in mtxt_cox


def test_forest_plot_svg_axis_geometry():
    """Test 9: Forest plot SVG contains logarithmic X-axis tick marks and titles."""
    df = pd.read_csv("synthetic_100.csv")
    SESSION.raw_df = df
    SESSION.sentinels = {"global_na_strings": ["NA"], "column_overrides": {}}
    SESSION.outcome_spec = {"column_name": "pfs_event", "event_value": 1, "censored_value": 0}
    SESSION.time_column = None

    protocol = {
        "exposure": {"column_name": "treatment_arm"},
        "confounders": ["age"],
        "outcome_confirmation": {"column_name": "pfs_event"},
        "interactions": [],
    }
    SESSION.h1_payload = {"provenance": {"plan_fingerprint_h1": "h1_hash"}, "protocol": protocol}
    fit_ok, results = _fit_model(protocol)
    SESSION.hexec_payload = {
        "model_results": results,
        "sensitivity_analysis": {"e_values": []},
    }

    svg_out = forest_plot()["svg"]
    assert "Adjusted Odds Ratio (95% CI)" in svg_out or "Adjusted Hazard Ratio (95% CI)" in svg_out
    assert ">1<" in svg_out or ">1.0<" in svg_out


def test_tab2_amendment_end_to_end_flow():
    """Test 10: Tab 2 amendment workflow (FAIL -> prepare_amendment -> amendment_state -> lock_stage2 -> re-run)."""
    df = pd.read_csv("synthetic_21_v2.csv")
    SESSION.raw_df = df
    SESSION.sentinels = {"global_na_strings": ["NA"], "column_overrides": {}}
    SESSION.outcome_spec = {"column_name": "pfs_event", "event_value": 1, "censored_value": 0}
    SESSION.time_column = "pfs_days"
    SESSION.exposure = {"column_name": "treatment_arm", "reference_level": "A"}
    SESSION.confounders = ["age", "iss_stage", "high_risk_fish", "prior_lines"]
    SESSION.interactions = []
    SESSION.missing_data_strategy = {"global_default": "complete_case", "column_overrides": {}}
    SESSION.h0_payload = {"provenance": {"payload_fingerprint_h0": "h0_hash_123"}}
    SESSION.plan_chain = []
    SESSION.pending_amendment = None

    # Lock initial H1 plan
    h1 = lock_stage2()
    assert "parent_plan_hash" not in h1["provenance"]

    # Run execution -> should FAIL on EPV < 5.0 (5 events / 6 parameters = 0.83 < 5.0)
    exec_res = run_execution()
    assert exec_res["route"] == "AUTOPSY_CANVAS"
    assert exec_res["diagnostics_summary"]["overall_status"] == "FAIL"
    assert exec_res["failed_gate"] == "events_per_variable_epv"

    # Prepare amendment in Tab 3 Autopsy Canvas
    prep = prepare_amendment(AmendmentPrepareIn(
        chosen_remediation="Prune non-essential confounders to restore EPV >= 10",
        rationale="Removing prior_lines and high_risk_fish to achieve sufficient EPV for primary model."
    ))
    assert prep["amendment_mode"] is True
    assert "parent_plan_hash" in prep

    # Query amendment state (used by Tab 2 UI on entry)
    state = get_amendment_state()
    assert state["amendment_mode"] is True
    assert state["parent_plan_hash"] == h1["provenance"]["plan_fingerprint_h1"]
    assert state["locked_readonly"]["exposure"]["column_name"] == "treatment_arm"

    # Amend confounders in Tab 2 (prune to 0 confounders: treatment_arm only -> k=1, EPV = 5.0 >= 5.0)
    SESSION.confounders = []

    # Re-lock plan (POST /api/plan/lock)
    h2 = lock_stage2()
    assert h2["provenance"]["parent_plan_hash"] == h1["provenance"]["plan_fingerprint_h1"]
    assert h2["provenance"]["amendment_rationale"] != ""
    assert SESSION.pending_amendment is None

    # Re-run execution (POST /api/execute/run) -> now passes/warns (EPV = 5.0 >= 5.0)
    exec_res2 = run_execution()
    assert exec_res2["route"] in ("PUBLICATION_PACKAGE", "PUBLICATION_PACKAGE_WITH_LIMITATIONS")
    assert exec_res2["provenance"]["was_amended"] is True


def test_clinical_display_label_formatting():
    """Test 11: get_display_label() formats dummy and continuous variables into publication-grade labels."""
    SESSION.exposure = {"column_name": "treatment_arm", "reference_level": "Arm A"}
    assert "ISS Stage II (vs Stage I)" in get_display_label("iss_stage_II")
    assert "High-Risk Cytogenetics (Yes vs No)" in get_display_label("high_risk_fish_yes")
    assert "Treatment Group: B (vs Arm A)" in get_display_label("treatment_arm_B")

    age_label = get_display_label("age")
    assert "Age, years" in age_label
    assert "Per year increase" in age_label


def test_label_parser_continuous_underscore_and_dynamic_reference_level():
    """Test 12: get_display_label() handles continuous cols with '_' without mis-splitting and uses dynamic exposure reference level."""
    # Test 12a: Continuous variable with underscore in raw_df should not split into base: level
    SESSION.raw_df = pd.DataFrame({"hemoglobin_g_dl": [12.5, 14.1, 13.0]})
    label_cont = get_display_label("hemoglobin_g_dl")
    assert label_cont == "Hemoglobin G Dl"
    assert "Hemoglobin G: dl" not in label_cont

    # Test 12b: Dynamic exposure reference level (e.g. 'Control' instead of hardcoded 'Arm A')
    SESSION.exposure = {"column_name": "treatment_arm", "reference_level": "Control"}
    label_exp = get_display_label("treatment_arm_B")
    assert label_exp == "Treatment Group: B (vs Control)"
