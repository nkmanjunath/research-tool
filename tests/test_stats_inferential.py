"""Tests for inferential statistics."""

import pandas as pd
import pytest

from core.stats.inferential import run_test


def _make_df():
    """Simple two-group data."""
    return pd.DataFrame({
        "group": ["A"] * 20 + ["B"] * 20,
        "outcome": [1] * 12 + [0] * 8 + [1] * 6 + [0] * 14,
        "continuous": [65 + i for i in range(20)] + [70 + i for i in range(20)],
        "time": [100 + i * 10 for i in range(40)],
        "event": [1 if i < 10 else 0 for i in range(20)] + [1 if i < 15 else 0 for i in range(20)],
    })


def test_chi_square():
    df = _make_df()
    result = run_test("chi_square", df, "outcome", "group")
    assert result["test_name"] == "chi_square"
    assert result["statistic"] is not None
    assert result["p_value"] is not None


def test_fishers_exact():
    df = _make_df()
    result = run_test("fishers_exact", df, "outcome", "group")
    assert result["test_name"] == "fishers_exact"
    assert result["statistic"] is not None


def test_t_test():
    df = _make_df()
    result = run_test("t_test", df, "continuous", "group")
    assert result["test_name"] == "t_test"
    assert result["statistic"] is not None


def test_mann_whitney():
    df = _make_df()
    result = run_test("mann_whitney_u", df, "continuous", "group")
    assert result["test_name"] == "mann_whitney_u"
    assert result["statistic"] is not None


def test_anova():
    df = pd.DataFrame({
        "group": ["A"] * 10 + ["B"] * 10 + ["C"] * 10,
        "val": [60 + i for i in range(10)] + [70 + i for i in range(10)] + [80 + i for i in range(10)],
    })
    result = run_test("anova", df, "val", "group")
    assert result["test_name"] == "anova"
    assert result["statistic"] is not None


def test_kruskal_wallis():
    df = pd.DataFrame({
        "group": ["A"] * 10 + ["B"] * 10 + ["C"] * 10,
        "val": [60 + i for i in range(10)] + [70 + i for i in range(10)] + [80 + i for i in range(10)],
    })
    result = run_test("kruskal_wallis", df, "val", "group")
    assert result["test_name"] == "kruskal_wallis"
    assert result["statistic"] is not None


def test_kaplan_meier_logrank():
    df = _make_df()
    result = run_test("kaplan_meier_logrank", df, "outcome",
                      "group", time_col="time", event_col="event")
    assert result["test_name"] == "kaplan_meier_logrank"
    assert result["statistic"] is not None
    assert result["p_value"] is not None


def test_cox_ph():
    df = _make_df()
    result = run_test("cox_proportional_hazards", df, "outcome",
                      "group", time_col="time", event_col="event",
                      covariates=["continuous"])
    assert result["test_name"] == "cox_proportional_hazards"
    # May or may not converge on tiny data, but should not crash
    assert result is not None


def test_unknown_test():
    df = _make_df()
    result = run_test("imaginary_test", df, "outcome", "group")
    assert "error" in str(result.get("params", {}))


def test_kaplan_meier_rejects_missing_event_col():
    """Survival test on a duration-only column must raise a clear ValueError."""
    df = pd.DataFrame({
        "pfs_days": [100, 200, 150],
        "treatment_arm": ["A", "B", "A"],
    })
    with pytest.raises(ValueError, match="no linked event/censoring column"):
        run_test("kaplan_meier_logrank", df, outcome_col="pfs_days",
                 group_col="treatment_arm", time_col="pfs_days",
                 event_col="pfs_event")


def test_kaplan_meier_rejects_none_event_col():
    """Survival test with event_col=None must raise a clear ValueError."""
    df = pd.DataFrame({
        "pfs_days": [100, 200, 150],
        "treatment_arm": ["A", "B", "A"],
    })
    with pytest.raises(ValueError, match="no linked event/censoring column"):
        run_test("kaplan_meier_logrank", df, outcome_col="pfs_days",
                 group_col="treatment_arm", time_col="pfs_days",
                 event_col=None)


def test_cox_ph_rejects_missing_event_col():
    """Cox PH on a duration-only column must raise a clear ValueError."""
    df = pd.DataFrame({
        "pfs_days": [100, 200, 150],
        "treatment_arm": ["A", "B", "A"],
    })
    with pytest.raises(ValueError, match="no linked event/censoring column"):
        run_test("cox_proportional_hazards", df, outcome_col="pfs_days",
                 group_col="treatment_arm", time_col="pfs_days",
                 event_col="pfs_event", covariates=[])
