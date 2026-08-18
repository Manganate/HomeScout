"""Filtering, validation, dedupe, and address-normalization tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from homescout.filtering import criteria as crit
from homescout.filtering.quality import (
    dedupe,
    normalize_address,
    reconcile_age,
    validate,
)
from homescout.models import Listing, SearchCriteria, days_since

CBD_LAT, CBD_LON = 45.0, -95.0


def _listing(mls_id="X1", **kw):
    base = dict(
        mls_id=mls_id, source="test", url="http://example.invalid",
        address="123 Main St SW", city="Example City",
        price=700_000, beds=3.0, baths=2.0, sqft=1500,
        property_type="House", latitude=44.995, longitude=-95.007,
        days_on_market=10,
    )
    base.update(kw)
    return Listing(**base)


# ---------------------------------------------------------------------------
# Address normalization — both sides of the assessment join depend on this
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("123 Elbow Drive Southwest", "123 ELBOW DR SW"),
    ("123 Elbow Dr SW", "123 ELBOW DR SW"),
    ("1010 5th Street S.W.", "1010 5TH ST SW"),
    ("Unit 5 123 Main St NW", "123 MAIN ST NW"),
    ("  123   Main   St   SW  ", "123 MAIN ST SW"),
])
def test_normalize_address_variants_collapse(raw, expected):
    assert normalize_address(raw) == expected


@pytest.mark.parametrize("address", [
    "15 DEERMEADE PL SE",
    "175 DEER LANE RD SE",
    "24 DEER LANE BA SE",
    "88 HARVEST GROVE GR NE",
])
def test_normalize_address_is_idempotent_on_city_format(address):
    """City-format addresses must survive normalization unchanged.

    The parcel index is keyed on these, so any rewriting here breaks the join.
    """
    assert normalize_address(address) == address
    assert normalize_address(normalize_address(address)) == address


def test_normalize_address_does_not_abbreviate_street_names():
    """Only the street-type position may be abbreviated.

    "175 DEER LANE RD SE" has the street name "DEER LANE" and type "RD".
    Abbreviating every matching word would corrupt it to "DEER LN RD".
    """
    assert normalize_address("175 Deer Lane Rd SE") == "175 DEER LANE RD SE"
    assert "LN" not in normalize_address("175 Deer Lane Rd SE")


def test_normalize_address_empty():
    assert normalize_address("") == ""
    assert normalize_address(None) == ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,fragment", [
    ({"price": None}, "price"),
    ({"price": 0}, "price"),
    ({"latitude": None}, "coordinates"),
    ({"longitude": None}, "coordinates"),
    ({"latitude": 999.0}, "out of range"),
    ({"sqft": 45}, "implausible"),
    ({"sqft": 99_000}, "implausible"),
    ({"address": ""}, "address"),
])
def test_validate_rejects_bad_records(kwargs, fragment):
    reason = validate(_listing(**kwargs))
    assert reason is not None and fragment in reason


def test_validate_accepts_good_record():
    assert validate(_listing()) is None


def test_missing_sqft_is_not_a_rejection():
    """A listing without sqft is still rankable on location and freshness."""
    assert validate(_listing(sqft=None)) is None


# ---------------------------------------------------------------------------
# Age reconciliation
# ---------------------------------------------------------------------------

def test_list_date_overrides_reported_day_count():
    """A real date beats a provider-reported count, which is often coarse."""
    three_days_ago = (datetime.now(UTC).date() - timedelta(days=3)).isoformat()
    listing = _listing(list_date=three_days_ago, days_on_market=400)
    reconcile_age(listing)
    assert listing.days_on_market == 3


def test_unparseable_date_leaves_reported_count():
    listing = _listing(list_date="not-a-date", days_on_market=12)
    reconcile_age(listing)
    assert listing.days_on_market == 12


def test_days_since_handles_formats():
    today = datetime.now(UTC).date()
    assert days_since(today.isoformat()) == 0
    assert days_since((today - timedelta(days=5)).isoformat()) == 5
    assert days_since(None) is None
    assert days_since("garbage") is None


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def test_dedupe_by_mls_id():
    # Distinct addresses, so only the repeated mls_id is the duplicate.
    kept, dropped = dedupe([
        _listing("A", address="1 First St SW"),
        _listing("A", address="1 First St SW"),
        _listing("B", address="2 Second Av NW"),
    ])
    assert dropped == 1 and len(kept) == 2
    assert {l.mls_id for l in kept} == {"A", "B"}


def test_dedupe_by_address_and_price():
    a = _listing("A", address="123 Main St SW", price=700_000)
    b = _listing("B", address="123 Main Street Southwest", price=700_000)
    kept, dropped = dedupe([a, b])
    assert dropped == 1, "same address written differently must dedupe"
    assert kept[0].mls_id == "A", "first occurrence wins"


def test_dedupe_keeps_same_address_at_different_price():
    a = _listing("A", address="123 Main St SW", price=700_000)
    b = _listing("B", address="123 Main St SW", price=750_000)
    kept, _ = dedupe([a, b])
    assert len(kept) == 2


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------

@pytest.fixture
def c():
    return SearchCriteria(
        cbd_lat=CBD_LAT, cbd_lon=CBD_LON,
        price_min=600_000, price_max=800_000,
        beds_min=3, baths_min=2, max_listing_age_days=30,
        max_commute_min=20, property_types=["House", "Townhouse"],
    )


@pytest.mark.parametrize("kwargs,fragment", [
    ({"price": 500_000}, "below minimum"),
    ({"price": 900_000}, "above maximum"),
    ({"beds": 2}, "beds below"),
    ({"baths": 1}, "baths below"),
    ({"days_on_market": 45}, "over 30d"),
    ({"property_type": "Condo"}, "not in allowed"),
])
def test_criteria_rejects(kwargs, fragment, c):
    reason = crit.check(_listing(**kwargs), c)
    assert reason is not None and fragment in reason


def test_criteria_accepts_matching(c):
    assert crit.check(_listing(), c) is None


def test_null_age_is_kept(c):
    """No usable date is not a reason to discard — the scorer handles the gap."""
    assert crit.check(_listing(days_on_market=None), c) is None


# ---------------------------------------------------------------------------
# Distance — Phase A
# ---------------------------------------------------------------------------

def test_haversine_known_distance():
    """Two points with a known great-circle distance of roughly 403 km."""
    d = crit.haversine_km(40.0, -75.0, 42.0, -71.0)
    assert 390 < d < 415, d


def test_haversine_zero():
    assert crit.haversine_km(45.0, -95.0, 45.0, -95.0) == pytest.approx(0.0, abs=1e-9)


def test_geo_bound_is_generous(c):
    """Phase A must only discard listings that *cannot* qualify.

    20 minutes at an 80 km/h ceiling is ~26.7 km; a real 20-minute drive covers
    far less, so nothing reachable is cut before routing runs.
    """
    assert c.geo_bound_km == pytest.approx(26.67, abs=0.1)


def test_far_listing_rejected_by_geo_bound(c):
    # Far outside any reasonable commute.
    reason = crit.check(_listing(latitude=48.0, longitude=-95.0), c)
    assert reason is not None and "pre-filter bound" in reason


def test_distance_km_is_recorded(c):
    listing = _listing()
    crit.check(listing, c)
    assert listing.distance_km is not None and listing.distance_km >= 0


def test_apply_splits_cohort(c):
    listings = [_listing("ok"), _listing("cheap", price=100_000), _listing("ok2")]
    passed, rejected = crit.apply(listings, c)
    assert [l.mls_id for l in passed] == ["ok", "ok2"]
    assert len(rejected) == 1 and rejected[0][0].mls_id == "cheap"
