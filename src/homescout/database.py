"""SQLite persistence: listings, stage tracking, and enrichment caches.

Each pipeline stage reads its input from here and writes its output back, which
is what makes stages independently runnable and resumable
(`homescout run analyze rank` re-scores without re-scraping).

The cache tables matter as much as the listings table: every enrichment service
is a free public endpoint with a hard rate limit, so repeated and overlapping
lookups must never hit the network twice.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from homescout.config import DB_PATH, ensure_dirs
from homescout.models import Listing

log = logging.getLogger(__name__)

# Stage a listing has advanced to.
STAGE_SCRAPED = "scraped"
STAGE_FILTERED = "filtered"
STAGE_ANALYZED = "analyzed"
STAGE_RANKED = "ranked"
STAGE_REJECTED = "rejected"

# Enrichment caches are keyed on lat/lon rounded to this many decimals.
# 3 decimals is roughly 110 m — close enough that two listings sharing a key
# have materially the same commute and amenity profile.
GEO_PRECISION = 3


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    mls_id            TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    url               TEXT,
    address           TEXT,
    city              TEXT,
    postal_code       TEXT,
    price             INTEGER,
    beds              REAL,
    baths             REAL,
    sqft              INTEGER,
    lot_sqft          INTEGER,
    property_type     TEXT,
    list_date         TEXT,
    days_on_market    INTEGER,
    latitude          REAL,
    longitude         REAL,
    price_per_sqft    REAL,

    -- enrichment
    distance_km       REAL,
    commute_min       REAL,
    commute_estimated INTEGER DEFAULT 0,
    walk_proxy        REAL,
    schools_n         INTEGER,
    cafes_n           INTEGER,
    parks_n           INTEGER,
    groceries_n       INTEGER,
    transit_n         INTEGER,
    assessed_value    INTEGER,
    assessment_ratio  REAL,

    -- scoring
    score_value       REAL,
    score_location    REAL,
    score_freshness   REAL,
    score_total       REAL,
    data_completeness REAL,
    explanation       TEXT,

    -- bookkeeping
    stage             TEXT DEFAULT 'scraped',
    reject_reason     TEXT,
    first_seen        REAL,
    last_seen         REAL
);

CREATE INDEX IF NOT EXISTS idx_listings_stage ON listings(stage);
CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(score_total DESC);

CREATE TABLE IF NOT EXISTS commute_cache (
    geo_key      TEXT PRIMARY KEY,
    mode         TEXT,
    minutes      REAL,
    estimated    INTEGER DEFAULT 0,
    fetched_at   REAL
);

CREATE TABLE IF NOT EXISTS amenity_cache (
    geo_key      TEXT PRIMARY KEY,
    payload      TEXT,
    fetched_at   REAL
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    query        TEXT PRIMARY KEY,
    latitude     REAL,
    longitude    REAL,
    fetched_at   REAL
);

-- Bulk City of Calgary parcel assessments, fetched once and joined locally.
CREATE TABLE IF NOT EXISTS assessments (
    address_key    TEXT PRIMARY KEY,
    assessed_value INTEGER,
    year           INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns written by upsert_listing, in order.
_LISTING_COLUMNS = [
    "mls_id", "source", "url", "address", "city", "postal_code",
    "price", "beds", "baths", "sqft", "lot_sqft", "property_type",
    "list_date", "days_on_market", "latitude", "longitude", "price_per_sqft",
]


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with row access by name and sane concurrency settings."""
    ensure_dirs()
    conn = sqlite3.connect(path or DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(path: Path | None = None) -> None:
    """Create the schema if it doesn't exist."""
    conn = get_connection(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def geo_key(lat: float, lon: float) -> str:
    """Cache key for a coordinate, rounded to ~110 m."""
    return f"{round(lat, GEO_PRECISION)},{round(lon, GEO_PRECISION)}"


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def upsert_listing(conn: sqlite3.Connection, listing: Listing) -> None:
    """Insert or update a listing by mls_id, preserving first_seen and any
    enrichment already computed for it."""
    row = listing.to_row()
    now = time.time()
    values = [row.get(c) for c in _LISTING_COLUMNS]

    placeholders = ", ".join("?" for _ in _LISTING_COLUMNS)
    columns = ", ".join(_LISTING_COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _LISTING_COLUMNS if c != "mls_id")

    conn.execute(
        f"""
        INSERT INTO listings ({columns}, first_seen, last_seen, stage)
        VALUES ({placeholders}, ?, ?, ?)
        ON CONFLICT(mls_id) DO UPDATE SET
            {updates},
            last_seen=excluded.last_seen
        """,
        (*values, now, now, STAGE_SCRAPED),
    )


def upsert_listings(conn: sqlite3.Connection, listings: list[Listing]) -> int:
    for listing in listings:
        upsert_listing(conn, listing)
    conn.commit()
    return len(listings)


def get_listings_by_stage(conn: sqlite3.Connection, stage: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM listings WHERE stage = ?", (stage,)).fetchall()


def set_stage(conn: sqlite3.Connection, mls_id: str, stage: str, reason: str | None = None) -> None:
    conn.execute(
        "UPDATE listings SET stage = ?, reject_reason = ? WHERE mls_id = ?",
        (stage, reason, mls_id),
    )


def reject(conn: sqlite3.Connection, mls_id: str, reason: str) -> None:
    set_stage(conn, mls_id, STAGE_REJECTED, reason)


def update_enrichment(conn: sqlite3.Connection, mls_id: str, fields: dict) -> None:
    """Write enrichment columns back for one listing."""
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE listings SET {assignments} WHERE mls_id = ?",
        (*fields.values(), mls_id),
    )


def update_scores(conn: sqlite3.Connection, mls_id: str, fields: dict) -> None:
    update_enrichment(conn, mls_id, fields)


def get_top_ranked(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM listings
        WHERE stage = ? AND score_total IS NOT NULL
        ORDER BY score_total DESC
        LIMIT ?
        """,
        (STAGE_RANKED, limit),
    ).fetchall()


def get_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT stage, COUNT(*) AS n FROM listings GROUP BY stage").fetchall()
    stats = {r["stage"]: r["n"] for r in rows}
    stats["total"] = sum(stats.values())
    return stats


def reset_stage(conn: sqlite3.Connection, from_stage: str, to_stage: str) -> int:
    """Move listings back a stage so a stage can be re-run. Returns rows affected."""
    cur = conn.execute("UPDATE listings SET stage = ? WHERE stage = ?", (to_stage, from_stage))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

def cache_get_commute(conn: sqlite3.Connection, key: str, mode: str) -> tuple[float, bool] | None:
    row = conn.execute(
        "SELECT minutes, estimated FROM commute_cache WHERE geo_key = ? AND mode = ?",
        (key, mode),
    ).fetchone()
    if row is None:
        return None
    return row["minutes"], bool(row["estimated"])


def cache_put_commute(conn: sqlite3.Connection, key: str, mode: str, minutes: float, estimated: bool) -> None:
    conn.execute(
        """INSERT INTO commute_cache (geo_key, mode, minutes, estimated, fetched_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(geo_key) DO UPDATE SET
               mode=excluded.mode, minutes=excluded.minutes,
               estimated=excluded.estimated, fetched_at=excluded.fetched_at""",
        (key, mode, minutes, int(estimated), time.time()),
    )


def cache_get_amenities(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT payload FROM amenity_cache WHERE geo_key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None


def cache_put_amenities(conn: sqlite3.Connection, key: str, payload: dict) -> None:
    conn.execute(
        """INSERT INTO amenity_cache (geo_key, payload, fetched_at)
           VALUES (?, ?, ?)
           ON CONFLICT(geo_key) DO UPDATE SET
               payload=excluded.payload, fetched_at=excluded.fetched_at""",
        (key, json.dumps(payload), time.time()),
    )


def cache_get_geocode(conn: sqlite3.Connection, query: str) -> tuple[float, float] | None:
    row = conn.execute(
        "SELECT latitude, longitude FROM geocode_cache WHERE query = ?", (query,)
    ).fetchone()
    if row is None:
        return None
    return row["latitude"], row["longitude"]


def cache_put_geocode(conn: sqlite3.Connection, query: str, lat: float, lon: float) -> None:
    conn.execute(
        """INSERT INTO geocode_cache (query, latitude, longitude, fetched_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(query) DO UPDATE SET
               latitude=excluded.latitude, longitude=excluded.longitude,
               fetched_at=excluded.fetched_at""",
        (query, lat, lon, time.time()),
    )


# ---------------------------------------------------------------------------
# Assessments (bulk, joined locally)
# ---------------------------------------------------------------------------

def assessments_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM assessments").fetchone()
    return row["n"] if row else 0


def bulk_insert_assessments(conn: sqlite3.Connection, rows: list[tuple[str, int, int]]) -> int:
    conn.executemany(
        """INSERT INTO assessments (address_key, assessed_value, year)
           VALUES (?, ?, ?)
           ON CONFLICT(address_key) DO UPDATE SET
               assessed_value=excluded.assessed_value, year=excluded.year""",
        rows,
    )
    conn.commit()
    return len(rows)


def lookup_assessment(conn: sqlite3.Connection, address_key: str) -> int | None:
    """Exact-match only. A mismatched parcel is worse than a missing one — it
    produces a confident wrong number in user-facing prose."""
    row = conn.execute(
        "SELECT assessed_value FROM assessments WHERE address_key = ?", (address_key,)
    ).fetchone()
    return row["assessed_value"] if row else None


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
