"""Offline sample listings.

Used by the test suite and by `homescout run scrape --source fixtures`, so the
whole pipeline can be developed and verified without touching a listing portal.

Built from REAL Calgary parcel records (see `_parcels.py`) — real addresses,
real coordinates, real City assessed values. Synthetic addresses would never
match the assessment join, so the fixture path would silently skip the very
code it is supposed to exercise.

The set is deterministic (fixed seed) and deliberately includes the edge cases
that break naive implementations: missing sqft, listings outside the price and
distance bounds, stale listings, duplicates, and unparseable dates.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from homescout.discovery._parcels import PARCELS
from homescout.models import Listing, SearchCriteria

_TYPES = ["House", "House", "House", "Townhouse", "Duplex", "Condo"]


class FixtureSource:
    """Deterministic offline listing source built from real parcel data."""

    name = "fixtures"

    def __init__(self, count: int | None = None, seed: int = 20260728) -> None:
        self.count = count or len(PARCELS)
        self.seed = seed

    def fetch(self, criteria: SearchCriteria) -> list[Listing]:
        rng = random.Random(self.seed)
        today = datetime.now(UTC).date()
        listings: list[Listing] = []

        for i, (address, assessed, lat, lon) in enumerate(PARCELS[: self.count]):
            # List price is derived from the real assessed value, so
            # assessment_ratio spreads realistically on both sides of 1.0.
            factor = rng.uniform(0.86, 1.18)
            price = int(assessed * factor // 5_000 * 5_000)

            sqft = rng.choice([950, 1100, 1250, 1400, 1550, 1700, 1850, 2000, 2200, 2450])
            age_days = rng.choice([0, 1, 2, 3, 4, 6, 8, 11, 14, 17, 21, 25, 29, 34, 55, 90])
            list_date = today - timedelta(days=age_days)

            listings.append(Listing(
                mls_id=f"FIX{i:04d}",
                source=self.name,
                url=f"https://example.invalid/listing/FIX{i:04d}",
                address=address,
                city="Calgary",
                postal_code="",
                price=price,
                beds=float(rng.choice([2, 3, 3, 3, 4, 4, 5])),
                baths=float(rng.choice([1, 2, 2, 2, 3, 3, 4])),
                sqft=sqft,
                lot_sqft=rng.choice([None, 3000, 4200, 5100, 6000]),
                property_type=rng.choice(_TYPES),
                list_date=list_date.isoformat(),
                days_on_market=age_days,
                latitude=lat,
                longitude=lon,
            ))

        _apply_edge_cases(listings)
        return listings


def _apply_edge_cases(listings: list[Listing]) -> None:
    """Introduce the specific gaps the pipeline must survive.

    Every one of these has a counterpart assertion in the test suite — they are
    the cases that make percentile scoring rank by data completeness if the
    missing-data policy is not implemented correctly.
    """
    if len(listings) < 30:
        return

    # Missing sqft — price_per_sqft undefined, so Value loses one component.
    for i in (2, 9, 17, 26):
        listings[i].sqft = None

    # Missing coordinates — must be dropped by the quality validator.
    listings[4].latitude = None
    listings[4].longitude = None

    # Missing / zero price — must be dropped.
    listings[11].price = None
    listings[19].price = 0

    # Implausible sqft — must be dropped as a data error.
    listings[13].sqft = 45
    listings[15].sqft = 99_000

    # Duplicate of listing 0 under a different MLS id — must be deduped.
    dup = listings[0]
    listings[7].address = dup.address
    listings[7].price = dup.price
    listings[7].latitude = dup.latitude
    listings[7].longitude = dup.longitude

    # Unparseable list_date — days_on_market must survive as None or derived.
    listings[21].list_date = "not-a-date"

    # Date and day-count disagree; the date is authoritative.
    listings[23].list_date = (datetime.now(UTC).date() - timedelta(days=3)).isoformat()
    listings[23].days_on_market = 400

    # An address the assessment join cannot match, to prove exact-match-only
    # yields None rather than a wrong-but-confident number.
    listings[29].address = "99999 NONEXISTENT PARCEL WY SW"
