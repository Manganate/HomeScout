"""Commute time to the CBD (Phase B of the distance handling).

Real routing via OSRM's public demo server, computed only for listings that
survived the Phase-A haversine bound, and cached by rounded coordinate so
re-runs and near-neighbours cost nothing.

If OSRM is unreachable, falls back to a distance-derived estimate rather than
failing the run — but flags it, so the report never presents an estimate as a
measured value.
"""

from __future__ import annotations

import logging

from homescout.database import cache_get_commute, geo_key
from homescout.enrichment.geo import haversine_km, http_get, service_config
from homescout.models import SearchCriteria

log = logging.getLogger(__name__)


def commute_minutes(
    lat: float,
    lon: float,
    criteria: SearchCriteria,
    conn=None,
) -> tuple[float | None, bool, bool]:
    """Return (minutes, was_estimated, came_from_cache).

    Never writes to the cache: callers running inside a worker pool queue the
    result and persist it on a single thread, because concurrent SQLite writes
    contend for the one available writer slot.
    """
    mode = criteria.commute_mode
    key = geo_key(lat, lon)

    if conn is not None:
        cached = cache_get_commute(conn, key, mode)
        if cached is not None:
            minutes, estimated = cached
            return minutes, estimated, True

    minutes, estimated = _fetch(lat, lon, criteria)
    return minutes, estimated, False


def _fetch(lat: float, lon: float, criteria: SearchCriteria) -> tuple[float | None, bool]:
    cfg = service_config("osrm")
    base = cfg.get("base_url", "https://router.project-osrm.org")

    # OSRM has no transit profile on the demo server; transit requests use the
    # driving route with a slower fallback speed rather than silently returning
    # a driving time labelled as transit.
    if criteria.commute_mode == "driving":
        url = f"{base}/route/v1/driving/{lon},{lat};{criteria.cbd_lon},{criteria.cbd_lat}"
        response = http_get("osrm", url, params={"overview": "false", "alternatives": "false"})
        if response is not None:
            try:
                payload = response.json()
                if payload.get("code") == "Ok" and payload.get("routes"):
                    seconds = float(payload["routes"][0]["duration"])
                    return round(seconds / 60.0, 1), False
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                log.warning("Could not parse OSRM response: %s", exc)

    return _estimate(lat, lon, criteria), True


def _estimate(lat: float, lon: float, criteria: SearchCriteria) -> float:
    """Distance-derived fallback when routing is unavailable."""
    cfg = service_config("osrm")
    kmh = float(cfg.get("fallback_kmh", 42.0))
    if criteria.commute_mode == "transit":
        kmh *= 0.55  # transit is materially slower than driving door-to-door
    distance = haversine_km(lat, lon, criteria.cbd_lat, criteria.cbd_lon)
    # Straight-line understates real travel; 1.3 is a standard detour factor.
    return round((distance * 1.3) / kmh * 60.0, 1)
