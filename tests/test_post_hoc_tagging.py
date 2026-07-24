"""Adversarial tests for post-hoc tagging.

Any analysis not in the locked plan gets tagged EXPLORATORY_POST_HOC
everywhere downstream.  The tag must be impossible to strip without
creating a new object.
"""

from core.stats.post_hoc import run_post_hoc, PostHocResult, EXPLORATORY_POST_HOC


def test_post_hoc_result_has_tag():
    r = PostHocResult(test_name="test", statistic=1.0, p_value=0.05)
    assert r.tag == EXPLORATORY_POST_HOC
    assert r.is_post_hoc() is True


def test_tag_survives_to_dict():
    r = PostHocResult(test_name="test", statistic=1.0, p_value=0.05)
    d = r.to_dict()
    assert d["tag"] == EXPLORATORY_POST_HOC


def test_tag_not_silently_removable():
    """The tag is a class attribute frozen at creation."""
    r = PostHocResult(test_name="test", statistic=1.0, p_value=0.05)
    d = r.to_dict()
    assert d["tag"] is not None
    assert d["tag"] == "EXPLORATORY_POST_HOC"


def test_run_post_hoc_wraps_result():
    def fake_test():
        return {"test_name": "chi_square", "statistic": 3.84, "p_value": 0.05,
                "ci_lower": None, "ci_upper": None, "params": {}}
    r = run_post_hoc(fake_test)
    assert isinstance(r, PostHocResult)
    assert r.tag == EXPLORATORY_POST_HOC
    assert r.test_name == "chi_square"


def test_multiple_post_hoc_results():
    """Multiple post-hoc results each carry their own tag."""
    results = [
        PostHocResult(test_name="a", statistic=1.0, p_value=0.05),
        PostHocResult(test_name="b", statistic=2.0, p_value=0.01),
    ]
    for r in results:
        assert r.tag == EXPLORATORY_POST_HOC
        assert "EXPLORATORY_POST_HOC" in r.to_dict()["tag"]
