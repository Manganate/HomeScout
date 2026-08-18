"""Enrichment tests: rate limiting, walkability, amenity counting, assessments.

Nothing here touches the network — the public services are rate-limited and
non-commercial, so tests must never hammer them.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from homescout.enrichment import walkability
from homescout.enrichment.amenities import (
    CATEGORIES,
    bbox_key,
    build_category_query,
    cohort_bbox,
    count_near,
)
from homescout.enrichment.geo import RateLimiter
from homescout.enrichment.location import ratio

# ---------------------------------------------------------------------------
# Rate limiting — exceeding these limits gets the user banned, not throttled
# ---------------------------------------------------------------------------

def test_rate_limiter_enforces_minimum_interval():
    limiter = RateLimiter(rate_per_sec=20)  # 50 ms apart
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    elapsed = time.monotonic() - start
    # 5 acquisitions => 4 gaps minimum.
    assert elapsed >= 0.20 - 0.02, f"limiter released too fast ({elapsed:.3f}s)"


def test_rate_limiter_serializes_across_threads():
    """The limiter must hold across the whole analyze worker pool.

    A per-thread limiter would let a pool of N workers issue N times the
    permitted request rate, which is exactly how a public endpoint ban happens.
    """
    limiter = RateLimiter(rate_per_sec=20)
    timestamps: list[float] = []
    lock = threading.Lock()

    def hit():
        limiter.acquire()
        with lock:
            timestamps.append(time.monotonic())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: hit(), range(8)))

    timestamps.sort()
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    assert all(g >= 0.04 for g in gaps), f"concurrent calls broke the interval: {gaps}"


def test_rate_limiter_zero_rate_is_unrestricted():
    limiter = RateLimiter(rate_per_sec=0)
    assert limiter.acquire() == 0.0


# ---------------------------------------------------------------------------
# Walkability
# ---------------------------------------------------------------------------

def test_walkability_none_when_no_data():
    """None and zero mean different things.

    None is "we never got amenity data", which the scorer reweights around.
    Zero asserts "we looked and found nothing walkable".
    """
    assert walkability.score(None) is None


def test_walkability_zero_when_nothing_nearby():
    assert walkability.score({k: 0 for k in CATEGORIES}) == 0.0


def test_walkability_more_amenities_scores_higher():
    sparse = {"schools": 1, "cafes": 1, "parks": 0, "groceries": 0, "transit": 1}
    rich = {"schools": 3, "cafes": 12, "parks": 5, "groceries": 3, "transit": 8}
    assert walkability.score(rich) > walkability.score(sparse)


def test_walkability_saturates():
    """The tenth cafe must add far less than the first, so one restaurant strip
    cannot dominate the score."""
    at_saturation = {"schools": 3, "cafes": 12, "parks": 5, "groceries": 3, "transit": 8}
    far_beyond = {k: v * 50 for k, v in at_saturation.items()}
    assert walkability.score(at_saturation) == pytest.approx(100.0)
    assert walkability.score(far_beyond) == pytest.approx(100.0)


def test_walkability_bounded():
    assert 0 <= walkability.score({"cafes": 99999}) <= 100


def test_walkability_handles_missing_categories():
    assert walkability.score({"cafes": 5}) is not None


# ---------------------------------------------------------------------------
# Amenity counting (local, from a prefetched index)
# ---------------------------------------------------------------------------

def test_cohort_bbox_covers_all_points_with_padding():
    bbox = cohort_bbox([(51.0, -114.0), (51.1, -114.2)])
    south, west, north, east = bbox
    assert south < 51.0 and north > 51.1
    assert west < -114.2 and east > -114.0


def test_cohort_bbox_empty():
    assert cohort_bbox([]) is None


def test_count_near_uses_radius():
    # ~1.1 km per 0.01 degree of latitude.
    index = {
        "cafes": [(51.000, -114.0), (51.005, -114.0), (51.050, -114.0)],
        "parks": [],
    }
    counts = count_near(index, 51.0, -114.0, radius_m=1000)
    assert counts["cafes"] == 2, "only POIs within 1 km should count"
    assert counts["parks"] == 0


def test_count_near_none_index_returns_none():
    """An unavailable index must not read as a zero-amenity location."""
    assert count_near(None, 51.0, -114.0) is None


def test_build_category_query_includes_bbox_and_tags():
    query = build_category_query("cafes", (50.9, -114.3, 51.2, -113.9))
    assert "50.9,-114.3,51.2,-113.9" in query
    assert "cafe" in query and "out:json" in query


def test_bbox_key_is_stable_and_rounded():
    a = bbox_key((50.9001, -114.3001, 51.2001, -113.9001))
    b = bbox_key((50.9002, -114.3002, 51.2002, -113.9002))
    assert a == b, "near-identical cohorts should share one cached index"


# ---------------------------------------------------------------------------
# Assessment ratio
# ---------------------------------------------------------------------------

def test_ratio_below_assessment():
    assert ratio(650_000, 700_000) == pytest.approx(0.9286, abs=1e-4)


def test_ratio_above_assessment():
    assert ratio(770_000, 700_000) == pytest.approx(1.1, abs=1e-4)


@pytest.mark.parametrize("price,assessed", [(None, 700_000), (700_000, None), (700_000, 0)])
def test_ratio_missing_inputs(price, assessed):
    assert ratio(price, assessed) is None
