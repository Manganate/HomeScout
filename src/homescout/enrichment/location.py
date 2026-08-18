"""Municipal open data: parcel assessments.

`assessment_ratio = list_price / assessed_value` is a stronger value signal than
price-per-sqft alone, because it directly surfaces listings priced below the
municipality's own market assessment.

The parcel table is fetched once as a slim CSV (address, value, year — filtered
server-side to residential) and cached in SQLite. Paging this per run would make
the analyze stage take many minutes for no benefit.

The join is EXACT-MATCH ONLY. A mismatched parcel is worse than a missing one:
it produces a confident wrong number in user-facing prose. The match rate is
logged so a silent collapse is visible rather than looking like "nothing is
below assessment".
"""

from __future__ import annotations

import csv
import io
import logging

import httpx

from homescout.config import load_sources_config
from homescout.database import assessments_count, bulk_insert_assessments, lookup_assessment, meta_set
from homescout.enrichment.geo import user_agent
from homescout.filtering.quality import normalize_address

log = logging.getLogger(__name__)

_PARAMS = {
    "$select": "address,assessed_value,roll_year",
    "$where": "assessment_class='RE'",
    "$limit": "700000",
}

META_KEY_LOADED = "assessments_loaded_year"


def ensure_assessments(conn, force: bool = False) -> int:
    """Populate the assessments table if empty. Returns the row count held.

    Best-effort: if the portal is unreachable, the pipeline continues with
    assessment_ratio unavailable and the scorer reweights around it.
    """
    existing = assessments_count(conn)
    if existing > 0 and not force:
        log.debug("Assessments already cached (%s rows)", f"{existing:,}")
        return existing

    cfg = load_sources_config().get("services", {}).get("assessments_open_data", {}) or {}
    url = cfg.get("assessments_resource")
    if not url:
        log.debug("No assessments_open_data.assessments_resource configured; skipping")
        return existing
    timeout = float(cfg.get("timeout_s", 300))

    log.info("Fetching municipal parcel assessments (one-time, large residential export)...")
    try:
        response = httpx.get(
            url,
            params=_PARAMS,
            timeout=timeout,
            headers={"User-Agent": user_agent()},
            follow_redirects=True,
        )
        if response.status_code != 200:
            log.warning("Municipal open data returned HTTP %s; assessments unavailable", response.status_code)
            return existing
    except httpx.HTTPError as exc:
        log.warning("Could not fetch municipal assessments (%s); continuing without them", exc)
        return existing

    rows = _parse_csv(response.text)
    if not rows:
        log.warning("Municipal assessment export parsed to zero rows")
        return existing

    inserted = bulk_insert_assessments(conn, rows)
    year = rows[0][2] if rows else 0
    meta_set(conn, META_KEY_LOADED, str(year))
    log.info("Cached %s residential parcel assessments (roll year %s)", f"{inserted:,}", year)
    return inserted


def _parse_csv(text: str) -> list[tuple[str, int, int]]:
    """Parse the slim CSV into (address_key, assessed_value, year) rows."""
    rows: list[tuple[str, int, int]] = []
    seen: set[str] = set()

    reader = csv.DictReader(io.StringIO(text))
    for record in reader:
        address = (record.get("address") or "").strip()
        if not address:
            continue

        key = normalize_address(address)
        if not key or key in seen:
            # Duplicate keys are usually multi-unit parcels sharing a street
            # address. Ambiguous, so the first wins and the rest are skipped.
            continue

        value = _to_int(record.get("assessed_value"))
        if not value or value <= 0:
            continue

        seen.add(key)
        rows.append((key, value, _to_int(record.get("roll_year")) or 0))

    return rows


def assessment_for(conn, address: str) -> int | None:
    """Exact-match lookup of the assessed value for a listing address."""
    key = normalize_address(address)
    if not key:
        return None
    return lookup_assessment(conn, key)


def ratio(price: int | None, assessed: int | None) -> float | None:
    """list_price / assessed_value. Below 1.0 means priced under assessment."""
    if not price or not assessed or assessed <= 0:
        return None
    return round(price / assessed, 4)


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None
