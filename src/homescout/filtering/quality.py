"""Data-quality validation and deduplication.

Runs before criteria filtering: a record that fails validation can't be
meaningfully compared against anything.
"""

from __future__ import annotations

import logging
import re

from homescout.models import Listing, days_since

log = logging.getLogger(__name__)

# Anything outside this is a data error, not a real home.
SQFT_MIN_PLAUSIBLE = 200
SQFT_MAX_PLAUSIBLE = 20_000

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[.,#]")

# Street-type and direction abbreviations, so the same address written two ways
# collapses to one key. Used for dedupe and for the parcel-assessment join.
# Calgary's own street-type abbreviations, taken from the City parcel dataset
# rather than assumed — the City uses AV (not AVE), BV (not BLVD), CO for Court,
# GR for Green, and GV for Grove. Getting these wrong silently drops the
# assessment join for whole street types.
_STREET_TYPES = {
    "avenue": "av", "ave": "av",
    "bay": "ba",
    "boulevard": "bv", "blvd": "bv",
    "cape": "ca",
    "circle": "ci",
    "close": "cl",
    "common": "cm", "commons": "cm",
    "court": "co", "crt": "co", "ct": "co",
    "cove": "cv",
    "crescent": "cr", "cres": "cr",
    "drive": "dr",
    "gardens": "gd", "garden": "gd",
    "gate": "ga",
    "green": "gr",
    "grove": "gv",
    "heath": "he",
    "heights": "ht",
    "hill": "hl",
    "island": "is",
    "landing": "ld",
    "lane": "ln",
    "link": "li",
    "manor": "mr",
    "mews": "me",
    "mount": "mt",
    "parade": "pa",
    "park": "pk",
    "place": "pl",
    "point": "pt",
    "rise": "ri",
    "road": "rd",
    "square": "sq",
    "street": "st",
    "terrace": "tc",
    "trail": "tr",
    "view": "vw",
    "villas": "vi", "villa": "vi",
    "way": "wy",
}
_QUADRANTS = {"southwest": "sw", "northwest": "nw", "southeast": "se", "northeast": "ne"}
_QUADRANT_ABBREV = {"sw", "nw", "se", "ne"}


def normalize_address(address: str) -> str:
    """Collapse an address to a comparable key.

    Both sides of the assessment join run through this, and the join accepts
    only exact matches on the result — a mismatched parcel is worse than a
    missing one, because it yields a confident wrong number.

    Only the *street-type position* is abbreviated, never every word. Calgary
    street names legitimately contain words that are also street types:
    "175 DEER LANE RD SE" has the name "DEER LANE" and the type "RD", and
    abbreviating every match would corrupt it to "DEER LN RD". Calgary
    addresses are `<number> <name> <type> <quadrant>`, so the type is the token
    before the quadrant, or the last token when no quadrant is present.
    """
    if not address:
        return ""
    text = _PUNCT.sub(" ", str(address).lower())
    text = _WS.sub(" ", text).strip()
    if not text:
        return ""

    words = text.split()

    # Punctuation stripping splits "S.W." into two tokens; rejoin it.
    if len(words) >= 2 and words[-2] in ("s", "n") and words[-1] in ("w", "e"):
        words[-2:] = [words[-2] + words[-1]]

    # Strip a leading unit designator ("unit 5 123 main st sw", "#5-123 ...").
    if words and words[0] in ("unit", "apt", "suite", "ste"):
        words = words[2:] if len(words) > 2 else words

    if not words:
        return ""

    # Normalize the quadrant if the last token is one.
    tail = len(words) - 1
    last = words[tail]
    if last in _QUADRANTS:
        words[tail] = _QUADRANTS[last]
        tail -= 1
    elif last in _QUADRANT_ABBREV:
        tail -= 1

    # Abbreviate only the street-type token, immediately before the quadrant.
    if tail >= 1:
        words[tail] = _STREET_TYPES.get(words[tail], words[tail])

    return " ".join(words).upper()


def validate(listing: Listing) -> str | None:
    """Return a rejection reason, or None if the listing is usable."""
    if not listing.price or listing.price <= 0:
        return "missing or zero price"

    if listing.latitude is None or listing.longitude is None:
        return "missing coordinates"

    if not (-90 <= listing.latitude <= 90) or not (-180 <= listing.longitude <= 180):
        return "coordinates out of range"

    # sqft is optional — a listing without it is still rankable on location and
    # freshness, and the scorer reweights around the gap. But an implausible
    # value is a data error and would poison price-per-sqft percentiles.
    if listing.sqft is not None and not (SQFT_MIN_PLAUSIBLE <= listing.sqft <= SQFT_MAX_PLAUSIBLE):
        return f"implausible sqft ({listing.sqft})"

    if not listing.address or not listing.address.strip():
        return "missing address"

    return None


def reconcile_age(listing: Listing) -> None:
    """Prefer a real list_date over a provider-reported day count.

    Portals often report age as a coarse formatted string ("3 days", "1 month").
    When an actual date is present it is authoritative, because the freshness
    score and the age cut both depend on day-level resolution.
    """
    derived = days_since(listing.list_date)
    if derived is not None:
        listing.days_on_market = derived


def dedupe(listings: list[Listing]) -> tuple[list[Listing], int]:
    """Drop duplicates: first by mls_id, then by normalized address + price.

    Returns (kept, n_dropped). The first occurrence wins.
    """
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, int]] = set()
    kept: list[Listing] = []
    dropped = 0

    for listing in listings:
        if listing.mls_id in seen_ids:
            dropped += 1
            continue

        key = (normalize_address(listing.address), listing.price or 0)
        if key[0] and key in seen_keys:
            log.debug("Dropping duplicate of %s: %s", key, listing.mls_id)
            dropped += 1
            continue

        seen_ids.add(listing.mls_id)
        if key[0]:
            seen_keys.add(key)
        kept.append(listing)

    return kept, dropped
