"""Inferential statistics — runs pre-registered tests from the locked plan.

Uses scipy, statsmodels, and lifelines.  Every function returns a standard
result dict so callers can consume uniformly.

URO (Unified Result Object) keys:
    test_name, statistic, p_value, ci_lower, ci_upper, params,
    effect_size: dict {"metric": str, "value": float} | None
    sample_counts: dict {"n_total": int, "n_analyzed": int, "n_excluded": int}
"""

from __future__ import annotations
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as sp_stats
from scipy.stats import chi2_contingency, fisher_exact, ttest_ind, ttest_rel
from scipy.stats import mannwhitneyu, f_oneway, kruskal

# Lazy-import lifelines only when needed (slow import)


def _uro(*, n_analyzed: int, n_total: int | None = None,
         **fields) -> dict:
    """Build a URO dict with defaults for common fields."""
    uro = {
        "test_name": None,
        "statistic": None,
        "p_value": None,
        "ci_lower": None,
        "ci_upper": None,
        "params": {},
        "effect_size": None,
        "sample_counts": {
            "n_total": n_total or n_analyzed,
            "n_analyzed": n_analyzed,
            "n_excluded": (n_total or n_analyzed) - n_analyzed,
        },
    }
    uro.update(fields)
    return uro


def _cohens_d(g1: pd.Series, g2: pd.Series) -> float:
    """Cohen's d for two independent groups (pooled SD)."""
    n1, n2 = len(g1), len(g2)
    s1, s2 = g1.var(ddof=1), g2.var(ddof=1)
    pooled = np.sqrt(((n1 - 1) * s1 + (n2 - 1) * s2) / (n1 + n2 - 2))
    if pooled == 0:
        return 0.0
    return float((g1.mean() - g2.mean()) / pooled)


def _cramers_v(chi2: float, n: int, r: int, c: int) -> float:
    """Cramér's V for chi-square test of association."""
    k = min(r - 1, c - 1)
    if k <= 0 or n == 0:
        return 0.0
    return float(np.sqrt(chi2 / (n * k)))


def run_test(
    test_name: str,
    data: pd.DataFrame,
    outcome_col: str,
    group_col: str,
    time_col: Optional[str] = None,
    event_col: Optional[str] = None,
    covariates: Optional[list[str]] = None,
) -> dict:
    """Dispatch to the appropriate test function.

    Returns a URO (Unified Result Object) dict.
    """
    if test_name in ("chi_square",):
        return _chi_square(data, outcome_col, group_col)
    elif test_name == "fishers_exact":
        return _fishers_exact(data, outcome_col, group_col)
    elif test_name == "t_test":
        return _t_test(data, outcome_col, group_col, paired=False)
    elif test_name == "paired_t_test":
        return _t_test(data, outcome_col, group_col, paired=True)
    elif test_name == "mann_whitney_u":
        return _mann_whitney(data, outcome_col, group_col)
    elif test_name == "wilcoxon_signed_rank":
        return _wilcoxon(data, outcome_col, group_col)
    elif test_name == "anova":
        return _anova(data, outcome_col, group_col)
    elif test_name == "kruskal_wallis":
        return _kruskal_wallis(data, outcome_col, group_col)
    elif test_name == "kaplan_meier_logrank":
        return _kaplan_meier_logrank(data, time_col, event_col, group_col)
    elif test_name == "cox_proportional_hazards":
        return _cox_ph(data, time_col, event_col, group_col, covariates or [])
    else:
        return _uro(test_name=test_name, n_analyzed=len(data),
                     params={"error": f"Unknown test: {test_name}"})


def _chi_square(data: pd.DataFrame, outcome_col: str, group_col: str) -> dict:
    ct = pd.crosstab(data[group_col], data[outcome_col])
    stat, p, dof, expected = chi2_contingency(ct)
    n = len(data)
    return _uro(
        test_name="chi_square", statistic=float(stat), p_value=float(p),
        n_analyzed=n,
        effect_size={
            "metric": "Cramér's V",
            "value": _cramers_v(float(stat), n, ct.shape[0], ct.shape[1]),
        },
        params={"dof": int(dof)},
    )


