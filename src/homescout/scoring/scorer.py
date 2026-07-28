"""Percentile-based scoring with an explicit missing-data policy.

Two decisions drive this module:

1. **Percentile rank, not min-max.** One outlier listing must not compress the
   whole cohort into a narrow band, and percentiles state cleanly in prose
   ("cheaper per sqft than 82% of matches").

2. **Per-component reweighting.** Every input can be null: sqft is often absent,
   and the parcel-assessment join legitimately misses condos. Percentile-ranking
   a partially-null column would silently rank the cohort by *which listings had
   complete data*. Instead each sub-score is computed from whichever components
   are present, with weights renormalized across only those, and the resulting
   `data_completeness` is stored and displayed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homescout.models import SearchCriteria

log = logging.getLogger(__name__)


# Component weights within each sub-score. Renormalized per listing over
# whichever components are actually available.
VALUE_COMPONENTS = {"price_per_sqft": 0.55, "assessment_ratio": 0.45}
LOCATION_COMPONENTS = {"commute_min": 0.55, "walk_proxy": 0.30, "amenities": 0.15}
FRESHNESS_COMPONENTS = {"days_on_market": 1.0}


@dataclass
class Scored:
    """Scoring result for one listing."""

    mls_id: str
    score_value: float | None
    score_location: float | None
    score_freshness: float | None
    score_total: float
    data_completeness: float
    # Percentiles retained so explanations quote the same numbers that were scored.
    percentiles: dict[str, float]
    missing: list[str]


# ---------------------------------------------------------------------------
# Percentile ranking
# ---------------------------------------------------------------------------

def percentile_ranks(values: list[float | None]) -> list[float | None]:
    """Map each value to its percentile rank in [0, 100] among non-null values.

    Uses the midpoint of tied ranks, so identical values score identically.
    Nulls stay null — they are excluded from the cohort, never imputed.
    """
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    out: list[float | None] = [None] * len(values)

    n = len(present)
    if n == 0:
        return out
    if n == 1:
        out[present[0][0]] = 50.0
        return out

    ordered = sorted(present, key=lambda pair: pair[1])

    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        # Midpoint rank for the tie group, scaled to 0-100.
        mean_rank = (i + j) / 2.0
        pct = (mean_rank / (n - 1)) * 100.0
        for k in range(i, j + 1):
            out[ordered[k][0]] = round(pct, 2)
        i = j + 1

    return out


def _invert(pct: float | None) -> float | None:
    """Lower raw value is better (cheaper, closer, newer)."""
    return None if pct is None else round(100.0 - pct, 2)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_cohort(rows: list[dict], criteria: SearchCriteria) -> list[Scored]:
    """Score every listing relative to the others in the cohort."""
    if not rows:
        return []

    # -- Raw columns --
    ppsf = [_ratio(r.get("price"), r.get("sqft")) for r in rows]
    aratio = [_float(r.get("assessment_ratio")) for r in rows]
    commute = [_float(r.get("commute_min")) for r in rows]
    walk = [_float(r.get("walk_proxy")) for r in rows]
    amenity = [_amenity_total(r) for r in rows]
    age = [_float(r.get("days_on_market")) for r in rows]

    # -- Percentiles, oriented so higher is always better --
    p_ppsf = [_invert(p) for p in percentile_ranks(ppsf)]
    p_aratio = [_invert(p) for p in percentile_ranks(aratio)]
    p_commute = [_invert(p) for p in percentile_ranks(commute)]
    p_walk = percentile_ranks(walk)
    p_amenity = percentile_ranks(amenity)
    p_age = [_invert(p) for p in percentile_ranks(age)]

    results: list[Scored] = []

    for i, row in enumerate(rows):
        missing: list[str] = []

        value, v_cov = _combine(
            {"price_per_sqft": p_ppsf[i], "assessment_ratio": p_aratio[i]},
            VALUE_COMPONENTS,
            missing,
        )
        location, l_cov = _combine(
            {"commute_min": p_commute[i], "walk_proxy": p_walk[i], "amenities": p_amenity[i]},
            LOCATION_COMPONENTS,
            missing,
        )
        freshness, f_cov = _combine(
            {"days_on_market": p_age[i]},
            FRESHNESS_COMPONENTS,
            missing,
        )

        # Composite: renormalize the user's weights across the sub-scores that
        # exist, so a listing missing Value entirely is not penalized to zero —
        # it is ranked on what is actually known about it.
        parts = {
            "value": (value, criteria.weight_value),
            "location": (location, criteria.weight_location),
            "freshness": (freshness, criteria.weight_freshness),
        }
        available = {k: (s, w) for k, (s, w) in parts.items() if s is not None}
        weight_sum = sum(w for _, w in available.values())

        if weight_sum > 0:
            total = sum(s * w for s, w in available.values()) / weight_sum
        else:
            total = 0.0

        completeness = round((v_cov + l_cov + f_cov) / 3.0 * 100, 1)

        results.append(Scored(
            mls_id=row["mls_id"],
            score_value=_round(value),
            score_location=_round(location),
            score_freshness=_round(freshness),
            score_total=round(total, 2),
            data_completeness=completeness,
            percentiles={
                "price_per_sqft": p_ppsf[i],
                "assessment_ratio": p_aratio[i],
                "commute_min": p_commute[i],
                "walk_proxy": p_walk[i],
                "amenities": p_amenity[i],
                "days_on_market": p_age[i],
            },
            missing=missing,
        ))

    return results


def _combine(
    components: dict[str, float | None],
    weights: dict[str, float],
    missing: list[str],
) -> tuple[float | None, float]:
    """Weighted mean over available components only.

    Returns (score, coverage) where coverage is the fraction of this sub-score's
    total weight that was actually available. Returns (None, 0.0) when nothing
    is available — the listing is then excluded from this sub-score rather than
    being scored as zero, which would be indistinguishable from "genuinely bad".
    """
    total = 0.0
    used_weight = 0.0

    for name, weight in weights.items():
        value = components.get(name)
        if value is None:
            missing.append(name)
            continue
        total += value * weight
        used_weight += weight

    all_weight = sum(weights.values())
    if used_weight <= 0:
        return None, 0.0

    return total / used_weight, used_weight / all_weight if all_weight else 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ratio(price, sqft) -> float | None:
    price, sqft = _float(price), _float(sqft)
    if not price or not sqft or sqft <= 0:
        return None
    return price / sqft


def _amenity_total(row: dict) -> float | None:
    """Combined amenity count. None when no amenity lookup succeeded at all."""
    keys = ("schools_n", "cafes_n", "parks_n", "groceries_n", "transit_n")
    values = [_float(row.get(k)) for k in keys]
    if all(v is None for v in values):
        return None
    return float(sum(v for v in values if v is not None))


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)
