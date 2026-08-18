"""Live listing source: REALTOR.ca via a real browser.

Why a real browser rather than an HTTP client: the site's internal
`PropertySearch_Post` endpoint sits behind Imperva/Incapsula, which rejects
datacenter IPs and headless-browser fingerprints outright. A visible Chromium
on a residential connection is the only approach that returns data.

Why the network response rather than the DOM: the JSON behind the map search
already carries coordinates, price, beds, baths and listing age in structured
form. Parsing it is both more robust than scraping rendered markup and far less
likely to break on a front-end redeploy.

TERMS OF USE — CREA's terms prohibit automated access to REALTOR.ca. This module
is written for personal, single-market use at human pace and does not
redistribute or republish listing data. It rate-limits itself, runs visibly, and
requires an explicit opt-in. Where a licensed MLS feed (CREA DDF, or a
commercial aggregator) is available to you, prefer it: the `ListingSource`
protocol exists so one can be dropped in without touching the pipeline.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import UTC, datetime, timedelta

from homescout.config import BROWSER_PROFILE_DIR, RAW_DIR, load_sources_config
from homescout.models import Listing, SearchCriteria

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.realtor.ca/map"
API_MARKER = "PropertySearch_Post"


class RealtorCaSource:
    """Fetches listings by driving the public map search UI."""

    name = "realtor_ca"

    def __init__(self, config: dict | None = None) -> None:
        cfg = config or (load_sources_config().get("listing_sources", {}) or {}).get("realtor_ca", {})
        self.cfg = cfg or {}
        self.headless = bool(self.cfg.get("headless", False))
        self.max_pages = int(self.cfg.get("max_pages", 20))
        self.page_size = int(self.cfg.get("page_size", 50))
        self.delay_min = float(self.cfg.get("page_delay_min", 3.0))
        self.delay_max = float(self.cfg.get("page_delay_max", 6.0))
        self.nav_timeout = int(self.cfg.get("nav_timeout_ms", 60000))
        self.bbox = self.cfg.get("bbox") or {}

    # -- Public API ---------------------------------------------------------

    def fetch(self, criteria: SearchCriteria) -> list[Listing]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required for the realtor_ca source. "
                "Install it with: pip install playwright && playwright install chromium"
            ) from exc

        payloads: list[dict] = []
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            # A persistent profile keeps cookies and the bot-check verdict
            # between runs, so a session is solved once rather than every run.
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=self.headless,
                viewport={"width": 1440, "height": 900},
                locale="en-CA",
                timezone_id="America/Edmonton",
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(self.nav_timeout)

            def on_response(response):
                if API_MARKER not in response.url:
                    return
                try:
                    payloads.append(response.json())
                except Exception:  # non-JSON or already consumed
                    log.debug("Could not decode a %s response", API_MARKER)

            page.on("response", on_response)

            try:
                log.info("Opening REALTOR.ca map search...")
                page.goto(self._search_url(criteria), wait_until="domcontentloaded")

                # Wait for the search call itself rather than a fixed sleep: a
                # slow first response would otherwise leave zero payloads and
                # return an empty result set that looks like "nothing matched".
                try:
                    page.wait_for_response(
                        lambda r: API_MARKER in r.url,
                        timeout=self.nav_timeout,
                    )
                except Exception:
                    log.debug("No %s response within the navigation timeout", API_MARKER)

                # The bot check may need a human. Running visibly is what makes
                # that possible; there is no automated bypass here by design.
                if self._looks_blocked(page):
                    log.warning(
                        "REALTOR.ca is showing a verification page. Solve it in the open "
                        "browser window; collection resumes automatically."
                    )
                    self._await_unblock(page)

                self._collect_pages(page, payloads)
            finally:
                context.close()

        listings = self._parse(payloads, criteria)
        self._dump_raw(payloads)
        log.info("REALTOR.ca returned %d listings", len(listings))
        return listings

    # -- Navigation ---------------------------------------------------------

    def _search_url(self, criteria: SearchCriteria) -> str:
        """Build a map-search URL carrying the price and room filters.

        Filters are pushed upstream to reduce how much is fetched, but the
        filter stage re-applies every criterion locally — a source is never
        trusted to have honored them.
        """
        bbox = self.bbox
        # Parameter names follow the documented shape of the underlying
        # PropertySearch_Post call (BedRange/BathRange are "min-max", where 0
        # means unbounded; PropertySearchTypeId 1 is Residential;
        # TransactionTypeId 2 is For Sale).
        params = [
            f"LatitudeMax={bbox.get('north', 0.0)}",
            f"LongitudeMax={bbox.get('east', 0.0)}",
            f"LatitudeMin={bbox.get('south', 0.0)}",
            f"LongitudeMin={bbox.get('west', 0.0)}",
            f"PriceMin={criteria.price_min}",
            f"PriceMax={criteria.price_max}",
            f"BedRange={criteria.beds_min}-0",
            f"BathRange={criteria.baths_min}-0",
            f"RecordsPerPage={self.page_size}",
            "PropertySearchTypeId=1",
            "TransactionTypeId=2",
            "SortBy=6-D",
            "CultureId=1",
            "Currency=CAD",
        ]
        return f"{SEARCH_URL}#{'&'.join(params)}"

    def _collect_pages(self, page, payloads: list[dict]) -> None:
        """Advance through result pages at human pace."""
        seen_before = len(payloads)

        for page_num in range(1, self.max_pages + 1):
            self._sleep()

            if len(payloads) == seen_before and page_num > 1:
                log.info("No new results on page %d; stopping.", page_num)
                break
            seen_before = len(payloads)

            if not self._go_next(page):
                log.info("Reached the last results page (%d).", page_num)
                break

    def _go_next(self, page) -> bool:
        """Click through to the next page. Returns False when there is none."""
        for selector in (
            'a[aria-label="Next page"]',
            'button[aria-label="Next page"]',
            "#gotoNextLink",
            ".paginationLinkForward",
        ):
            try:
                element = page.locator(selector).first
                if element.count() and element.is_enabled():
                    element.click(timeout=10000)
                    page.wait_for_timeout(2000)
                    return True
            except Exception:
                continue
        return False

    def _looks_blocked(self, page) -> bool:
        try:
            content = page.content().lower()
        except Exception:
            return False
        return any(marker in content for marker in (
            "access denied", "incapsula", "_incapsula_resource",
            "request unsuccessful", "are you a human", "unusual traffic",
        ))

    def _await_unblock(self, page, timeout_s: int = 300) -> None:
        """Wait for a human to clear the verification page."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            page.wait_for_timeout(3000)
            if not self._looks_blocked(page):
                log.info("Verification cleared; continuing.")
                return
        log.warning("Verification not cleared within %ds; continuing anyway.", timeout_s)

    def _sleep(self) -> None:
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    # -- Parsing ------------------------------------------------------------

    def _parse(self, payloads: list[dict], criteria: SearchCriteria) -> list[Listing]:
        listings: list[Listing] = []
        seen: set[str] = set()

        for payload in payloads:
            for result in (payload or {}).get("Results", []) or []:
                listing = self._to_listing(result)
                if listing and listing.mls_id not in seen:
                    seen.add(listing.mls_id)
                    listings.append(listing)

        _check_field_health(listings)
        return listings

    def _to_listing(self, result: dict) -> Listing | None:
        try:
            mls_id = str(result.get("MlsNumber") or result.get("Id") or "").strip()
            if not mls_id:
                return None

            prop = result.get("Property", {}) or {}
            address = prop.get("Address", {}) or {}
            building = result.get("Building", {}) or {}

            # This endpoint is undocumented and its field names are not
            # contractual. Each value is looked up across the paths observed in
            # public clients, so a rename in one place degrades that field to
            # None instead of silently emptying the whole record.
            lat = _first(_float, address.get("Latitude"), prop.get("Latitude"), result.get("Latitude"))
            lon = _first(_float, address.get("Longitude"), prop.get("Longitude"), result.get("Longitude"))

            street, city, postal = _split_address(
                address.get("AddressText") or address.get("Text") or ""
            )

            return Listing(
                mls_id=mls_id,
                source=self.name,
                url=_absolute(result.get("RelativeDetailsURL") or result.get("RelativeURLEn") or ""),
                address=street,
                city=city,
                postal_code=postal,
                price=_first(_price, prop.get("Price"), prop.get("PriceUnformattedValue"), result.get("Price")),
                beds=_first(_rooms, building.get("Bedrooms"), result.get("Bedrooms")),
                baths=_first(_rooms, building.get("BathroomTotal"), building.get("Bathrooms"),
                             result.get("BathroomTotal")),
                sqft=_first(_sqft, building.get("SizeInterior"), building.get("SizeInteriorFinished"),
                            building.get("TotalFinishedArea")),
                lot_sqft=_first(_sqft, prop.get("SizeTotal"), prop.get("LotSize")),
                property_type=str(prop.get("Type") or building.get("Type") or "").strip(),
                list_date=_list_date(result),
                days_on_market=_days_on_market(result),
                latitude=lat,
                longitude=lon,
            )
        except Exception as exc:
            log.debug("Skipping an unparseable result: %s", exc)
            return None

    def _dump_raw(self, payloads: list[dict]) -> None:
        """Persist raw payloads so a parsing break can be diagnosed offline."""
        if not payloads:
            return
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = RAW_DIR / f"realtor_ca-{stamp}.json"
        try:
            path.write_text(json.dumps(payloads, indent=2)[:20_000_000], encoding="utf-8")
            log.debug("Raw payloads written to %s", path)
        except OSError as exc:
            log.debug("Could not write raw payloads: %s", exc)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

