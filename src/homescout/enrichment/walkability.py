"""Walkability proxy derived from OpenStreetMap POI density.

This is NOT Walk Score(R) — that is a trademarked, key-gated commercial product
with a different methodology. This is a transparent local proxy, and the whole
formula lives in this one function so it can be audited and tuned.

Method: each category contributes a saturating share of the total. Saturation
matters — the tenth cafe within a kilometre adds far less to walkability than
the first, so a raw count would let one restaurant strip dominate the score.
"""

from __future__ import annotations

# Category weights (sum to 1.0) and the count at which each category is
# considered fully served.
_WEIGHTS: dict[str, tuple[float, int]] = {
    #                weight  saturation count
    "groceries":    (0.25,   3),
    "cafes":        (0.25,   12),
    "transit":      (0.20,   8),
    "parks":        (0.15,   5),
    "schools":      (0.15,   3),
}

LABEL = "Walk proxy"


def score(counts: dict[str, int] | None) -> float | None:
    """Return a 0-100 walkability proxy, or None if no amenity data exists.

    None and 0.0 mean different things: None is "we never got amenity data for
    this listing" (so the scorer reweights around it), while 0.0 is "we looked
    and found nothing walkable".
    """
    if counts is None:
        return None

    total = 0.0
    for category, (weight, saturation) in _WEIGHTS.items():
        n = counts.get(category)
        if n is None:
            n = 0
        # Saturating curve: reaches ~1.0 at the saturation count, never exceeds it.
        share = min(1.0, n / saturation) if saturation > 0 else 0.0
        total += weight * share

    return round(total * 100.0, 1)


def describe(counts: dict[str, int] | None) -> str:
    """Short human phrase for the explanation text."""
    if not counts:
        return "no amenity data"

    parts = []
    for label, key in (("school", "schools"), ("cafe", "cafes"), ("park", "parks"), ("grocer", "groceries")):
        n = counts.get(key, 0)
        if n:
            parts.append(f"{n} {label}{'s' if n != 1 else ''}")

    if not parts:
        return "no amenities within 1 km"
    return ", ".join(parts[:3]) + " within 1 km"
