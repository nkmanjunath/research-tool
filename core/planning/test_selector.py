"""Statistical test selector — suggests appropriate tests based on variable types.

This module SUGGESTS only.  It never auto-picks a test based on outcome data.
Distribution checks that inform test selection must happen pre-lock on baseline
data or be declared explicitly in the study plan.
"""

from __future__ import annotations
from abc import ABC, abstractmethod

from core.database import get_connection, DATA_ROOT


# ── AssumptionCheck interface ──────────────────────────────────────────


class AssumptionCheck(ABC):
    """Pluggable pre-lock assumption check for a statistical test type.

    Subclasses implement ``applies_to`` (which test names they handle) and
    ``check`` (which produces warnings by querying only marginal / independently-
    derivable quantities — never a cross-tabulation of outcome × group before
    the plan is locked).

    Warning messages must never name specific outcome category values or group
    labels — only variable names, counts, and dimensions.
    """

    @abstractmethod
    def applies_to(self, test_name: str) -> bool:
        ...

    @abstractmethod
    def check(
        self, study_id: str, test: dict, group_col: str, conn, var_info: dict
    ) -> list[str]:
        ...


# ── Registry of all registered checks ──────────────────────────────────
# Each AssumptionCheck is instantiated once at module level.  The dispatcher
# (check_assumptions) iterates this list for every declared test.
_CHECKERS: list[AssumptionCheck] = []


def _register(checker: type[AssumptionCheck]) -> type[AssumptionCheck]:
    _CHECKERS.append(checker())
    return checker


# ── Test suggestion helpers ────────────────────────────────────────────


def suggested_tests(
    primary_outcome_data_type: str,
    study_type: str,
    n_comparison_groups: int = 2,
    is_paired: bool = False,
    n_total: int | None = None,
) -> list[dict]:
    """Return a list of appropriate statistical tests given the variable types."""
    if primary_outcome_data_type == "categorical":
        return _categorical_tests(n_comparison_groups, is_paired, n_total)
    elif primary_outcome_data_type == "continuous":
        return _continuous_tests(n_comparison_groups, is_paired)
    elif primary_outcome_data_type == "time_to_event":
        return _survival_tests(n_comparison_groups)
    return []


def _categorical_tests(n_groups: int, paired: bool, n_total: int | None = None) -> list[dict]:
    if n_total is not None and n_total < 30:
        tests = [
            {
                "test_name": "fishers_exact",
                "rationale": "Exact test for 2x2 tables when sample sizes are small.",
                "assumptions": "None.",
            },
            {
                "test_name": "chi_square",
                "rationale": "Tests association between categorical variables.",
                "assumptions": "Expected frequency >=5 in each cell. May be unreliable with small samples.",
            },
        ]
    else:
        tests = [
            {
                "test_name": "chi_square",
                "rationale": "Tests association between categorical variables.",
                "assumptions": "Expected frequency >=5 in each cell.",
            },
            {
                "test_name": "fishers_exact",
                "rationale": "Exact test for 2x2 tables when sample sizes are small.",
                "assumptions": "None.",
            },
        ]
    if n_groups == 2 and paired:
        tests.append({
            "test_name": "mcnemar",
            "rationale": "Paired categorical data (before/after, matched pairs).",
            "assumptions": "Binary outcome, discordant pairs.",
        })
    return tests


def _continuous_tests(n_groups: int, paired: bool) -> list[dict]:
    tests = [
        {
            "test_name": "t_test" if n_groups == 2 else "anova",
            "rationale": "Compares means across groups. Parametric.",
            "assumptions": "Normality and homogeneity of variance.",
        },
        {
            "test_name": "mann_whitney_u" if n_groups == 2 else "kruskal_wallis",
            "rationale": "Non-parametric comparison of distributions.",
            "assumptions": "None (distribution-free).",
        },
    ]
    if n_groups == 2 and paired:
        tests.extend([
            {
                "test_name": "paired_t_test",
                "rationale": "Paired continuous data. Parametric.",
                "assumptions": "Normality of differences.",
            },
            {
                "test_name": "wilcoxon_signed_rank",
                "rationale": "Paired continuous data. Non-parametric.",
                "assumptions": "Symmetric distribution of differences.",
            },
        ])
    return tests


