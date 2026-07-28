"""Shared geo utilities, HTTP client, and the rate limiter.

Every enrichment service is a free public endpoint with a hard limit — OSRM's
demo server and Nominatim both cap at 1 request/second and are non-commercial.
Exceeding those gets you banned, not throttled, so all outbound calls funnel
through a token bucket that is shared across the analyze worker pool.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from homescout.config import load_sources_config
from homescout.filtering.criteria import haversine_km  # re-exported for convenience

log = logging.getLogger(__name__)

__all__ = ["RateLimiter", "geocode", "get_limiter", "haversine_km", "http_get", "user_agent"]

_CONFIG = load_sources_config()
_SERVICES = _CONFIG.get("services", {}) or {}
_HTTP = _CONFIG.get("http", {}) or {}


def user_agent() -> str:
    """Nominatim's usage policy requires a descriptive User-Agent."""
    return _HTTP.get("user_agent", "HomeScout/0.1 (personal real-estate search tool)")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Thread-safe token bucket enforcing a minimum interval between calls.

    Serializes across every worker thread, which is the point: the pool fans
    out across listings, but the shared upstream limit is what actually paces
    the work.
    """

    def __init__(self, rate_per_sec: float) -> None:
        self.min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> float:
        """Block until a request may proceed. Returns the seconds waited."""
        if self.min_interval <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.min_interval
        if wait > 0:
            time.sleep(wait)
        return wait


_limiters: dict[str, RateLimiter] = {}
_limiters_lock = threading.Lock()


def get_limiter(service: str) -> RateLimiter:
    """Return the process-wide limiter for a named service."""
    with _limiters_lock:
        if service not in _limiters:
            rate = float(_SERVICES.get(service, {}).get("rate_limit_per_sec", 1.0))
            _limiters[service] = RateLimiter(rate)
        return _limiters[service]


def service_config(service: str) -> dict:
    return _SERVICES.get(service, {}) or {}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get(service: str, url: str, params: dict | None = None, timeout: float | None = None) -> httpx.Response | None:
    """Rate-limited GET. Returns None on any failure — enrichment is best-effort
    and a single unreachable service must never abort the run."""
    limiter = get_limiter(service)
    cfg = service_config(service)
    timeout = timeout or float(cfg.get("timeout_s", 15))

    limiter.acquire()
    try:
        response = httpx.get(
            url,
            params=params,
            timeout=timeout,
            headers={"User-Agent": user_agent()},
            follow_redirects=True,
        )
        if response.status_code != 200:
            log.warning("%s returned HTTP %s for %s", service, response.status_code, url)
            return None
        return response
    except httpx.HTTPError as exc:
        log.warning("%s request failed: %s", service, exc)
        return None


def http_post(service: str, url: str, data: dict | str, timeout: float | None = None) -> httpx.Response | None:
    """Rate-limited POST, used by Overpass whose queries exceed URL length limits."""
    limiter = get_limiter(service)
    cfg = service_config(service)
    timeout = timeout or float(cfg.get("timeout_s", 45))

    limiter.acquire()
    try:
        response = httpx.post(
            url,
            data=data,
            timeout=timeout,
            headers={"User-Agent": user_agent()},
        )
        if response.status_code != 200:
            log.warning("%s returned HTTP %s", service, response.status_code)
            return None
        return response
    except httpx.HTTPError as exc:
        log.warning("%s request failed: %s", service, exc)
        return None


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode(query: str, conn=None) -> tuple[float, float] | None:
    """Resolve a place name to coordinates via Nominatim, cached in SQLite.

    Only needed for the CBD anchor when a user supplies a place name instead of
    coordinates; listings arrive with their own lat/lon.
    """
    if not query or not query.strip():
        return None
    query = query.strip()

    if conn is not None:
        from homescout.database import cache_get_geocode
        cached = cache_get_geocode(conn, query)
        if cached:
            return cached

    cfg = service_config("nominatim")
    base = cfg.get("base_url", "https://nominatim.openstreetmap.org")
    response = http_get("nominatim", f"{base}/search", params={"q": query, "format": "json", "limit": 1})
    if response is None:
        return None

    try:
        results = response.json()
    except ValueError:
        return None
    if not results:
        return None

    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])

    if conn is not None:
        from homescout.database import cache_put_geocode
        cache_put_geocode(conn, query, lat, lon)
        conn.commit()

    return lat, lon