# A field renamed upstream would otherwise show up as "no listings matched"
# rather than as a broken parser, so a scrape that returns records with a
# critical field almost entirely absent says so loudly.
_CRITICAL = ("price", "latitude", "beds")
_HEALTH_THRESHOLD = 0.5


def _check_field_health(listings: list[Listing]) -> None:
    if not listings:
        return

    total = len(listings)
    for field in _CRITICAL:
        present = sum(1 for listing in listings if getattr(listing, field) is not None)
        if present / total < _HEALTH_THRESHOLD:
            log.warning(
                "Only %d/%d listings have a '%s' value. REALTOR.ca's response fields are "
                "undocumented and may have changed — check the raw payload written to the "
                "raw/ directory and update the field paths in _to_listing().",
                present, total, field,
            )


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------

def _first(parser, *candidates):
    """Return the first candidate that parses to a non-None value."""
    for candidate in candidates:
        if candidate is None:
            continue
        value = parser(candidate)
        if value is not None:
            return value
    return None

_MONEY = re.compile(r"[^\d.]")
_SQFT = re.compile(r"([\d,]+(?:\.\d+)?)")
# "3 days", "1 month", "22 hours" — coarse, so it is only a fallback.
_AGE = re.compile(r"(\d+)\s*(hour|day|week|month|year)", re.IGNORECASE)
_UNITS = {"hour": 1 / 24, "day": 1, "week": 7, "month": 30.44, "year": 365.25}