def _fishers_exact(data: pd.DataFrame, outcome_col: str, group_col: str) -> dict:
    ct = pd.crosstab(data[group_col], data[outcome_col])
    if ct.shape != (2, 2):
        return _uro(
            test_name="fishers_exact", n_analyzed=len(data),
            params={"error": "Fisher's exact requires a 2x2 table"},
        )
    odds_ratio, p = fisher_exact(ct)
    return _uro(
        test_name="fishers_exact", statistic=float(odds_ratio),
        p_value=float(p), n_analyzed=len(data),
        effect_size={"metric": "Odds Ratio", "value": float(odds_ratio)},
    )


def _t_test(data: pd.DataFrame, outcome_col: str, group_col: str,
            paired: bool = False) -> dict:
    groups = [g for _, g in data.groupby(group_col)[outcome_col]]
    if len(groups) != 2:
        return _uro(
            test_name="t_test", n_analyzed=len(data),
            params={"error": "t-test requires exactly 2 groups"},
        )
    g1, g2 = groups
    g1 = pd.to_numeric(g1, errors="coerce").dropna()
    g2 = pd.to_numeric(g2, errors="coerce").dropna()
    if paired:
        n = min(len(g1), len(g2))
        stat, p = ttest_rel(g1.iloc[:n], g2.iloc[:n])
        test = "paired_t_test"
        es = None  # No simple paired Cohen's d without r; skip for now
    else:
        stat, p = ttest_ind(g1, g2, equal_var=False)
        test = "t_test"
        es = {"metric": "Cohen's d", "value": _cohens_d(g1, g2)}
    n_total = len(g1) + len(g2)
    return _uro(
        test_name=test, statistic=float(stat), p_value=float(p),
        n_analyzed=n_total, effect_size=es,
        params={"n1": len(g1), "n2": len(g2)},
    )


def _mann_whitney(data: pd.DataFrame, outcome_col: str, group_col: str) -> dict:
    groups = [g for _, g in data.groupby(group_col)[outcome_col]]
    if len(groups) != 2:
        return _uro(
            test_name="mann_whitney_u", n_analyzed=len(data),
            params={"error": "Mann-Whitney requires exactly 2 groups"},
        )
    g1 = pd.to_numeric(groups[0], errors="coerce").dropna()
    g2 = pd.to_numeric(groups[1], errors="coerce").dropna()
    stat, p = mannwhitneyu(g1, g2)
    return _uro(
        test_name="mann_whitney_u", statistic=float(stat), p_value=float(p),
        n_analyzed=len(g1) + len(g2),
        params={"n1": len(g1), "n2": len(g2)},
    )


def _wilcoxon(data: pd.DataFrame, outcome_col: str, group_col: str) -> dict:
    groups = [g for _, g in data.groupby(group_col)[outcome_col]]
    if len(groups) != 2:
        return _uro(
            test_name="wilcoxon_signed_rank", n_analyzed=len(data),
            params={"error": "Wilcoxon requires exactly 2 groups"},
        )
    g1 = pd.to_numeric(groups[0], errors="coerce").dropna()
    g2 = pd.to_numeric(groups[1], errors="coerce").dropna()
    n = min(len(g1), len(g2))
    stat, p = sp_stats.wilcoxon(g1.iloc[:n], g2.iloc[:n])
    return _uro(
        test_name="wilcoxon_signed_rank", statistic=float(stat),
        p_value=float(p), n_analyzed=n,
        params={"n": n},
    )


def _anova(data: pd.DataFrame, outcome_col: str, group_col: str) -> dict:
    groups = [pd.to_numeric(g, errors="coerce").dropna()
              for _, g in data.groupby(group_col)[outcome_col]]
    stat, p = f_oneway(*groups)
    n = sum(len(g) for g in groups)
    return _uro(
        test_name="anova", statistic=float(stat), p_value=float(p),
        n_analyzed=n,
        params={"k": len(groups)},
    )


