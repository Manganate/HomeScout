"""Scoring tests, focused on the missing-data policy.

The central risk this module guards: percentile-ranking a partially-null column
silently ranks the cohort by *which listings had complete data*. These tests
fail if that regression is ever reintroduced.
"""

from __future__ import annotations

import pytest

from homescout.models import SearchCriteria
from homescout.scoring.scorer import percentile_ranks, score_cohort

# ---------------------------------------------------------------------------
# percentile_ranks
# ---------------------------------------------------------------------------

def test_percentile_ranks_orders_ascending():
    assert percentile_ranks([10, 20, 30]) == [0.0, 50.0, 100.0]


def test_percentile_ranks_ties_share_midpoint():
    ranks = percentile_ranks([5, 5, 9])
    assert ranks[0] == ranks[1], "tied values must score identically"
    assert ranks[2] > ranks[0]


def test_percentile_ranks_preserves_nulls():
    ranks = percentile_ranks([1, None, 3])
    assert ranks[1] is None, "nulls must never be imputed"
    assert ranks[0] == 0.0 and ranks[2] == 100.0


def test_percentile_ranks_single_value_is_midpoint():
    assert percentile_ranks([42]) == [50.0]


def test_percentile_ranks_all_null():
    assert percentile_ranks([None, None]) == [None, None]


def test_percentile_ranks_resists_outliers():
    """An extreme outlier must not compress the rest of the cohort.

    This is the reason for percentile rank over min-max: under min-max the
    non-outlier values would all collapse toward zero.
    """
    ranks = percentile_ranks([1, 2, 3, 4, 1_000_000])
    spread = ranks[3] - ranks[0]
    assert spread >= 70, f"outlier compressed the cohort (spread={spread})"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _row(mls_id, **kw):
    base = {
        "mls_id": mls_id,
        "price": 700_000,
        "sqft": 1500,
        "assessment_ratio": 1.0,
        "commute_min": 15.0,
        "walk_proxy": 50.0,
        "schools_n": 2, "cafes_n": 5, "parks_n": 3, "groceries_n": 1, "transit_n": 4,
        "days_on_market": 10,
    }
    base.update(kw)
    return base


@pytest.fixture
def criteria():
    return SearchCriteria()


# ---------------------------------------------------------------------------
# Missing-data policy
# ---------------------------------------------------------------------------

def test_missing_sqft_still_scores_value_from_assessment(criteria):
    """One Value component missing must not null out Value entirely."""
    rows = [_row("A"), _row("B", sqft=None, assessment_ratio=0.85), _row("C", assessment_ratio=1.2)]
    scored = {s.mls_id: s for s in score_cohort(rows, criteria)}

    assert scored["B"].score_value is not None, "Value must survive a single missing component"
    assert "price_per_sqft" in scored["B"].missing
    assert scored["B"].data_completeness < scored["A"].data_completeness


def test_both_value_components_missing_excludes_value_not_zeroes_it(criteria):
    """A listing with no Value signal must be excluded from Value, not scored 0.

    Scoring it zero is indistinguishable from "genuinely terrible value" and
    would bury a listing whose only fault is a missing field.
    """
    rows = [_row("A"), _row("B"), _row("C", sqft=None, assessment_ratio=None)]
    scored = {s.mls_id: s for s in score_cohort(rows, criteria)}

    assert scored["C"].score_value is None
    assert scored["C"].score_total > 0, "must still rank on location and freshness"


def test_ranking_is_not_driven_by_data_completeness(criteria):
    """The core regression guard.

    Listings with missing fields are otherwise identical to complete ones. If
    the scorer imputed nulls as zero, every incomplete listing would sink to the
    bottom and rank order would track completeness.
    """
    rows = []
    for i in range(6):
        rows.append(_row(f"complete{i}", days_on_market=10 + i))
    for i in range(6):
        # Same underlying quality, but missing the sqft signal.
        rows.append(_row(f"partial{i}", sqft=None, days_on_market=10 + i))

    scored = sorted(score_cohort(rows, criteria), key=lambda s: s.score_total, reverse=True)
    top_half = {s.mls_id for s in scored[:6]}

    n_partial_in_top = sum(1 for m in top_half if m.startswith("partial"))
    assert 1 <= n_partial_in_top <= 5, (
        f"rank order tracks data completeness: {n_partial_in_top}/6 partial listings in top half"
    )


def test_completeness_is_reported(criteria):
    rows = [_row("A"), _row("B", sqft=None, walk_proxy=None)]
    scored = {s.mls_id: s for s in score_cohort(rows, criteria)}

    assert scored["A"].data_completeness == 100.0
    assert scored["B"].data_completeness < 100.0


# ---------------------------------------------------------------------------
# Scoring direction
# ---------------------------------------------------------------------------

def test_cheaper_per_sqft_scores_higher(criteria):
    rows = [_row("cheap", price=600_000, sqft=2000), _row("dear", price=900_000, sqft=1000)]
    scored = {s.mls_id: s for s in score_cohort(rows, criteria)}
    assert scored["cheap"].score_value > scored["dear"].score_value


def test_below_assessment_scores_higher(criteria):
    rows = [_row("under", assessment_ratio=0.85), _row("over", assessment_ratio=1.25)]
    scored = {s.mls_id: s for s in score_cohort(rows, criteria)}
    assert scored["under"].score_value > scored["over"].score_value, \
        "a listing priced below assessed value must score better on Value"


def test_shorter_commute_scores_higher(criteria):
    rows = [_row("near", commute_min=5), _row("far", commute_min=45)]
    scored = {s.mls_id: s for s in score_cohort(rows, criteria)}
    assert scored["near"].score_location > scored["far"].score_location


def test_newer_listing_scores_higher(criteria):
    rows = [_row("new", days_on_market=1), _row("old", days_on_market=90)]
    scored = {s.mls_id: s for s in score_cohort(rows, criteria)}
    assert scored["new"].score_freshness > scored["old"].score_freshness


def test_weights_shift_ranking():
    """The user's priority weights must actually change the outcome."""
    rows = [
        _row("bargain_far", price=500_000, sqft=2500, commute_min=40, days_on_market=60),
        _row("close_pricey", price=900_000, sqft=1000, commute_min=4, days_on_market=60),
    ]

    value_first = SearchCriteria(weight_value=10, weight_location=1, weight_freshness=1)
    location_first = SearchCriteria(weight_value=1, weight_location=10, weight_freshness=1)

    by_value = {s.mls_id: s.score_total for s in score_cohort(rows, value_first)}
    by_location = {s.mls_id: s.score_total for s in score_cohort(rows, location_first)}

    assert by_value["bargain_far"] > by_value["close_pricey"]
    assert by_location["close_pricey"] > by_location["bargain_far"]


def test_empty_cohort():
    assert score_cohort([], SearchCriteria()) == []
