"""Inferential statistics — runs pre-registered tests from the locked plan.

Uses scipy, statsmodels, and lifelines.  Every function returns a standard
result dict so callers can consume uniformly.

URO (Unified Result Object) keys:
    test_name, statistic, p_value, ci_lower, ci_upper, params,
    effect_size: dict {"metric": str, "value": float} | None
    sample_counts: dict {"n_total": int, "n_analyzed": int, "n_excluded": int}
"""

from __future__ import annotations
import logging
import re
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as sp_stats
from scipy.stats import chi2_contingency, fisher_exact, ttest_ind, ttest_rel
from lifelines.exceptions import ConvergenceError
from scipy.stats import mannwhitneyu, f_oneway, kruskal

logger = logging.getLogger(__name__)

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
    **kwargs,
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
    elif test_name == "cox_ph_model":
        var_types = kwargs.get("var_types", {})
        interaction_terms = kwargs.get("interaction_terms", [])
        try:
            return _cox_ph_model(data, time_col, event_col, group_col, covariates or [],
                                 var_types=var_types, interaction_terms=interaction_terms)
        except ConvergenceError as e:
            return _uro(
                test_name=test_name, n_analyzed=len(data),
                params={"error": f"ConvergenceError: {e}"},
                status="error",
            )
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
        es = None
        diff = g1.iloc[:n] - g2.iloc[:n]
        mean_diff = diff.mean()
        se = diff.std(ddof=1) / (n ** 0.5)
        df = n - 1
    else:
        stat, p = ttest_ind(g1, g2, equal_var=False)
        test = "t_test"
        es = {"metric": "Cohen's d", "value": _cohens_d(g1, g2)}
        mean_diff = g1.mean() - g2.mean()
        v1, v2 = g1.var(ddof=1), g2.var(ddof=1)
        n1, n2 = len(g1), len(g2)
        se = ((v1 / n1) + (v2 / n2)) ** 0.5
        df_num = ((v1 / n1) + (v2 / n2)) ** 2
        df_den = ((v1 / n1) ** 2 / (n1 - 1)) + ((v2 / n2) ** 2 / (n2 - 1))
        df = df_num / df_den if df_den > 0 else 1.0
    from scipy.stats import t as t_dist
    t_crit = t_dist.ppf(0.975, df)
    ci_lower = mean_diff - t_crit * se
    ci_upper = mean_diff + t_crit * se
    n_total = len(g1) + len(g2)
    return _uro(
        test_name=test, statistic=float(stat), p_value=float(p),
        n_analyzed=n_total, effect_size=es,
        ci_lower=float(ci_lower), ci_upper=float(ci_upper),
        params={"n1": len(g1), "n2": len(g2), "df": float(df)},
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

    if not event_col or event_col not in data.columns:
        raise ValueError(
            f"Cannot run kaplan_meier_logrank on '{time_col}': "
            f"no linked event/censoring column found. "
            f"A time-to-event variable requires both a duration column "
            f"and an event indicator column."
        )

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
    """Legacy univariate Cox PH (for backward compatibility with test suite)."""
    from lifelines import CoxPHFitter

    if not event_col or event_col not in data.columns:
        raise ValueError(
            f"Cannot run cox_proportional_hazards on '{time_col}': "
            f"no linked event/censoring column found. "
            f"A time-to-event variable requires both a duration column "
            f"and an event indicator column."
        )

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
    cph.fit(df, duration_col=time_col, event_col=event_col)
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
        ci_lower=np.exp(float(ci.iloc[0])) if ci is not None else None,
        ci_upper=np.exp(float(ci.iloc[1])) if ci is not None else None,
        n_analyzed=len(df),
        effect_size={
            "metric": "Hazard Ratio",
            "value": float(hr),
        } if hr is not None else None,
        params={"covariates": covariates, "assumption_diagnostics": diagnostics},
    )


