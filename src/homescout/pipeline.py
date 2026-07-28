"""Pipeline orchestrator.

    scrape ──▶ filter ──▶ analyze ──▶ rank
    (parallel   (serial   (parallel    (serial
     by source)  reduce)   by listing)  reduce)

Two worker pools, matching where the work actually fans out: scrape across
sources, analyze across listings. Filter and rank are serial reductions —
ranking needs the whole cohort present to compute percentiles, so it cannot be
parallelized without changing its meaning.

Each stage reads from SQLite and writes back, so stages are independently
runnable and resumable: `homescout run analyze rank` re-scores without
re-scraping.

Usage (via CLI):
    homescout run                      # all stages
    homescout run analyze rank         # specific stages
    homescout run --dry-run            # preview without executing
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console

from homescout.config import load_search_config, load_sources_config
from homescout.database import (
    STAGE_ANALYZED,
    STAGE_FILTERED,
    STAGE_RANKED,
    STAGE_SCRAPED,
    get_connection,
    get_listings_by_stage,
    init_db,
    reject,
    set_stage,
    update_enrichment,
    update_scores,
    upsert_listings,
)
from homescout.models import SearchCriteria

log = logging.getLogger(__name__)
console = Console()

STAGE_ORDER = ("scrape", "filter", "analyze", "rank")

STAGE_META: dict[str, dict] = {
    "scrape":  {"desc": "Fetch listings from configured sources"},
    "filter":  {"desc": "Apply hard criteria, validate, and dedupe"},
    "analyze": {"desc": "Commute, amenities, walkability, assessed value"},
    "rank":    {"desc": "Percentile scoring and explanations"},
}

_UPSTREAM: dict[str, str | None] = {
    "scrape": None,
    "filter": "scrape",
    "analyze": "filter",
    "rank": "analyze",
}


def run(
    stages: list[str] | None = None,
    source: str | None = None,
    dry_run: bool = False,
    criteria: SearchCriteria | None = None,
) -> dict:
    """Run the requested stages in order. Returns per-stage stats."""
    stages = _resolve_stages(stages)
    criteria = criteria or load_search_config()

    if dry_run:
        _preview(stages, criteria, source)
        return {"dry_run": True, "stages": stages}

    init_db()
    stats: dict = {}

    for stage in stages:
        console.rule(f"[bold cyan]{stage}[/bold cyan] — {STAGE_META[stage]['desc']}")
        runner = _RUNNERS[stage]
        stats[stage] = runner(criteria, source)

    return stats


# ---------------------------------------------------------------------------
# Stage: scrape
# ---------------------------------------------------------------------------

def _run_scrape(criteria: SearchCriteria, source: str | None) -> dict:
    """Fan out across sources. Never across pages of a single host — serial
    pacing per host is what keeps the scraper from tripping bot detection."""
    from homescout.discovery.base import get_source

    names = [source] if source else _enabled_sources()
    if not names:
        console.print("[yellow]No listing sources enabled.[/yellow]")
        return {"sources": 0, "listings": 0}

    workers = min(len(names), _concurrency("scrape_workers", 2))
    collected = []
    errors = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for name in names:
            try:
                futures[pool.submit(get_source(name).fetch, criteria)] = name
            except Exception as exc:  # source failed to construct
                errors[name] = str(exc)
                console.print(f"  [red]{name}: {exc}[/red]")

        for future in as_completed(futures):
            name = futures[future]
            try:
                found = future.result()
                collected.extend(found)
                console.print(f"  [green]{name}[/green]: {len(found)} listings")
            except Exception as exc:
                errors[name] = str(exc)
                console.print(f"  [red]{name} failed: {exc}[/red]")

    conn = get_connection()
    try:
        saved = upsert_listings(conn, collected)
    finally:
        conn.close()

    console.print(f"  [bold]{saved}[/bold] listings stored")
    return {"sources": len(names), "listings": saved, "errors": errors}


# ---------------------------------------------------------------------------
# Stage: filter
# ---------------------------------------------------------------------------

def _run_filter(criteria: SearchCriteria, source: str | None) -> dict:
    """Serial reduction: validate, dedupe, then apply hard criteria."""
    from homescout.filtering import criteria as crit
    from homescout.filtering import quality

    conn = get_connection()
    try:
        rows = get_listings_by_stage(conn, STAGE_SCRAPED)
        if not rows:
            console.print("  [yellow]Nothing to filter — run scrape first.[/yellow]")
            return {"input": 0, "passed": 0}

        listings = [_row_to_listing(r) for r in rows]

        valid, invalid = [], 0
        for listing in listings:
            reason = quality.validate(listing)
            if reason:
                reject(conn, listing.mls_id, reason)
                invalid += 1
            else:
                quality.reconcile_age(listing)
                valid.append(listing)

        deduped, dropped = quality.dedupe(valid)
        kept_ids = {l.mls_id for l in deduped}
        for listing in valid:
            if listing.mls_id not in kept_ids:
                reject(conn, listing.mls_id, "duplicate listing")

        passed, rejected = crit.apply(deduped, criteria)

        for listing, reason in rejected:
            reject(conn, listing.mls_id, reason)

        for listing in passed:
            update_enrichment(conn, listing.mls_id, {
                "distance_km": listing.distance_km,
                "days_on_market": listing.days_on_market,
            })
            set_stage(conn, listing.mls_id, STAGE_FILTERED)

        conn.commit()

        console.print(
            f"  {len(rows)} in → [red]{invalid}[/red] invalid, [red]{dropped}[/red] duplicate, "
            f"[red]{len(rejected)}[/red] off-criteria → [bold green]{len(passed)}[/bold green] passed"
        )
        return {"input": len(rows), "invalid": invalid, "duplicates": dropped,
                "off_criteria": len(rejected), "passed": len(passed)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stage: analyze
# ---------------------------------------------------------------------------

def _run_analyze(criteria: SearchCriteria, source: str | None) -> dict:
    """Fan out across listings, behind a shared rate limiter.

    The pool is small on purpose: every upstream service here is a free public
    endpoint capped near 1 req/s, so a large pool would just queue behind the
    token bucket. Phases B and C of the distance handling happen here.
    """
    from homescout.enrichment import amenities, calgary, commute, walkability

    conn = get_connection()
    try:
        rows = get_listings_by_stage(conn, STAGE_FILTERED)
        if not rows:
            console.print("  [yellow]Nothing to analyze — run filter first.[/yellow]")
            return {"input": 0, "analyzed": 0}

        # One-time bulk load; joined locally thereafter.
        held = calgary.ensure_assessments(conn)
        if held:
            console.print(f"  Parcel assessments available: [bold]{held:,}[/bold]")

        # Amenities are fetched once for the whole cohort's bounding box and
        # counted locally per listing. One query per listing meant N
        # rate-limited round trips and was heavy enough to draw 504s from the
        # public Overpass instances.
        points = [(r["latitude"], r["longitude"]) for r in rows]
        bbox = amenities.cohort_bbox(points)
        poi_index = amenities.fetch_poi_index(bbox, conn=conn) if bbox else None
        if poi_index:
            total_pois = sum(len(v) for v in poi_index.values())
            console.print(f"  Amenity index: [bold]{total_pois:,}[/bold] POIs across "
                          f"{len(poi_index)} categories")
        else:
            console.print("  [yellow]Amenity data unavailable — location scores "
                          "will lean on commute time.[/yellow]")

        workers = _concurrency("analyze_workers", 4)
        results: dict[str, dict] = {}
        pending_cache: list[tuple] = []
        cache_lock = threading.Lock()

        # Workers read the caches through their own read-only connection but
        # never write: SQLite allows one writer at a time, so concurrent cache
        # writes here produced "database is locked" and silently lost entries.
        # Fetched values are queued and written once, on this thread, below.
        local = threading.local()

        def reader():
            if not hasattr(local, "conn"):
                local.conn = get_connection()
            return local.conn

        def enrich(row) -> tuple[str, dict]:
            rconn = reader()
            lat, lon = row["latitude"], row["longitude"]
            fields: dict = {}

            minutes, estimated, from_cache = commute.commute_minutes(lat, lon, criteria, conn=rconn)
            if minutes is not None:
                fields["commute_min"] = minutes
                fields["commute_estimated"] = int(estimated)
                if not from_cache:
                    with cache_lock:
                        pending_cache.append(("commute", lat, lon, criteria.commute_mode, minutes, estimated))

            counts = amenities.count_near(poi_index, lat, lon)
            if counts is not None:
                fields.update({
                    "schools_n": counts.get("schools"),
                    "cafes_n": counts.get("cafes"),
                    "parks_n": counts.get("parks"),
                    "groceries_n": counts.get("groceries"),
                    "transit_n": counts.get("transit"),
                })
                with cache_lock:
                    pending_cache.append(("amenities", lat, lon, counts))
            # walkability.score(None) returns None, so an unavailable amenity
            # index reads as "no data" and the scorer reweights around it,
            # rather than as a genuine zero-amenity location.
            fields["walk_proxy"] = walkability.score(counts)

            return row["mls_id"], fields

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(enrich, row) for row in rows]
            for future in as_completed(futures):
                try:
                    mls_id, fields = future.result()
                    results[mls_id] = fields
                except Exception as exc:
                    log.warning("Enrichment failed for a listing: %s", exc)

        _flush_cache(conn, pending_cache)

        # Assessment join and persistence happen on the main thread: SQLite
        # writes are serialized anyway, and the match rate must be counted once.
        matched = 0
        for row in rows:
            fields = results.get(row["mls_id"], {})

            assessed = calgary.assessment_for(conn, row["address"])
            if assessed:
                matched += 1
                fields["assessed_value"] = assessed
                fields["assessment_ratio"] = calgary.ratio(row["price"], assessed)

            update_enrichment(conn, row["mls_id"], fields)

        # Phase C: the actual minutes cut, now that real routing exists.
        cut = 0
        for row in rows:
            minutes = results.get(row["mls_id"], {}).get("commute_min")
            if minutes is not None and minutes > criteria.max_commute_min:
                reject(conn, row["mls_id"],
                       f"{minutes:.0f} min commute exceeds {criteria.max_commute_min} min limit")
                cut += 1
            else:
                set_stage(conn, row["mls_id"], STAGE_ANALYZED)

        conn.commit()

        rate = (matched / len(rows) * 100) if rows else 0
        console.print(f"  Assessment join matched [bold]{matched}/{len(rows)}[/bold] ({rate:.0f}%)")
        if rows and rate < 60:
            console.print("  [yellow]Low assessment match rate — value scores lean on price/sqft.[/yellow]")
        console.print(f"  [red]{cut}[/red] cut on commute → [bold green]{len(rows) - cut}[/bold green] analyzed")

        return {"input": len(rows), "analyzed": len(rows) - cut,
                "commute_cut": cut, "assessment_matched": matched, "assessment_rate": round(rate, 1)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stage: rank
# ---------------------------------------------------------------------------

def _run_rank(criteria: SearchCriteria, source: str | None) -> dict:
    """Serial reduction — percentiles need the whole cohort at once."""
    from homescout.scoring.explain import explain
    from homescout.scoring.scorer import score_cohort

    conn = get_connection()
    try:
        rows = get_listings_by_stage(conn, STAGE_ANALYZED)
        if not rows:
            console.print("  [yellow]Nothing to rank — run analyze first.[/yellow]")
            return {"input": 0, "ranked": 0}

        dicts = [dict(r) for r in rows]
        scored = score_cohort(dicts, criteria)

        rows_by_id = {d["mls_id"]: d for d in dicts}
        order = sorted(scored, key=lambda s: s.score_total, reverse=True)
        cohort_size = len(order)

        for rank, s in enumerate(order, start=1):
            row = rows_by_id[s.mls_id]
            update_scores(conn, s.mls_id, {
                "score_value": s.score_value,
                "score_location": s.score_location,
                "score_freshness": s.score_freshness,
                "score_total": s.score_total,
                "data_completeness": s.data_completeness,
                "explanation": explain(row, s, rank, cohort_size),
            })
            set_stage(conn, s.mls_id, STAGE_RANKED)

        conn.commit()

        avg_completeness = sum(s.data_completeness for s in scored) / len(scored)
        console.print(f"  Ranked [bold green]{cohort_size}[/bold green] listings "
                      f"(avg data completeness {avg_completeness:.0f}%)")
        if cohort_size < 10:
            console.print(f"  [yellow]Small cohort ({cohort_size}) — percentile comparisons are coarse.[/yellow]")

        return {"input": len(rows), "ranked": cohort_size,
                "avg_completeness": round(avg_completeness, 1)}
    finally:
        conn.close()


_RUNNERS = {
    "scrape": _run_scrape,
    "filter": _run_filter,
    "analyze": _run_analyze,
    "rank": _run_rank,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flush_cache(conn, pending: list[tuple]) -> int:
    """Write queued enrichment cache entries on a single thread."""
    if not pending:
        return 0

    from homescout.database import cache_put_amenities, cache_put_commute, geo_key

    written = 0
    for entry in pending:
        kind = entry[0]
        if kind == "commute":
            _, lat, lon, mode, minutes, estimated = entry
            cache_put_commute(conn, geo_key(lat, lon), mode, minutes, estimated)
        elif kind == "amenities":
            _, lat, lon, counts = entry
            cache_put_amenities(conn, geo_key(lat, lon), counts)
        written += 1

    conn.commit()
    log.debug("Flushed %d enrichment cache entries", written)
    return written


def _resolve_stages(stages: list[str] | None) -> list[str]:
    if not stages:
        return list(STAGE_ORDER)
    unknown = [s for s in stages if s not in STAGE_META]
    if unknown:
        raise ValueError(f"Unknown stage(s): {', '.join(unknown)}. Valid: {', '.join(STAGE_ORDER)}")
    return [s for s in STAGE_ORDER if s in stages]


def _enabled_sources() -> list[str]:
    cfg = load_sources_config().get("listing_sources", {}) or {}
    enabled = [name for name, conf in cfg.items() if (conf or {}).get("enabled")]
    # Fixtures are a development source; never run them alongside a live source.
    live = [n for n in enabled if n != "fixtures"]
    return live or enabled


def _concurrency(key: str, default: int) -> int:
    cfg = load_sources_config().get("concurrency", {}) or {}
    return int(cfg.get(key, default))


def _row_to_listing(row):
    from homescout.models import Listing
    fields = {f for f in Listing.__dataclass_fields__}
    # sqlite3.Row iterates over values, not keys, so .keys() is required here.
    data = {k: row[k] for k in row.keys() if k in fields}  # noqa: SIM118
    return Listing(**data)


def _preview(stages: list[str], criteria: SearchCriteria, source: str | None) -> None:
    from rich.table import Table

    table = Table(title="Dry run — no changes will be made", title_style="bold yellow")
    table.add_column("Stage", style="cyan")
    table.add_column("Would do")

    for stage in stages:
        table.add_row(stage, STAGE_META[stage]["desc"])

    console.print(table)
    console.print(
        f"\n[dim]Area:[/dim] {criteria.area}   "
        f"[dim]Budget:[/dim] ${criteria.price_min:,}–${criteria.price_max:,}   "
        f"[dim]Beds/baths:[/dim] {criteria.beds_min}+/{criteria.baths_min}+\n"
        f"[dim]Commute:[/dim] ≤{criteria.max_commute_min} min {criteria.commute_mode} "
        f"([dim]pre-filter[/dim] {criteria.geo_bound_km:.1f} km)   "
        f"[dim]Age:[/dim] ≤{criteria.max_listing_age_days} d\n"
        f"[dim]Source:[/dim] {source or ', '.join(_enabled_sources()) or 'none enabled'}"
    )
