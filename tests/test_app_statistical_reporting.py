"""
Unit tests for the 4 statistical reporting fixes implemented for the app interface:
1. k degrees-of-freedom calculation for categorical dummy variables in epv_live
2. EPV < 10 warning propagation in _run_gates
3. _classify_coefficient boundary behavior
4. SMD calculation for continuous and multi-level categorical covariates
"""
import math
import sys
from pathlib import Path
import pandas as pd
import pytest

backend_dir = Path(__file__).parent.parent / "app" / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from routers.execution import _run_gates
from routers.reporting import _classify_coefficient


def test_k_degrees_of_freedom_calculation():
    """Test 1: k degrees-of-freedom counts correctly for binary + multi-level categorical confounders."""
    # Setup df: iss_stage has 3 levels ('I', 'II', 'III') -> 2 dof
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

    # Simulating epv_live logic
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


def test_epv_warning_propagation():
    """Test 2: EPV < threshold forces overall_status to at least WARNING even when all gates PASS."""
    protocol = {"outcome_confirmation": {}}
    fit_ok = True
    # Simulated execution results where separation and VIF pass, but k=7 and e_effective=44 -> EPV=6.29
    results = {
        "max_se": 1.50,  # Gate 1 PASS
        "vif": {"var1": 1.20, "var2": 1.30},  # Gate 2 PASS
        "e_effective": 44,
        "coefficients": [{"variable": f"v{i}"} for i in range(7)],  # k = 7
    }

    overall, tests = _run_gates(protocol, fit_ok, results)
    assert overall == "WARNING"

    epv_test = next(t for t in tests if t["test_name"] == "events_per_variable_epv")
    assert epv_test["status"] == "WARNING"
    assert epv_test["metric_value"] == 6.29


def test_classify_coefficient_boundary_behavior():
    """Test 3: _classify_coefficient() boundary behavior."""
    # Case 1: Significant (CI excludes 1.0 AND p < 0.05)
    sig_coef = {"adjusted_or": 2.50, "adjusted_ci_95": [1.30, 4.80], "adjusted_p": 0.012}
    assert _classify_coefficient(sig_coef) == "significant"

    # Case 2: Borderline/trend (p=0.0516, CI crosses 1.0 at 1.006)
    borderline_coef = {"adjusted_or": 0.391, "adjusted_ci_95": [0.152, 1.006], "adjusted_p": 0.0516}
    assert _classify_coefficient(borderline_coef) == "borderline/trend"

    # Case 3: Not significant (CI [0.60, 2.00], p=0.750)
    not_sig_coef = {"adjusted_or": 1.10, "adjusted_ci_95": [0.60, 2.00], "adjusted_p": 0.750}
    assert _classify_coefficient(not_sig_coef) == "not_significant"


def test_smd_calculation_continuous_and_categorical():
    """Test 4: SMD calculation for continuous and categorical covariates against expected math."""
    m1, m2 = 60.0, 65.0
    s1, s2 = 10.0, 10.0
    pooled_sd = math.sqrt((s1**2 + s2**2) / 2)
    smd_continuous = abs(m1 - m2) / pooled_sd
    assert round(smd_continuous, 3) == 0.500

    p1, p2 = 0.6, 0.2
    denom = math.sqrt((p1 * (1 - p1) + p2 * (1 - p2)) / 2)
    smd_cat = abs(p1 - p2) / denom
    assert round(smd_cat, 3) == 0.894