def _kruskal_wallis(data: pd.DataFrame, outcome_col: str, group_col: str) -> dict:
    groups = [pd.to_numeric(g, errors="coerce").dropna()
              for _, g in data.groupby(group_col)[outcome_col]]
    stat, p = kruskal(*groups)
    return _uro(
        test_name="kruskal_wallis", statistic=float(stat), p_value=float(p),
        n_analyzed=sum(len(g) for g in groups),
        params={"k": len(groups)},
    )


def _kaplan_meier_logrank(data: pd.DataFrame, time_col: str, event_col: str,
                          group_col: str) -> dict:
    from lifelines import KaplanMeierFitter
    from lifelines.statistics import logrank_test

    data = data.copy()
    data[time_col] = pd.to_numeric(data[time_col], errors="coerce")
    data[event_col] = pd.to_numeric(data[event_col], errors="coerce")

    groups = data[group_col].unique()
    if len(groups) != 2:
        return _uro(
            test_name="kaplan_meier_logrank", n_analyzed=len(data),
            params={"error": "Log-rank requires exactly 2 groups"},
        )

    g0 = data[data[group_col] == groups[0]].dropna(subset=[time_col, event_col])
    g1 = data[data[group_col] == groups[1]].dropna(subset=[time_col, event_col])

    result = logrank_test(
        g0[time_col], g1[time_col],
        event_observed_A=g0[event_col], event_observed_B=g1[event_col],
    )
    n = len(g0) + len(g1)
    return _uro(
        test_name="kaplan_meier_logrank",
        statistic=float(result.test_statistic),
        p_value=float(result.p_value), n_analyzed=n,
        params={"n0": len(g0), "n1": len(g1),
                "groups": [str(groups[0]), str(groups[1])]},
    )


def _cox_ph(data: pd.DataFrame, time_col: str, event_col: str,
            group_col: str, covariates: list[str]) -> dict:
    from lifelines import CoxPHFitter

    df = data[[time_col, event_col, group_col] + covariates].copy()
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna()

    if df.empty:
        return _uro(
            test_name="cox_proportional_hazards", n_analyzed=0,
            params={"error": "No valid data after dropping NAs"},
        )

    cph = CoxPHFitter()
    cph.fit(df, duration_col=time_col, event_col=event_col, step_size=0.1)
    hr = cph.hazard_ratios_.get(group_col, None)
    ci = cph.confidence_intervals_.loc[group_col] if group_col in cph.confidence_intervals_.index else None
    p = cph.summary.loc[group_col, "p"] if group_col in cph.summary.index else None

    # Post-fit proportional-hazards diagnostic (Schoenfeld residuals)
    diagnostics = None
    try:
        from lifelines.statistics import proportional_hazard_test
        results = proportional_hazard_test(cph, df, time_transform="km")
        # Build per-covariate summary
        diag_rows = []
        for cov_name in results.summary.index:
            row = results.summary.loc[cov_name]
            diag_rows.append({
                "covariate": str(cov_name),
                "test_statistic": float(row.get("test_statistic", 0)),
                "p_value": float(row.get("p", 1.0)),
            })
        diagnostics = {"test": "Schoenfeld residuals", "covariates": diag_rows}
    except Exception:
        diagnostics = {"test": "Schoenfeld residuals", "error": "Could not compute"}

    return _uro(
        test_name="cox_proportional_hazards",
        statistic=float(hr) if hr is not None else None,
        p_value=float(p) if p is not None else None,
        ci_lower=float(ci[ci.index[0]]) if ci is not None else None,
        ci_upper=float(ci[ci.index[1]]) if ci is not None else None,
        n_analyzed=len(df),
        effect_size={
            "metric": "Hazard Ratio",
            "value": float(hr),
        } if hr is not None else None,
        params={"covariates": covariates, "assumption_diagnostics": diagnostics},
    )
