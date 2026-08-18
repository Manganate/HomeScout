"""End-to-end pipeline tests over the fixture source.

Network access is stubbed out: the public enrichment services are rate-limited
and non-commercial, so the suite must never call them. The stubs stand in for
OSRM and Overpass while every other code path — staging, persistence, the
assessment join, scoring, explanations — runs for real.
"""

from __future__ import annotations

import pytest

from homescout.models import SearchCriteria


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated HOMESCOUT_HOME with all outbound calls stubbed."""
    monkeypatch.setenv("HOMESCOUT_HOME", str(tmp_path))

    # config/database read module-level paths, so reload after setting the env.
    import importlib

    from homescout import config, database
    importlib.reload(config)
    importlib.reload(database)

    from homescout import pipeline
    importlib.reload(pipeline)

    from homescout.enrichment import amenities, commute, location

    # Deterministic commute: derived from distance, never routed.
    def fake_commute(lat, lon, criteria, conn=None):
        from homescout.filtering.criteria import haversine_km
        km = haversine_km(lat, lon, criteria.cbd_lat, criteria.cbd_lon)
        return round(km * 1.6, 1), True, False

    # A small synthetic POI index spread across the city.
    def fake_index(bbox, conn=None):
        south, west, north, east = bbox
        pts = []
        for i in range(12):
            f = i / 11
            pts.append((south + (north - south) * f, west + (east - west) * f))
        return {c: pts for c in amenities.CATEGORIES}

    monkeypatch.setattr(commute, "commute_minutes", fake_commute)
    monkeypatch.setattr(amenities, "fetch_poi_index", fake_index)
    # No parcel download; the join simply finds nothing, which is a valid state.
    monkeypatch.setattr(location, "ensure_assessments", lambda conn, force=False: 0)

    database.init_db()
    return {"pipeline": pipeline, "database": database, "tmp_path": tmp_path}


CRITERIA = SearchCriteria(
    cbd_lat=51.0452, cbd_lon=-114.0630,  # matches the fixture parcel data in _parcels.py
    price_min=600_000, price_max=800_000,
    beds_min=3, baths_min=2,
    max_commute_min=20, max_listing_age_days=30,
    property_types=["House", "Townhouse", "Duplex", "Condo"],
)


def _run_all(env):
    return env["pipeline"].run(
        stages=["scrape", "filter", "analyze", "rank"],
        source="fixtures",
        criteria=CRITERIA,
    )


# ---------------------------------------------------------------------------
# Stage isolation
# ---------------------------------------------------------------------------

def test_stages_run_and_advance(env):
    stats = _run_all(env)
    assert stats["scrape"]["listings"] > 0
    assert stats["filter"]["passed"] > 0
    assert stats["rank"]["ranked"] > 0


def test_filter_removes_the_planted_edge_cases(env):
    env["pipeline"].run(stages=["scrape", "filter"], source="fixtures", criteria=CRITERIA)

    conn = env["database"].get_connection()
    try:
        rejected = conn.execute(
            "SELECT reject_reason FROM listings WHERE stage = 'rejected'"
        ).fetchall()
    finally:
        conn.close()

    reasons = " ".join(r["reject_reason"] or "" for r in rejected)
    assert "price" in reasons
    assert "coordinates" in reasons
    assert "implausible" in reasons
    assert "duplicate" in reasons


def test_second_full_run_reprocesses_everything(env):
    """A repeat run must not be a silent no-op.

    Listings are keyed by mls_id, so a second scrape hits ON CONFLICT for every
    already-seen listing. If the upsert leaves `stage` at whatever it reached,
    the filter stage finds nothing pending and the whole run quietly does
    nothing — on a tool whose entire purpose is repeated runs.
    """
    first = _run_all(env)
    second = _run_all(env)

    assert second["filter"]["passed"] == first["filter"]["passed"], \
        "second run filtered nothing — stage was not reset on re-scrape"
    assert second["rank"]["ranked"] == first["rank"]["ranked"]


def test_rescrape_reconsiders_a_previously_rejected_listing(env):
    """A listing that was too expensive must be reconsidered if its price drops.

    Rejection is a judgement about the data at the time, not a permanent mark.
    """
    from homescout.models import Listing

    database = env["database"]
    mls_id = "PRICEDROP1"

    def listing_at(price):
        return Listing(
            mls_id=mls_id, source="fixtures", url="http://example.invalid",
            address="1 Test St SW", city="Example City",
            price=price, beds=3.0, baths=2.0, sqft=1500,
            property_type="House", latitude=51.04, longitude=-114.07,
            days_on_market=5,
        )

    # First sighting: over budget, so the filter stage rejects it.
    conn = database.get_connection()
    try:
        database.upsert_listings(conn, [listing_at(950_000)])
    finally:
        conn.close()

    env["pipeline"].run(stages=["filter"], criteria=CRITERIA)

    conn = database.get_connection()
    try:
        before = conn.execute(
            "SELECT stage, reject_reason FROM listings WHERE mls_id=?", (mls_id,)
        ).fetchone()
    finally:
        conn.close()
    assert before["stage"] == database.STAGE_REJECTED
    assert "above maximum" in before["reject_reason"]

    # Second sighting: the price has dropped into range.
    conn = database.get_connection()
    try:
        database.upsert_listings(conn, [listing_at(700_000)])
        after = conn.execute(
            "SELECT stage, reject_reason FROM listings WHERE mls_id=?", (mls_id,)
        ).fetchone()
    finally:
        conn.close()

    assert after["stage"] == database.STAGE_SCRAPED, \
        "a re-scraped listing must be reconsidered, not left rejected forever"
    assert after["reject_reason"] is None, "the stale rejection reason must be cleared"

    env["pipeline"].run(stages=["filter"], criteria=CRITERIA)

    conn = database.get_connection()
    try:
        final = conn.execute("SELECT stage FROM listings WHERE mls_id=?", (mls_id,)).fetchone()
    finally:
        conn.close()
    assert final["stage"] == database.STAGE_FILTERED


def test_rank_is_resumable_without_rescraping(env):
    _run_all(env)

    conn = env["database"].get_connection()
    try:
        before = conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"]
    finally:
        conn.close()

    env["database"].reset_stage(env["database"].get_connection(), "ranked", "analyzed")
    env["pipeline"].run(stages=["rank"], criteria=CRITERIA)

    conn = env["database"].get_connection()
    try:
        after = conn.execute("SELECT COUNT(*) n FROM listings").fetchone()["n"]
    finally:
        conn.close()

    assert before == after, "re-ranking must not add or lose listings"


# ---------------------------------------------------------------------------
# The results must actually satisfy what was asked for
# ---------------------------------------------------------------------------

def test_every_ranked_listing_satisfies_the_criteria(env):
    _run_all(env)

    conn = env["database"].get_connection()
    try:
        rows = [dict(r) for r in env["database"].get_top_ranked(conn, 10)]
    finally:
        conn.close()

    assert rows, "expected ranked results"

    for r in rows:
        assert CRITERIA.price_min <= r["price"] <= CRITERIA.price_max, r["address"]
        assert r["beds"] >= CRITERIA.beds_min, r["address"]
        assert r["baths"] >= CRITERIA.baths_min, r["address"]
        assert r["days_on_market"] <= CRITERIA.max_listing_age_days, r["address"]
        assert r["commute_min"] <= CRITERIA.max_commute_min, (
            f"{r['address']} has a {r['commute_min']} min commute, over the "
            f"{CRITERIA.max_commute_min} min limit"
        )


def test_results_are_ordered_by_score(env):
    _run_all(env)
    conn = env["database"].get_connection()
    try:
        rows = [dict(r) for r in env["database"].get_top_ranked(conn, 10)]
    finally:
        conn.close()

    scores = [r["score_total"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_explanations_quote_the_stored_scores(env):
    """Explanation numbers must match the stored sub-scores, not be invented."""
    _run_all(env)

    conn = env["database"].get_connection()
    try:
        rows = [dict(r) for r in env["database"].get_top_ranked(conn, 10)]
    finally:
        conn.close()

    for r in rows:
        text = r["explanation"]
        assert text, f"missing explanation for {r['address']}"
        assert f"{r['score_total']:.0f}/100" in text

        for label, key in (("Value", "score_value"),
                           ("Location", "score_location"),
                           ("Freshness", "score_freshness")):
            if r[key] is not None:
                assert f"{label} {r[key]:.0f}" in text, (
                    f"{label} score {r[key]} not reflected in: {text}"
                )


def test_explanation_names_missing_data(env):
    """A listing scored on partial data must say so, not silently omit it."""
    _run_all(env)

    conn = env["database"].get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM listings WHERE stage='ranked' AND sqft IS NULL"
        ).fetchall()]
    finally:
        conn.close()

    if not rows:
        pytest.skip("no partial-data listings survived filtering")

    for r in rows:
        assert r["data_completeness"] < 100.0
        assert "available signals" in r["explanation"], r["explanation"]


def test_cohort_size_is_reported_in_explanations(env):
    """Percentile phrasing is meaningless without the cohort size."""
    stats = _run_all(env)
    cohort = stats["rank"]["ranked"]

    conn = env["database"].get_connection()
    try:
        row = dict(env["database"].get_top_ranked(conn, 1)[0])
    finally:
        conn.close()

    assert f"of {cohort} matches" in row["explanation"]