def _cox_ph_model(data: pd.DataFrame, time_col: str, event_col: str,
                  group_col: str, covariates: list[str],
                  var_types: dict[str, str] | None = None,
                  interaction_terms: list[list[str]] | None = None) -> dict:
    """Multivariable Cox PH model returning per-covariate HRs, CIs, and overall model stats.
    interaction_terms: list of [var_a, var_b] pairs for a*b expansion.
    """
    from lifelines import CoxPHFitter

    interaction_terms = interaction_terms or []

    if not event_col or event_col not in data.columns:
        raise ValueError(
            f"Cannot run cox_ph_model on '{time_col}': "
            f"no linked event/censoring column found. "
            f"A time-to-event variable requires both a duration column "
            f"and an event indicator column."
        )

    # All columns needed
    all_cols = [time_col, event_col, group_col] + covariates
    for pair in interaction_terms:
        for v in pair:
            if v not in all_cols:
                all_cols.append(v)
    df = data[all_cols].copy()

    var_types = var_types or {}
    numeric_cols = [time_col, event_col]
    for c in covariates:
        dtype = var_types.get(c)
        if dtype == "continuous" or (dtype is None and data[c].dtype != 'object'):
            numeric_cols.append(c)
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna()

    if df.empty:
        return _uro(
            test_name="cox_ph_model", n_analyzed=0,
            params={"error": "No valid data after dropping NAs"},
        )

    # Build formula: group_col + covariates + interaction terms (a*b)
    formula_terms = [group_col] + covariates
    for pair in interaction_terms:
        formula_terms.append(f"{pair[0]} * {pair[1]}")
    formula = " + ".join(formula_terms)

    cph = CoxPHFitter()
    cph.fit(df, duration_col=time_col, event_col=event_col, formula=formula)

    # --- Primary treatment effect ---
    hr = None
    ci = None
    p = None
    for cov in cph.summary.index:
        if cov.startswith(group_col) or cov == group_col:
            hr = cph.hazard_ratios_.get(cov, None)
            if cov in cph.confidence_intervals_.index:
                ci = cph.confidence_intervals_.loc[cov]
            p = cph.summary.loc[cov, "p"]
            break

    # --- Per-covariate results ---
    covariate_results = []
    for cov in [group_col] + covariates:
        matching_idx = [idx for idx in cph.summary.index if idx.startswith(cov) or idx == cov]
        if matching_idx:
            idx = matching_idx[0]
            row = cph.summary.loc[idx]
            cov_hr = cph.hazard_ratios_.get(idx, None)
            cov_ci = cph.confidence_intervals_.loc[idx] if idx in cph.confidence_intervals_.index else None

            ref_level = None
            tested_level = None
            if idx != cov:
                m2 = re.search(r'\[T\.(.+)\]$', idx)
                if m2:
                    tested_level = m2.group(1)
                    uniq = sorted(str(v) for v in data[cov].dropna().unique()) if cov in data.columns else []
                    ref_candidates = [v for v in uniq if v != tested_level]
                    ref_level = ref_candidates[0] if ref_candidates else None

            covariate_results.append({
                "covariate": cov,
                "hr": float(cov_hr) if cov_hr is not None else None,
                "ci_lower": np.exp(float(cov_ci.iloc[0])) if cov_ci is not None else None,
                "ci_upper": np.exp(float(cov_ci.iloc[1])) if cov_ci is not None else None,
                "wald_p": float(row["p"]) if "p" in row else None,
                "coef": float(row["coef"]) if "coef" in row else None,
                "se": float(row["se"]) if "se" in row else None,
                "z": float(row["z"]) if "z" in row else None,
                "reference_level": ref_level,
                "tested_level": tested_level,
            })

    # --- Interaction terms ---
    for pair in interaction_terms:
        int_name = f"{pair[0]}:{pair[1]}"
        # lifelines puts categorical level suffixes: treatment_arm[T.B]:high_risk_fish[T.yes]
        pattern = rf"^{re.escape(pair[0])}(?:\[.*?\])?:{re.escape(pair[1])}(?:\[.*?\])?$"
        matching = [idx for idx in cph.summary.index if re.match(pattern, idx)]
        for idx in matching:
            row = cph.summary.loc[idx]
            covariate_results.append({
                "covariate": f"{int_name} (interaction)",
                "hr": float(cph.hazard_ratios_.get(idx, 0)) if cph.hazard_ratios_.get(idx) else None,
                "ci_lower": np.exp(float(cph.confidence_intervals_.loc[idx].iloc[0])) if idx in cph.confidence_intervals_.index else None,
                "ci_upper": np.exp(float(cph.confidence_intervals_.loc[idx].iloc[1])) if idx in cph.confidence_intervals_.index else None,
                "wald_p": float(row["p"]) if "p" in row else None,
                "coef": float(row["coef"]) if "coef" in row else None,
                "se": float(row["se"]) if "se" in row else None,
                "z": float(row["z"]) if "z" in row else None,
                "reference_level": None,
                "tested_level": None,
            })

    # --- Overall model statistics ---
    lr_test_p = None
    try:
        lr_test = cph.log_likelihood_ratio_test()
        lr_test_p = float(lr_test.p_value)
    except Exception:
        logger.warning("LR test extraction failed", exc_info=True)
        lr_test_p = None

    concordance = float(cph.concordance_index_) if hasattr(cph, "concordance_index_") and cph.concordance_index_ else None

    # --- Proportional hazards diagnostics (Schoenfeld residuals) ---
    diagnostics = None
    try:
        from lifelines.statistics import proportional_hazard_test
        results = proportional_hazard_test(cph, df, time_transform="km")
        diag_rows = []
        for cov_name in results.summary.index:
            row = results.summary.loc[cov_name]
            diag_rows.append({
                "covariate": str(cov_name),
                "test_statistic": float(row.get("test_statistic", 0)),
                "p_value": float(row.get("p", 1.0)),
            })
        # Flag any violations
        violations = [r for r in diag_rows if r["p_value"] < 0.05]
        if violations:
            diag_rows.append({
                "warning": f"PH assumption violated for {len(violations)} covariate(s) (p<0.05). "
                          f"Consider time-varying coefficients or stratification for: "
                          f"{', '.join(v['covariate'] for v in violations)}"
            })
        diagnostics = {"test": "Schoenfeld residuals", "covariates": diag_rows}
    except Exception as e:
        diagnostics = {"test": "Schoenfeld residuals", "error": f"Could not compute: {e}"}

    # --- Missing data handling ---
    n_total = len(data)
    n_analyzed = len(df)
    n_excluded = n_total - n_analyzed

    return _uro(
        test_name="cox_ph_model",
        statistic=float(hr) if hr is not None else None,
        p_value=float(p) if p is not None else None,
        ci_lower=np.exp(float(ci.iloc[0])) if ci is not None else None,
        ci_upper=np.exp(float(ci.iloc[1])) if ci is not None else None,
        n_analyzed=n_analyzed,
        n_total=n_total,
        effect_size={
            "metric": "Hazard Ratio (primary treatment)",
            "value": float(hr),
        } if hr is not None else None,
        params={
            "covariates": covariates,
            "formula": formula,
            "per_covariate_results": covariate_results,
            "lr_test_p_value": lr_test_p,
            "concordance_index": concordance,
            "assumption_diagnostics": diagnostics,
            "missing_data": {"n_total": n_total, "n_analyzed": n_analyzed, "n_excluded": n_excluded},
        },
    )
