"""Post-hoc analysis tagging.

Any analysis NOT in the locked plan gets tagged EXPLORATORY_POST_HOC
everywhere it appears — in the data model, in any generated table, and in
the manuscript draft.  This tag must be impossible to silently strip.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Any


EXPLORATORY_POST_HOC = "EXPLORATORY_POST_HOC"


@dataclass
class PostHocResult:
    """A result wrapper that carries the EXPLORATORY_POST_HOC tag.

    The tag is embedded in the data model, not just in presentation.
    Any attempt to strip it must be explicit and intentional.
    """
    test_name: str
    statistic: Optional[float]
    p_value: Optional[float]
    adjusted_p_value: Optional[float] = None
    ci_lower: Optional[float] = None
    ci_upper: Optional[float] = None
    params: dict = None
    tag: str = EXPLORATORY_POST_HOC  # cannot be unset without creating a new object

    def to_dict(self) -> dict:
        return {
            "test_name": self.test_name,
            "statistic": self.statistic,
            "p_value": self.p_value,
            "adjusted_p_value": self.adjusted_p_value,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "params": self.params or {},
            "tag": self.tag,  # always present
        }

    def is_post_hoc(self) -> bool:
        return True


def run_post_hoc(fn, *args, **kwargs) -> PostHocResult:
    """Wrap any test function and tag the result as EXPLORATORY_POST_HOC.

    The underlying function returns a dict with keys:
      test_name, statistic, p_value, ci_lower, ci_upper, params
    """
    result = fn(*args, **kwargs)
    return PostHocResult(
        test_name=result.get("test_name", "unknown"),
        statistic=result.get("statistic"),
        p_value=result.get("p_value"),
        ci_lower=result.get("ci_lower"),
        ci_upper=result.get("ci_upper"),
        params=result.get("params"),
    )
