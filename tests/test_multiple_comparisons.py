"""Tests for multiple comparisons correction."""

import pytest

from core.stats.multiple_comparisons import bonferroni, holm_bonferroni, benjamini_hochberg, correct


def test_bonferroni_identity():
    """Single p-value unchanged by Bonferroni."""
    assert bonferroni([0.05]) == [0.05]
    assert bonferroni([0.01]) == [0.01]


def test_bonferroni_caps_at_one():
    adjusted = bonferroni([0.5, 0.5])
    assert all(a == 1.0 for a in adjusted)


def test_bonferroni_multiple():
    adjusted = bonferroni([0.01, 0.03, 0.05])
    assert adjusted[0] == pytest.approx(0.03)  # 0.01 * 3
    assert adjusted[1] == pytest.approx(0.09)  # 0.03 * 3
    assert adjusted[2] == pytest.approx(0.15)  # 0.05 * 3


def test_holm_bonferroni():
    adjusted = holm_bonferroni([0.01, 0.03, 0.05])
    # After monotonicity enforcement: 0.06, 0.06, 0.05
    assert adjusted == pytest.approx([0.06, 0.06, 0.05])


def test_holm_monotonic():
    """Holm-Bonferroni adjusted p-values must be non-decreasing."""
    adjusted = holm_bonferroni([0.001, 0.01, 0.1, 0.5])
    for i in range(len(adjusted) - 1):
        assert adjusted[i] <= adjusted[i + 1] + 1e-10


def test_benjamini_hochberg():
    adjusted = benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05])
    assert all(a <= 1.0 for a in adjusted)
    assert adjusted[0] <= adjusted[1]  # non-decreasing


def test_empty_input():
    assert bonferroni([]) == []
    assert holm_bonferroni([]) == []
    assert benjamini_hochberg([]) == []


def test_correct_dispatch():
    pvals = [0.01, 0.02, 0.03]
    assert correct(pvals, "bonferroni")[0] == 0.03
    assert len(correct(pvals, "holm_bonferroni")) == 3
    assert len(correct(pvals, "benjamini_hochberg")) == 3
    assert correct(pvals, "unknown") == pvals
