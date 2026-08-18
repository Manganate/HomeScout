# HomeScout

A real estate listing agent: scrape listings, filter them against hard criteria, enrich them with data the listing site doesn't provide, score them, and return a ranked top 10 with plain-language reasons.

Every search parameter is configurable.

## Pipeline

```
scrape ──▶ filter ──▶ analyze ──▶ rank
(parallel   (serial   (parallel    (serial
 by source)  reduce)   by listing)  reduce)
```

Each stage reads from SQLite and writes back, so stages are independently runnable and resumable — `homescout run analyze rank` re-scores without re-scraping.

| Stage | What it does |
|---|---|
| **scrape** | Fetch listings from a configured source into a normalized schema |
| **filter** | Apply price, beds, baths, listing age, and a generous distance bound; validate and dedupe |
| **analyze** | Real commute time, walkability proxy, nearby amenities, City assessed value |
| **rank** | Percentile-based value / location / freshness scores, composite, and explanations |

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
homescout init
```

`init` walks through every search parameter and writes `~/.homescout/search.yaml`. Re-run it anytime, or edit that file directly — no code change is needed to search a different city, budget, or commute radius.

## Usage

```bash
homescout run                      # full pipeline
homescout run --dry-run            # preview the stage plan
homescout run analyze rank         # re-score without re-scraping
homescout run scrape --source fixtures   # offline sample data
homescout view --top 10            # ranked table + HTML report
homescout sources                  # list available listing sources
```

## Configuration

All user state lives in `~/.homescout/` — never in this repo:

| Path | Contents |
|---|---|
| `search.yaml` | Your search criteria and ranking weights |
| `homescout.db` | Listings, scores, and enrichment caches |
| `.env` | Optional API key for the LLM explanation upgrade |
| `raw/` | Raw source payloads, for debugging |
| `report.html` | Generated ranked report |

## Scoring

Three sub-scores, each normalized by **percentile rank within the current cohort** so a single outlier can't distort the scale:

- **Value** — price per sqft, and list price versus City assessed value
- **Location** — commute time to the CBD, walkability proxy, nearby amenities
- **Freshness** — days on market

Weights are set in `search.yaml` and default to equal thirds.

Missing data is handled explicitly: each sub-score is computed from whichever components are present, with weights renormalized across only those. A `data_completeness` figure is stored and displayed, so a high rank resting on partial data is visible rather than implied. Explanations name what's missing rather than silently omitting it.

The walkability figure is an **OpenStreetMap-derived proxy**, computed from distance-decayed POI counts. It is not Walk Score®.

## Data sources

Listing sources implement the `ListingSource` protocol in `discovery/base.py`, so swapping one changes nothing downstream.

| Source | Status | Notes |
|---|---|---|
| `email` | **default** | saved-search alert emails |
| `fixtures` | dev/test | Offline sample data built from real parcels |
| `realtor_ca` | **disabled** | Blocked by bot protection |

### `email` — alert ingestion

You create saved searches on REALTOR.ca; it emails you matching listings; HomeScout reads them. Nothing is scraped and no access control is worked around — the site is deliberately sending you this data, and it arrives in your own mailbox.

Two ways to supply the messages:

```bash
# 1. Drop them in as files (no credentials)
#    Drag the email from Mail to Finder, or File > Save As
cp ~/Desktop/*.eml ~/.homescout/inbox/
homescout run --source email

# 2. Or fetch over IMAP — add to ~/.homescout/.env
HOMESCOUT_IMAP_USER=you@gmail.com
HOMESCOUT_IMAP_PASSWORD=<app password, not your account password>
HOMESCOUT_IMAP_HOST=imap.gmail.com     # optional
HOMESCOUT_IMAP_FROM=realtor.ca         # optional sender filter
```

IMAP access is read-only: messages are fetched with `BODY.PEEK` so they aren't marked read, and nothing is ever deleted.

Two things the emails don't carry, and how each is recovered:

- **No coordinates.** Addresses are geocoded via Nominatim (1 req/s, cached), expanding street-type abbreviations first — `12 Someplace Vi NW` → `Someplace Villas`, which Nominatim can resolve. Without that expansion the listing fails to geocode and is dropped for missing coordinates.
- **No list date.** The email's own `Date:` header is the freshness signal — an alert means "new as of when this was sent". Bare saved HTML has no headers, so freshness is simply unavailable and the scorer reweights around it.

### `realtor_ca` — disabled

Direct scraping does not work. REALTOR.ca sits behind Imperva, which returns **HTTP 403 to automated browsers before the page loads**, so the search API is never reached. Notably a plain `curl` from the same IP gets 200 — the detection is on the browser fingerprint, not the network. Getting past that would mean defeating an access control the operator is actively enforcing, so the module is left disabled as a reference implementation.

Enrichment uses free, keyless public services — OpenStreetMap Overpass for amenities, OSRM for routing, Nominatim for geocoding, and a municipal open data portal for parcel assessments. All are rate-limited and cached in SQLite, so re-runs cost nothing: a cold analyze stage takes minutes, a warm one under a second.

## Terms of use

Listing data is retrieved for personal, single-market use at human pace. It is not redistributed or republished. Check the terms of service of any source you configure before enabling it — some portals prohibit automated access, and a licensed MLS feed is the compliant alternative where one is available to you.

## License

AGPL-3.0-only.
