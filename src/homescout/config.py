"""Paths, environment, and search-config loading.

All user state lives under ~/.homescout/ so the repo stays clean and a fresh
clone on another machine only needs `homescout init`. Paths resolve via
Path.home() and Path(__file__) — never hardcoded.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from homescout.models import SearchCriteria

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APP_DIR = Path(os.environ.get("HOMESCOUT_HOME", Path.home() / ".homescout"))

DB_PATH = APP_DIR / "homescout.db"
ENV_PATH = APP_DIR / ".env"
SEARCH_CONFIG_PATH = APP_DIR / "search.yaml"
RAW_DIR = APP_DIR / "raw"
BROWSER_PROFILE_DIR = APP_DIR / "browser-profile"
REPORT_PATH = APP_DIR / "report.html"

PACKAGE_DIR = Path(__file__).resolve().parent
BUNDLED_CONFIG_DIR = PACKAGE_DIR / "config"
SEARCH_EXAMPLE_PATH = BUNDLED_CONFIG_DIR / "search.example.yaml"
SOURCES_CONFIG_PATH = BUNDLED_CONFIG_DIR / "sources.yaml"

_DIRS = (APP_DIR, RAW_DIR)

_WARNED_NO_CONFIG = False


def ensure_dirs() -> None:
    """Create the user data directories if they don't exist."""
    for d in _DIRS:
        d.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    """Load ~/.homescout/.env into os.environ if present. Optional — the tool
    works fully without any API key; .env only enables the LLM explanation upgrade."""
    if not ENV_PATH.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        log.debug("python-dotenv not installed; skipping .env load")


# ---------------------------------------------------------------------------
# Search config
# ---------------------------------------------------------------------------

def load_search_config(path: Path | None = None) -> SearchCriteria:
    """Load search.yaml into a SearchCriteria.

    Falls back to SearchCriteria defaults when the file is missing, so the
    pipeline is runnable before `init` has been run.
    """
    path = path or SEARCH_CONFIG_PATH
    if not path.exists():
        # Warn once per process; this is called from several stages and the
        # repeated message reads like a new problem each time.
        global _WARNED_NO_CONFIG
        if not _WARNED_NO_CONFIG:
            log.warning("No search config at %s — using built-in defaults. Run `homescout init`.", path)
            _WARNED_NO_CONFIG = True
        return SearchCriteria()

    with path.open() as f:
        raw = yaml.safe_load(f) or {}

    return criteria_from_dict(raw)


def criteria_from_dict(raw: dict) -> SearchCriteria:
    """Build SearchCriteria from a nested search.yaml dict."""
    search = raw.get("search", {}) or {}
    location = raw.get("location", {}) or {}
    weights = raw.get("weights", {}) or {}

    defaults = SearchCriteria()

    return SearchCriteria(
        area=location.get("area", defaults.area),
        cbd_lat=float(location.get("cbd_lat", defaults.cbd_lat)),
        cbd_lon=float(location.get("cbd_lon", defaults.cbd_lon)),
        price_min=int(search.get("price_min", defaults.price_min)),
        price_max=int(search.get("price_max", defaults.price_max)),
        beds_min=int(search.get("beds_min", defaults.beds_min)),
        beds_max=_opt_int(search.get("beds_max")),
        baths_min=int(search.get("baths_min", defaults.baths_min)),
        baths_max=_opt_int(search.get("baths_max")),
        sqft_min=_opt_int(search.get("sqft_min")),
        sqft_max=_opt_int(search.get("sqft_max")),
        max_commute_min=int(search.get("max_commute_min", defaults.max_commute_min)),
        commute_mode=str(search.get("commute_mode", defaults.commute_mode)),
        max_listing_age_days=int(search.get("max_listing_age_days", defaults.max_listing_age_days)),
        property_types=list(search.get("property_types", defaults.property_types)),
        weight_value=float(weights.get("value", defaults.weight_value)),
        weight_location=float(weights.get("location", defaults.weight_location)),
        weight_freshness=float(weights.get("freshness", defaults.weight_freshness)),
    )


def criteria_to_dict(c: SearchCriteria) -> dict:
    """Serialize SearchCriteria back into the nested search.yaml shape."""
    return {
        "location": {
            "area": c.area,
            "cbd_lat": c.cbd_lat,
            "cbd_lon": c.cbd_lon,
        },
        "search": {
            "price_min": c.price_min,
            "price_max": c.price_max,
            "beds_min": c.beds_min,
            "beds_max": c.beds_max,
            "baths_min": c.baths_min,
            "baths_max": c.baths_max,
            "sqft_min": c.sqft_min,
            "sqft_max": c.sqft_max,
            "max_commute_min": c.max_commute_min,
            "commute_mode": c.commute_mode,
            "max_listing_age_days": c.max_listing_age_days,
            "property_types": c.property_types,
        },
        "weights": {
            "value": round(c.weight_value, 4),
            "location": round(c.weight_location, 4),
            "freshness": round(c.weight_freshness, 4),
        },
    }


def save_search_config(c: SearchCriteria, path: Path | None = None) -> Path:
    """Write SearchCriteria to search.yaml."""
    path = path or SEARCH_CONFIG_PATH
    ensure_dirs()
    with path.open("w") as f:
        yaml.safe_dump(criteria_to_dict(c), f, sort_keys=False, default_flow_style=False)
    return path


def load_sources_config() -> dict:
    """Load the bundled sources.yaml (scraper pacing, endpoints, limits)."""
    if not SOURCES_CONFIG_PATH.exists():
        return {}
    with SOURCES_CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {}


def _opt_int(value) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
