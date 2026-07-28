"""Parsing tests for the REALTOR.ca source.

No browser and no network — these exercise only the payload-to-Listing
translation, which is the part most likely to break on a front-end change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from homescout.discovery.realtor_ca import (
    RealtorCaSource,
    _absolute,
    _days_on_market,
    _list_date,
    _price,
    _rooms,
    _split_address,
    _sqft,
)


@pytest.fixture
def source():
    return RealtorCaSource(config={"headless": True})


def _result(**overrides):
    base = {
        "MlsNumber": "A2100001",
        "RelativeDetailsURL": "/real-estate/12345678/123-main-st-sw-calgary",
        "TimeOnRealtor": "4 days",
        "Property": {
            "Price": "$725,000",
            "Type": "Single Family",
            "SizeTotal": "5,100 sqft",
            "Address": {
                "AddressText": "123 Main St SW, Calgary, Alberta T2P1A1",
                "Latitude": "51.0400",
                "Longitude": "-114.0700",
            },
        },
        "Building": {"Bedrooms": "3 + 1", "BathroomTotal": "2", "SizeInterior": "1,650 sqft"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Whole-record parsing
# ---------------------------------------------------------------------------

def test_parses_a_complete_record(source):
    listing = source._to_listing(_result())

    assert listing.mls_id == "A2100001"
    assert listing.source == "realtor_ca"
    assert listing.price == 725_000
    assert listing.beds == 4.0, "'3 + 1' means 3 main plus 1 basement bedroom"
    assert listing.baths == 2.0
    assert listing.sqft == 1650
    assert listing.latitude == pytest.approx(51.04)
    assert listing.longitude == pytest.approx(-114.07)
    assert listing.address == "123 Main St SW"
    assert listing.city == "Calgary"
    assert listing.postal_code == "T2P1A1"
    assert listing.url.startswith("https://www.realtor.ca/")


def test_missing_mls_number_is_skipped(source):
    assert source._to_listing({"Property": {}}) is None


def test_malformed_record_does_not_raise(source):
    assert source._to_listing({"MlsNumber": "X1"}) is not None
    assert source._to_listing({}) is None


def test_missing_sqft_survives_parsing(source):
    """A listing without interior size must still parse — the scorer reweights."""
    record = _result(Building={"Bedrooms": "3", "BathroomTotal": "2"})
    listing = source._to_listing(record)
    assert listing is not None and listing.sqft is None


def test_parse_deduplicates_across_pages(source):
    payloads = [{"Results": [_result(), _result()]}, {"Results": [_result()]}]
    from homescout.models import SearchCriteria
    listings = source._parse(payloads, SearchCriteria())
    assert len(listings) == 1, "the same MLS number across pages must appear once"


# ---------------------------------------------------------------------------
# Listing age — the field the freshness score depends on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("4 days", 4),
    ("1 day", 1),
    ("22 hours", 1),
    ("2 weeks", 14),
    ("1 month", 30),
    ("3 months", 91),
    (7, 7),
])
def test_days_on_market_parses_formatted_strings(raw, expected):
    assert _days_on_market({"TimeOnRealtor": raw}) == expected


def test_days_on_market_unparseable():
    assert _days_on_market({"TimeOnRealtor": "recently"}) is None
    assert _days_on_market({}) is None


def test_list_date_prefers_a_real_timestamp_over_the_age_string():
    """A coarse "1 month" cannot support a 30-day cut or day-level freshness,
    so an actual date must win whenever one is present."""
    real = "2026-07-20T00:00:00Z"
    result = {"InsertedDateUTC": real, "TimeOnRealtor": "1 month"}
    assert _list_date(result) == "2026-07-20"


def test_list_date_falls_back_to_age_string():
    derived = _list_date({"TimeOnRealtor": "5 days"})
    expected = (datetime.now(UTC).date() - timedelta(days=5)).isoformat()
    assert derived == expected


def test_list_date_absent():
    assert _list_date({}) is None


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("$725,000", 725_000),
    ("725000", 725_000),
    ("$1,250,000.00", 1_250_000),
    (None, None),
    ("", None),
    ("Contact agent", None),
])
def test_price_parsing(raw, expected):
    assert _price(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("3", 3.0), ("3 + 1", 4.0), ("2.5", 2.5), (None, None), ("n/a", None),
])
def test_room_parsing(raw, expected):
    assert _rooms(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1,650 sqft", 1650),
    ("1650", 1650),
    ("0.25 acres", 10890),
    (None, None),
    ("unknown", None),
])
def test_sqft_parsing(raw, expected):
    assert _sqft(raw) == expected


def test_sqft_converts_square_metres():
    assert _sqft("150 m2") == pytest.approx(1614, abs=2)


@pytest.mark.parametrize("text,street,city", [
    ("123 Main St SW, Calgary, Alberta T2P1A1", "123 Main St SW", "Calgary"),
    ("456 Elbow Dr NW, Calgary", "456 Elbow Dr NW", "Calgary"),
    ("789 Bow Cr SE|Unit 3", "789 Bow Cr SE", ""),
    ("", "", ""),
])
def test_address_splitting(text, street, city):
    got_street, got_city, _ = _split_address(text)
    assert got_street == street and got_city == city


def test_absolute_url():
    assert _absolute("/real-estate/1") == "https://www.realtor.ca/real-estate/1"
    assert _absolute("real-estate/1") == "https://www.realtor.ca/real-estate/1"
    assert _absolute("https://www.realtor.ca/x") == "https://www.realtor.ca/x"
    assert _absolute("") == ""


# ---------------------------------------------------------------------------
# Search URL
# ---------------------------------------------------------------------------

def test_search_url_carries_the_criteria(source):
    from homescout.models import SearchCriteria
    c = SearchCriteria(price_min=600_000, price_max=800_000, beds_min=3, baths_min=2)
    url = source._search_url(c)
    assert "PriceMin=600000" in url and "PriceMax=800000" in url
    assert "BedRange=3-0" in url and "BathRange=2-0" in url