def _survival_tests(n_groups: int) -> list[dict]:
    tests = [
        {
            "test_name": "kaplan_meier_logrank",
            "rationale": "Compares survival distributions between groups.",
            "assumptions": "Non-informative censoring, proportional hazards.",
        },
        {
            "test_name": "cox_proportional_hazards",
            "rationale": "Multivariable survival analysis with covariates.",
            "assumptions": "Proportional hazards.",
        },
    ]
    return tests


# ── Chi-square: sparse expected-cell warning ───────────────────────────


@_register
class ChiSquareAssumptionCheck(AssumptionCheck):
    """Warn when any expected cell count < 5 for a chi-square test.

    Expected counts are computed from marginal totals only (row counts from
    the shadow table, column counts from the raw group column) — never a
    cross-tabulation of outcome × arm.
    """

    def applies_to(self, test_name: str) -> bool:
        return test_name == "chi_square"

    def check(
        self, study_id: str, test: dict, group_col: str, conn, var_info: dict
    ) -> list[str]:
        var_name = test.get("variable_name", "")
        if not var_name:
            return []
        if var_info.get(var_name) != "categorical":
            return []

        shadow_table = f"raw_masked_{study_id}"
        raw_table = f"raw_{study_id}"

        # Row marginals — outcome category counts from the shadow table
        try:
            cur = conn.execute(
                f'SELECT "{var_name}", COUNT(*) AS cnt FROM {shadow_table} '
                f'WHERE "{var_name}" IS NOT NULL GROUP BY "{var_name}"'
            )
            row_marginals = [r["cnt"] for r in cur.fetchall()]
        except Exception:
            return [
                f"Could not read outcome data for '{var_name}' from shadow table."
            ]

        if not row_marginals:
            return []

        # Column marginals — group counts from the raw table
        try:
            cur = conn.execute(
                f'SELECT "{group_col}", COUNT(*) AS cnt FROM {raw_table} '
                f'WHERE "{group_col}" IS NOT NULL GROUP BY "{group_col}"'
            )
            col_marginals = [r["cnt"] for r in cur.fetchall()]
        except Exception:
            return [
                f"Could not read group data for '{group_col}' from raw table."
            ]

        if not col_marginals:
            return []

        # Expected counts under independence
        grand = sum(row_marginals)
        min_expected = float("inf")
        for r_cnt in row_marginals:
            for c_cnt in col_marginals:
                exp = r_cnt * c_cnt / grand
                if exp < min_expected:
                    min_expected = exp

        if min_expected < 5:
            return [
                f"chi_square on '{var_name}': minimum expected cell count is "
                f"{min_expected:.1f} (below 5 threshold for a "
                f"{len(row_marginals)}×{len(col_marginals)} table). "
                f"Consider using fisher_exact instead."
            ]

        return []


# FIXME: Add t-test normality check here.
#        Must check normality using ONLY baseline/masked-permitted data —
#        no outcome values should be cross-tabulated with treatment groups.
#        Relevant test_names: "t_test", "paired_t_test"


@_register
class TTestAssumptionCheck(AssumptionCheck):
    """Check normality of pooled outcome distribution for t-test plans.

    Reads the marginal (ungrouped) outcome values from the shadow table and
    runs Shapiro-Wilk.  Warns if n < 30 *and* the normality test suggests
    non-normality (p < 0.05).  For n >= 30 the Central Limit Theorem provides
    robustness — no warning regardless of the Shapiro-Wilk result.

    IMPORTANT: this checks the *pooled* distribution only, never per-group.
    Per-group normality is not checked pre-lock because it would reveal
    structure about the association between the grouping variable and the
    outcome, the same class of problem as the chi-square cross-tab leak.
    """

    def applies_to(self, test_name: str) -> bool:
        return test_name in ("t_test", "paired_t_test")

    def check(
        self, study_id: str, test: dict, group_col: str, conn, var_info: dict
    ) -> list[str]:
        var_name = test.get("variable_name", "")
        if not var_name:
            return []
        if var_info.get(var_name) != "continuous":
            return []

        shadow_table = f"raw_masked_{study_id}"

        # Read marginal (pooled, ungrouped) outcome values from shadow table.
        # No GROUP BY, no reference to group_col — just the raw column values.
        try:
            cur = conn.execute(
                f'SELECT CAST("{var_name}" AS REAL) AS val '
                f'FROM {shadow_table} '
                f'WHERE "{var_name}" IS NOT NULL'
            )
            vals = [r["val"] for r in cur.fetchall() if r["val"] is not None]
        except Exception:
            return [
                f"Could not read outcome data for '{var_name}' from shadow table."
            ]

        if not vals:
            return []

        n = len(vals)
        # CLT robustness for n >= 30 — skip test, no warning
        if n >= 30:
            return []

        from scipy.stats import shapiro as shapiro_test
        try:
            _, p = shapiro_test(vals)
        except Exception:
            return []

        if p < 0.05:
            return [
                f"t_test on '{var_name}': pooled distribution may be non-normal "
                f"(Shapiro-Wilk p={p:.4f}, n={n}). "
                f"Consider mann_whitney instead."
            ]

        return []


