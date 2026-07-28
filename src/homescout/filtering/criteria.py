"""Hard-criteria filtering, including Phase A of the distance handling.

Distance is handled in three phases across the pipeline, because the user's
criterion is *minutes* but the cheap signal is *kilometres*:

  Phase A (here)     haversine bound, deliberately generous
  Phase B (analyze)  real routing, only for Phase-A survivors
  Phase C (analyze)  the actual minutes cut

Computing real drive time for every scraped listing would mean thousands of
requests against a 1 req/s public endpoint, so Phase A exists purely to discard
listings that *cannot* qualify.
"""

from __future__ import annotations

import logging
from math import asin, cos, radians, sin, sqrt

from homescout.models import Listing, SearchCriteria

log = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = radians(lat1), radians(lat2)
    dphi = p2 - p1
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def check(listing: Listing, criteria: SearchCriteria) -> str | None:
    """Return a rejection reason, or None if the listing passes every criterion.

    Assumes the listing has already passed quality validation.
    """
    if listing.price is not None:
        if listing.price < criteria.price_min:
            return f"price ${listing.price:,} below minimum"
        if listing.price > criteria.price_max:
            return f"price ${listing.price:,} above maximum"

    if listing.beds is not None:
        if listing.beds < criteria.beds_min:
            return f"{listing.beds:g} beds below minimum {criteria.beds_min}"
        if criteria.beds_max is not None and listing.beds > criteria.beds_max:
            return f"{listing.beds:g} beds above maximum {criteria.beds_max}"

    if listing.baths is not None:
        if listing.baths < criteria.baths_min:
            return f"{listing.baths:g} baths below minimum {criteria.baths_min}"
        if criteria.baths_max is not None and listing.baths > criteria.baths_max:
            return f"{listing.baths:g} baths above maximum {criteria.baths_max}"

    if listing.sqft is not None:
        if criteria.sqft_min is not None and listing.sqft < criteria.sqft_min:
            return f"{listing.sqft} sqft below minimum"
        if criteria.sqft_max is not None and listing.sqft > criteria.sqft_max:
            return f"{listing.sqft} sqft above maximum"

    # Age. A listing with no usable date is kept rather than dropped — it is
    # still rankable on value and location, and the scorer handles the gap.
    if listing.days_on_market is not None and listing.days_on_market > criteria.max_listing_age_days:
        return f"listed {listing.days_on_market}d ago, over {criteria.max_listing_age_days}d limit"

    if criteria.property_types and listing.property_type:
        allowed = {t.strip().lower() for t in criteria.property_types}
        if listing.property_type.strip().lower() not in allowed:
            return f"property type {listing.property_type!r} not in allowed types"

    # Phase A: generous straight-line bound.
    if listing.latitude is not None and listing.longitude is not None:
        distance = haversine_km(listing.latitude, listing.longitude, criteria.cbd_lat, criteria.cbd_lon)
        listing.distance_km = round(distance, 3)
        if distance > criteria.geo_bound_km:
            return f"{distance:.1f} km from CBD, beyond {criteria.geo_bound_km:.1f} km pre-filter bound"

    return None


def apply(listings: list[Listing], criteria: SearchCriteria) -> tuple[list[Listing], list[tuple[Listing, str]]]:
    """Split listings into (passed, [(rejected, reason), ...])."""
    passed: list[Listing] = []
    rejected: list[tuple[Listing, str]] = []

    for listing in listings:
        reason = check(listing, criteria)
        if reason:
            rejected.append((listing, reason))
        else:
            passed.append(listing)

    return passed, rejected
