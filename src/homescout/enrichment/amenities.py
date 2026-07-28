"""Nearby amenities from OpenStreetMap via the Overpass API.

Fetched as a **POI index for the whole cohort's bounding box**, then counted
locally per listing. The obvious implementation — one Overpass query per
listing — is both far slower and much ruder to a free public service: 36
listings meant 36 rate-limited round trips, and the combined per-listing query
was heavy enough that public instances returned 504.

One query per category across the whole bbox returns in a few seconds and
serves every listing, so a run makes 5 requests instead of N.
"""

from __future__ import annotations

import logging

from homescout.database import cache_get_amenities, geo_key
from homescout.enrichment.geo import haversine_km, http_post, service_config

log = logging.getLogger(__name__)

# Category -> the OSM tag filters that define it. Kept in one place so
# walkability.py and the report always agree on what each count means.
CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "schools":   [("amenity", "^(school|kindergarten)$")],
    "cafes":     [("amenity", "^(cafe|restaurant)$")],
    "parks":     [("leisure", "^(park|playground)$")],
    "groceries": [("shop", "^(supermarket|convenience)$")],
    "transit":   [("public_transport", "^(platform|station)$"), ("railway", "^station$")],
}

EMPTY = {k: 0 for k in CATEGORIES}

# Padding around the listing extremes, so POIs just outside the cohort's
# bounding box still count toward listings on its edge.
BBOX_PAD_DEG = 0.02


def build_category_query(category: str, bbox: tuple[float, float, float, float], timeout: int | None = None) -> str:
    """Overpass QL for one category across a bounding box.

    Only nodes and ways are requested — relations add cost for POI types that
    are rarely mapped as relations.
    """
    if timeout is None:
        timeout = int(service_config("overpass").get("query_timeout_s", 90))
    south, west, north, east = bbox
    area = f"({south},{west},{north},{east})"
    parts = []
    for key, pattern in CATEGORIES[category]:
        parts.append(f'node["{key}"~"{pattern}"]{area};')
        parts.append(f'way["{key}"~"{pattern}"]{area};')
    return f"[out:json][timeout:{timeout}];({''.join(parts)});out center tags;"


def cohort_bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    """Bounding box covering every listing, with padding."""
    if not points:
        return None
    lats = [p[0] for p in points if p[0] is not None]
    lons = [p[1] for p in points if p[1] is not None]
    if not lats or not lons:
        return None
    return (
        min(lats) - BBOX_PAD_DEG,
        min(lons) - BBOX_PAD_DEG,
        max(lats) + BBOX_PAD_DEG,
        max(lons) + BBOX_PAD_DEG,
    )


def bbox_key(bbox: tuple[float, float, float, float]) -> str:
    """Cache key for a bounding box, rounded so near-identical cohorts share one."""
    return "bbox:" + ",".join(f"{v:.2f}" for v in bbox)


def fetch_poi_index(
    bbox: tuple[float, float, float, float],
    conn=None,
) -> dict[str, list[tuple[float, float]]] | None:
    """Fetch every category's POI coordinates for the bbox, cached in SQLite.

    Returns None only if *every* category failed — a partial index is still
    useful, and categories that fail are simply absent from the result. Any
    partial index is cached too, and missing categories are refetched next run.
    """
    key = bbox_key(bbox)

    if conn is not None:
        cached = cache_get_amenities(conn, key)
        if cached:
            index = {k: [tuple(p) for p in v] for k, v in cached.items()}
            if len(index) == len(CATEGORIES):
                log.debug("POI index served from cache (%s)", key)
                return index
            # Partial cache: keep what we have and try to fill the rest.
            log.debug("Partial POI index cached; refetching missing categories")
            return _fill(index, bbox, conn, key)

    return _fill({}, bbox, conn, key)


def _fill(index, bbox, conn, key):
    """Fetch any categories not already present, then persist the result."""
    cfg = service_config("overpass")
    endpoints = _endpoints(cfg)
    fetched = False

    for category in CATEGORIES:
        if category in index:
            continue
        coords = _fetch_category(category, bbox, endpoints)
        if coords is not None:
            index[category] = coords
            fetched = True
            log.debug("Overpass: %s POIs for %s", len(coords), category)

    if not index:
        log.warning("Every Overpass category failed; amenity data unavailable")
        return None

    missing = [c for c in CATEGORIES if c not in index]
    if missing:
        log.warning("Amenity categories unavailable: %s", ", ".join(missing))

    if conn is not None and fetched:
        from homescout.database import cache_put_amenities
        cache_put_amenities(conn, key, {k: [list(p) for p in v] for k, v in index.items()})
        conn.commit()

    return index


def _fetch_category(category, bbox, endpoints) -> list[tuple[float, float]] | None:
    """Try every endpoint, then retry the whole rotation.

    Public Overpass instances return 504 or stall when their slots are busy;
    that is usually transient, so a second pass typically succeeds where the
    first did not.
    """
    query = build_category_query(category, bbox)
    attempts = int(service_config("overpass").get("retries", 2))

    for attempt in range(1, attempts + 1):
        for url in endpoints:
            response = http_post("overpass", url, data={"data": query})
            if response is None:
                continue
            try:
                elements = response.json().get("elements", [])
            except ValueError:
                log.warning("Overpass returned non-JSON from %s for %s", url, category)
                continue
            return _coords(elements)
        if attempt < attempts:
            log.debug("Overpass attempt %d failed for %s; retrying", attempt, category)

    return None


def _coords(elements: list[dict]) -> list[tuple[float, float]]:
    """Extract coordinates. Nodes carry lat/lon; ways carry a `center`."""
    out = []
    for element in elements:
        lat, lon = element.get("lat"), element.get("lon")
        if lat is None or lon is None:
            center = element.get("center") or {}
            lat, lon = center.get("lat"), center.get("lon")
        if lat is not None and lon is not None:
            out.append((float(lat), float(lon)))
    return out


def count_near(
    index: dict[str, list[tuple[float, float]]] | None,
    lat: float,
    lon: float,
    radius_m: int | None = None,
) -> dict[str, int] | None:
    """Count POIs of each category within the radius of one listing.

    Returns None when no index exists, so the scorer reweights around the gap
    rather than treating the listing as genuinely amenity-free.
    """
    if not index:
        return None

    if radius_m is None:
        radius_m = int(service_config("overpass").get("radius_m", 1000))
    radius_km = radius_m / 1000.0

    counts = {}
    for category in CATEGORIES:
        points = index.get(category)
        if points is None:
            continue
        counts[category] = sum(
            1 for plat, plon in points if haversine_km(lat, lon, plat, plon) <= radius_km
        )

    return counts or None


def nearby(lat: float, lon: float, conn=None) -> tuple[dict[str, int] | None, bool]:
    """Single-listing lookup, kept for direct use and tests.

    The pipeline uses fetch_poi_index + count_near instead, which serves the
    whole cohort from one set of requests.
    """
    key = geo_key(lat, lon)
    if conn is not None:
        cached = cache_get_amenities(conn, key)
        if cached is not None:
            return {k: int(cached.get(k, 0)) for k in CATEGORIES}, True

    pad = BBOX_PAD_DEG
    index = fetch_poi_index((lat - pad, lon - pad, lat + pad, lon + pad))
    return count_near(index, lat, lon), False


def _endpoints(cfg: dict) -> list[str]:
    primary = cfg.get("base_url", "https://overpass-api.de/api/interpreter")
    mirrors = cfg.get("mirrors") or []
    seen, out = set(), []
    for url in [primary, *mirrors]:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out