def _list_date(result: dict) -> str | None:
    """Prefer a real timestamp; the formatted age string is a last resort.

    Freshness scoring and the age cut both need day-level resolution, and
    "1 month" cannot supply it.
    """
    for key in ("InsertedDateUTC", "ListingDate", "PostedDateTime"):
        raw = result.get(key)
        if not raw:
            continue
        # Some fields arrive as epoch-like integers rather than ISO strings.
        text = str(raw).strip()
        if text.isdigit() and len(text) >= 10:
            try:
                return datetime.fromtimestamp(int(text[:10]), tz=UTC).date().isoformat()
            except (ValueError, OSError):
                continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            continue

    # Fall back to deriving a date from the coarse age string.
    days = _days_on_market(result)
    if days is not None:
        return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()
    return None


def _days_on_market(result: dict) -> int | None:
    raw = result.get("TimeOnRealtor") or result.get("DaysOnMarket")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)

    match = _AGE.search(str(raw))
    if not match:
        return None
    count, unit = int(match.group(1)), match.group(2).lower()
    return int(round(count * _UNITS.get(unit, 1)))


def _split_address(text: str) -> tuple[str, str, str]:
    """Split "123 Main St SW, City, Province T2P1A1" into its parts."""
    parts = [p.strip() for p in str(text or "").split("|")[0].split(",") if p.strip()]
    street = parts[0] if parts else ""
    city = parts[1] if len(parts) > 1 else ""
    postal = ""
    if len(parts) > 2:
        tail = parts[-1]
        match = re.search(r"[A-Z]\d[A-Z]\s*\d[A-Z]\d", tail.upper())
        postal = match.group(0) if match else ""
    return street, city, postal


def _absolute(relative: str) -> str:
    relative = str(relative or "").strip()
    if not relative:
        return ""
    if relative.startswith("http"):
        return relative
    return "https://www.realtor.ca" + ("" if relative.startswith("/") else "/") + relative


def _price(value) -> int | None:
    if value is None:
        return None
    cleaned = _MONEY.sub("", str(value))
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _rooms(value) -> float | None:
    """Beds/baths may arrive as "3 + 1" (main + basement)."""
    if value is None:
        return None
    numbers = re.findall(r"\d+(?:\.\d+)?", str(value))
    if not numbers:
        return None
    return float(sum(float(n) for n in numbers))


def _sqft(value) -> int | None:
    if value is None:
        return None
    text = str(value)
    match = _SQFT.search(text)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    if "acre" in text.lower():
        number *= 43_560
    elif "m2" in text.lower() or "sqm" in text.lower():
        number *= 10.7639
    return int(number) if number > 0 else None


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