@_register
class CoxPHAssumptionCheck(AssumptionCheck):
    """Screen Cox plans using only marginal event information available pre-lock.

    Computes events-per-variable (EPV) from the pooled event count and declared
    covariates.  Warns when EPV < 10 (common rule of thumb).

    NOTE: The true proportional-hazards assumption (Schoenfeld residuals) cannot
    be checked pre-lock — it requires the fitted Cox model, which needs unmasked,
    grouped outcome data.  This EPV check is a feasibility proxy only.  The real
    PH check runs post-unmask inside ``_cox_ph()``.
    """

    def applies_to(self, test_name: str) -> bool:
        return test_name == "cox_proportional_hazards"

    def check(self, study_id: str, test: dict, group_col: str, conn, var_info: dict) -> list[str]:
        var_name = test.get("variable_name", "")
        if var_info.get(var_name) != "time_to_event":
            return []
        prefix = var_name.replace("_days", "").replace("_months", "").replace("_time", "")
        event_col = f"{prefix}_event"
        shadow_table = f"raw_masked_{study_id}"

        # Number of covariates declared in the plan (passed through from cmd_plan)
        n_covariates = max(int(test.get("n_covariates", 0)), 0)
        # +1 for the primary group variable (treatment_arm)
        total_predictors = n_covariates + 1

        try:
            row = conn.execute(
                f'SELECT COUNT("{event_col}") AS n_events '
                f'FROM {shadow_table} '
                f'WHERE CAST("{event_col}" AS INTEGER) = 1'
            ).fetchone()
        except Exception:
            return [
                f"cox_proportional_hazards on '{var_name}': "
                "could not count events from shadow table."
            ]

        n_events = int(row["n_events"] or 0)
        if n_events == 0:
            return [
                f"cox_proportional_hazards on '{var_name}': "
                "zero events recorded in pooled data — Cox model cannot converge."
            ]

        epv = n_events / total_predictors if total_predictors > 0 else n_events
        if epv < 10:
            return [
                f"cox_proportional_hazards on '{var_name}': "
                f"{n_events} events across {total_predictors} predictor(s) "
                f"(EPV={epv:.1f}). "
                f"Cox models are unreliable below 10 events per predictor. "
                f"Consider reducing the number of covariates or using a "
                f"simpler analysis."
            ]

        return []

# FIXME: Add ANOVA variance-homogeneity check here.
#        Must check homogeneity using ONLY baseline/group marginal data.
#        Relevant test_names: "anova"


# ── Public API ─────────────────────────────────────────────────────────


def check_assumptions(
    study_id: str,
    tests: list[dict],
    group_col: str = "treatment_arm",
) -> list[str]:
    """Check pre-lock assumptions for each planned test.

    Iterates the registered :class:`AssumptionCheck` implementations and
    calls each one whose ``applies_to`` matches the test name.  All checks
    use only marginal / independently-derivable quantities — never a cross-
    tabulation of outcome × group before the plan is locked.

    Parameters
    ----------
    study_id : str
    tests : list[dict]
        Each dict must have at least ``test_name`` and ``variable_name`` keys.
    group_col : str
        Column name for the comparison groups (e.g. treatment_arm).

    Returns
    -------
    list[str]
        Warning strings — empty when no assumptions are violated.
    """
    conn = get_connection(study_id)

    # Pre-fetch variable data types
    cur = conn.execute(
        "SELECT column_name, data_type FROM variables WHERE study_id=?",
        (study_id,),
    )
    var_info = {r["column_name"]: r["data_type"] for r in cur.fetchall()}

    warnings: list[str] = []

    for test in tests:
        test_name = test.get("test_name", "")
        for checker in _CHECKERS:
            if checker.applies_to(test_name):
                warnings.extend(
                    checker.check(study_id, test, group_col, conn, var_info)
                )

    conn.close()
    return warnings
