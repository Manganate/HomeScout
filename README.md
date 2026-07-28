# HomeScout

A real estate listing agent: scrape listings, filter them against hard criteria, enrich them with data the listing site doesn't provide, score them, and return a ranked top 10 with plain-language reasons.

Built for Calgary, Alberta by default, but every search parameter is configurable.

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

Enrichment uses free, keyless public services — OpenStreetMap Overpass for amenities, OSRM for routing, Nominatim for geocoding, and the City of Calgary open data portal for parcel assessments. All are rate-limited and cached in SQLite, so re-runs cost nothing: a cold analyze stage takes minutes, a warm one under a second.

### Known limits

**The REALTOR.ca response schema is unverified against live output.** That endpoint is undocumented, and its field names are not contractual. The parser looks each value up across several paths observed in public clients, and a post-scrape health check warns loudly if a critical field comes back mostly empty — but until the scraper has run against the live site, treat the field mapping in `discovery/realtor_ca.py` as unconfirmed. Raw payloads are written to `~/.homescout/raw/` on every run precisely so the mapping can be corrected from real data.

**The assessment-join match rate is measured, but on reformatted City records.** 40 real Calgary addresses were rewritten into portal-style variants (expanded street types, punctuated quadrants, unit prefixes) and matched at 100%. That validates the normalizer — it caught three wrong abbreviations (`AV` not `AVE`, `GR` for Green, `CO` for Court) — but it is not evidence about real portal output. Condo-style addresses such as `#302 123 Main Street SW` are expected to miss; they degrade to `assessment_ratio = None` and the scorer reweights around them rather than producing a wrong number.

## Terms of use

Listing data is retrieved for personal, single-market use at human pace. It is not redistributed or republished. Check the terms of service of any source you configure before enabling it — some portals prohibit automated access, and a licensed MLS feed is the compliant alternative where one is available to you.

## License

AGPL-3.0-only.
