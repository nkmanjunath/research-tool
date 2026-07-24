"""Multiple comparisons correction.

Applied automatically whenever more than one test is run in a single analysis
pass — never leave this to the researcher to remember.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import false_discovery_control


def bonferroni(p_values: list[float], alpha: float = 0.05) -> list[float]:
    """Bonferroni correction.  Simplest, most conservative."""
    n = len(p_values)
    if n == 0:
        return []
    return [min(p * n, 1.0) for p in p_values]


def holm_bonferroni(p_values: list[float], alpha: float = 0.05) -> list[float]:
    """Holm-Bonferroni step-down correction.  Less conservative than Bonferroni."""
    n = len(p_values)
    if n == 0:
        return []
    sorted_idx = np.argsort(p_values)
    sorted_p = np.array(p_values)[sorted_idx]
    adjusted = np.zeros(n)
    for i, p in enumerate(sorted_p):
        adjusted[i] = min(p * (n - i), 1.0)
    # Enforce monotonicity
    for i in range(n - 2, -1, -1):
        adjusted[i] = max(adjusted[i], adjusted[i + 1])
    result = np.zeros(n)
    result[sorted_idx] = adjusted
    return result.tolist()


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[float]:
    """Benjamini-Hochberg FDR correction.  Controls false discovery rate."""
    return false_discovery_control(p_values, method="bh").tolist()


def correct(p_values: list[float], method: str = "bonferroni") -> list[float]:
    """Apply multiple comparisons correction.

    Parameters
    ----------
    p_values : list[float]
    method : str — "bonferroni" (default), "holm_bonferroni", or "benjamini_hochberg"

    Returns
    -------
    list[float] — adjusted p-values
    """
    if method == "bonferroni":
        return bonferroni(p_values)
    elif method == "holm_bonferroni":
        return holm_bonferroni(p_values)
    elif method == "benjamini_hochberg":
        return benjamini_hochberg(p_values)
    return p_values
