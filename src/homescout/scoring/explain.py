"""Explanations for why a listing ranked where it did.

Template-driven and grounded in the stored sub-scores, with no LLM required:
the tool must work without an API key, and a template quoting the actual numbers
cannot hallucinate a reason. An optional LLM pass can rewrite these into prose.

Explanations name what is missing rather than silently omitting the clause, so a
high rank resting on partial data is visible in the text itself.
"""

from __future__ import annotations

from homescout.scoring.scorer import Scored

# Human labels for the component names used in Scored.missing.
_MISSING_LABELS = {
    "price_per_sqft": "size unavailable, so price per sqft could not be computed",
    "assessment_ratio": "no City assessment on file for this address",
    "commute_min": "commute time unavailable",
    "walk_proxy": "no walkability data",
    "amenities": "no amenity data",
    "days_on_market": "listing date unknown",
}


def explain(row: dict, scored: Scored, rank: int, cohort_size: int) -> str:
    """Build the 'why it ranked' text for one listing."""
    parts = [f"Ranked #{rank} of {cohort_size} matches, scoring {scored.score_total:.0f}/100."]

    value = _value_clause(row, scored)
    if value:
        parts.append(value)

    location = _location_clause(row, scored)
    if location:
        parts.append(location)

    freshness = _freshness_clause(row, scored)
    if freshness:
        parts.append(freshness)

    gaps = _gaps_clause(scored)
    if gaps:
        parts.append(gaps)

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Clauses
# ---------------------------------------------------------------------------

def _value_clause(row: dict, s: Scored) -> str:
    if s.score_value is None:
        return ""

    bits = []

    ppsf = _ppsf(row)
    pct = s.percentiles.get("price_per_sqft")
    if ppsf is not None and pct is not None:
        bits.append(f"${ppsf:,.0f}/sqft, cheaper than {pct:.0f}% of matches")

    ratio = _float(row.get("assessment_ratio"))
    if ratio is not None:
        delta = (ratio - 1.0) * 100
        if delta <= -1:
            bits.append(f"listed {abs(delta):.0f}% below City assessed value")
        elif delta >= 1:
            bits.append(f"listed {delta:.0f}% above City assessed value")
        else:
            bits.append("listed essentially at City assessed value")

    if not bits:
        return f"Value {s.score_value:.0f}."
    return f"Value {s.score_value:.0f} — " + ", and ".join(bits) + "."


def _location_clause(row: dict, s: Scored) -> str:
    if s.score_location is None:
        return ""

    bits = []

    commute = _float(row.get("commute_min"))
    if commute is not None:
        estimated = " (estimated)" if row.get("commute_estimated") else ""
        bits.append(f"{commute:.0f} min to downtown{estimated}")

    amenities = _amenity_phrase(row)
    if amenities:
        bits.append(amenities)

    walk = _float(row.get("walk_proxy"))
    if walk is not None:
        bits.append(f"walk proxy {walk:.0f}/100")

    if not bits:
        return f"Location {s.score_location:.0f}."
    return f"Location {s.score_location:.0f} — " + ", ".join(bits) + "."


def _freshness_clause(row: dict, s: Scored) -> str:
    if s.score_freshness is None:
        return ""

    days = _float(row.get("days_on_market"))
    if days is None:
        return f"Freshness {s.score_freshness:.0f}."
    if days <= 0:
        return f"Freshness {s.score_freshness:.0f} — listed today."
    if days == 1:
        return f"Freshness {s.score_freshness:.0f} — listed yesterday."
    return f"Freshness {s.score_freshness:.0f} — listed {days:.0f} days ago."


def _gaps_clause(s: Scored) -> str:
    """Name missing inputs, so a rank built on partial data is visible."""
    if not s.missing:
        return ""
    labels = [_MISSING_LABELS.get(m, m) for m in s.missing]
    # De-duplicate while preserving order.
    seen, unique = set(), []
    for label in labels:
        if label not in seen:
            seen.add(label)
            unique.append(label)
    return f"Scored on {s.data_completeness:.0f}% of available signals — " + "; ".join(unique) + "."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _amenity_phrase(row: dict) -> str:
    pairs = [("school", "schools_n"), ("cafe", "cafes_n"), ("park", "parks_n")]
    bits = []
    for label, key in pairs:
        n = _float(row.get(key))
        if n:
            bits.append(f"{n:.0f} {label}{'s' if n != 1 else ''}")
    if not bits:
        return ""
    return ", ".join(bits) + " within 1 km"


def _ppsf(row: dict) -> float | None:
    price, sqft = _float(row.get("price")), _float(row.get("sqft"))
    if not price or not sqft or sqft <= 0:
        return None
    return price / sqft


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
