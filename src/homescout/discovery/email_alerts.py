"""Listing source: REALTOR.ca saved-search alert emails.

You create saved searches on REALTOR.ca and it emails you matching listings.
This source reads those emails. Nothing is scraped and no access control is
worked around — the site is deliberately sending you this data, and it lands in
your own mailbox.

Two ways to supply the messages:

  1. Drop the emails into ~/.homescout/inbox/ as .eml or .html files
     (in most mail clients, drag the message to a Finder window, or
     File > Save As). No credentials involved.

  2. Let HomeScout fetch them over IMAP, configured in .env. Read-only:
     messages are downloaded, never deleted or altered.

Alert emails carry address, price, beds/baths and the listing link, but no
coordinates — so addresses are geocoded through the cached Nominatim helper.
That is also enough for the municipal assessment join, which keys on address.
"""

from __future__ import annotations

import email
import html as html_module
import logging
import os
import re
from email import policy
from pathlib import Path

from homescout.config import APP_DIR
from homescout.models import Listing, SearchCriteria

log = logging.getLogger(__name__)

INBOX_DIR = APP_DIR / "inbox"

# A REALTOR.ca detail link carries the MLS id and an address slug, which makes
# it the most reliable anchor in an email whose layout changes freely.
_DETAIL_LINK = re.compile(
    r"https?://(?:www\.)?realtor\.ca/(?:real-estate|immobilier)/(\d+)/([a-z0-9\-]+)",
    re.IGNORECASE,
)

_PRICE = re.compile(r"\$\s?([\d,]{6,12})")
_BEDS = re.compile(r"(\d+(?:\s*\+\s*\d+)?)\s*(?:bed|bdrm|bedroom|chambre)", re.IGNORECASE)
_BATHS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bath|bthrm|bathroom|salle)", re.IGNORECASE)
_SQFT = re.compile(r"([\d,]{3,7})\s*(?:sq\.?\s?ft|sqft|square feet|pi\.?\s?ca)", re.IGNORECASE)
_MLS = re.compile(r"MLS[®\s#:]*([A-Z]?\d{6,9})", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
# Matches the whole anchor tag, so substituting it leaves the URL as plain text
# *outside* any angle brackets. Replacing only the href attribute would leave
# the URL inside the tag, where the tag stripper below would delete it.
_ANCHOR = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>""", re.IGNORECASE)

# How much text around a listing link to search for that listing's details.
_WINDOW = 1400


class EmailAlertSource:
    """Parses REALTOR.ca alert emails into listings."""

    name = "email"

    def __init__(self, inbox: Path | None = None, geocode: bool = True) -> None:
        self.inbox = inbox or INBOX_DIR
        self.geocode = geocode

    def fetch(self, criteria: SearchCriteria) -> list[Listing]:
        self.inbox.mkdir(parents=True, exist_ok=True)

        if _imap_configured():
            try:
                n = fetch_via_imap(self.inbox)
                if n:
                    log.info("Downloaded %d new alert email(s) over IMAP", n)
            except Exception as exc:
                log.warning("IMAP fetch failed (%s); using whatever is already in the inbox", exc)

        files = sorted(
            [p for p in self.inbox.iterdir() if p.suffix.lower() in (".eml", ".html", ".htm", ".txt")]
        )
        if not files:
            log.warning(
                "No alert emails found in %s. Save REALTOR.ca alert emails there as .eml or "
                ".html files, or configure IMAP in .env.", self.inbox,
            )
            return []

        listings: dict[str, Listing] = {}
        for path in files:
            try:
                for listing in parse_message(path.read_bytes(), source=self.name, area=criteria.area):
                    # Later emails win: a re-alert usually means a price change.
                    listings[listing.mls_id] = listing
            except Exception as exc:
                log.warning("Could not parse %s: %s", path.name, exc)

        found = list(listings.values())
        log.info("Parsed %d listing(s) from %d email file(s)", len(found), len(files))

        if self.geocode:
            _geocode_all(found, area=criteria.area)

        return found


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_message(raw: bytes, source: str = "email", area: str = "") -> list[Listing]:
    """Extract listings from one saved email (.eml) or raw HTML."""
    text = _to_text(raw)
    if not text:
        return []

    # The email's own date is the freshness signal: an alert means "new as of
    # when this was sent". Nothing in the body carries a list date, so without
    # this the freshness sub-score would be unavailable for every listing.
    sent = _message_date(raw)
    city = area.split(",")[0].strip() if area else ""

    listings = []
    seen: set[str] = set()

    for match in _DETAIL_LINK.finditer(text):
        listing_id, slug = match.group(1), match.group(2)
        if listing_id in seen:
            continue
        seen.add(listing_id)

        # Search the text following the link for this listing's details; email
        # layouts put the figures after the anchor far more often than before.
        window = text[match.start(): match.start() + _WINDOW]

        listing = Listing(
            mls_id=_mls_id(window, listing_id),
            source=source,
            url=match.group(0),
            address=_address_from_slug(slug, area),
            city=city,
            price=_first_int(_PRICE, window),
            beds=_rooms(_BEDS, window),
            baths=_rooms(_BATHS, window),
            sqft=_first_int(_SQFT, window),
            property_type="",
            list_date=sent,
        )
        listings.append(listing)

    return listings


def _message_date(raw: bytes) -> str | None:
    """ISO date from the email's Date header, or None for a bare HTML file."""
    head = raw[:4000].lower()
    if b"date:" not in head and b"from:" not in head:
        return None
    try:
        message = email.message_from_bytes(raw, policy=policy.default)
        sent = message.get("Date")
        if not sent:
            return None
        parsed = email.utils.parsedate_to_datetime(sent)
        return parsed.date().isoformat()
    except Exception as exc:
        log.debug("Could not read message date: %s", exc)
        return None


def _to_text(raw: bytes) -> str:
    """Decode an .eml or raw HTML payload to searchable plain text."""
    body = raw

    # .eml files start with headers; raw HTML does not.
    head = raw[:2000].lower()
    if b"content-type:" in head or b"subject:" in head or b"from:" in head:
        try:
            message = email.message_from_bytes(raw, policy=policy.default)
            parts = []
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get_content_type() in ("text/html", "text/plain"):
                    try:
                        parts.append(part.get_content())
                    except Exception:
                        payload = part.get_payload(decode=True) or b""
                        parts.append(payload.decode("utf-8", "replace"))
            body = "\n".join(parts).encode("utf-8", "replace")
        except Exception as exc:
            log.debug("Falling back to raw decode: %s", exc)

    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body

    # Emails wrap URLs across lines and encode entities; normalize before regex.
    text = text.replace("=\r\n", "").replace("=\n", "")
    text = text.replace("=3D", "=").replace("&amp;", "&")
    text = html_module.unescape(text)

    # Listing URLs live inside href attributes, so each anchor tag is replaced
    # by its URL as plain text before tags are stripped — otherwise tag removal
    # deletes every link and the parser finds nothing at all.
    text = _ANCHOR.sub(r" \1 ", text)

    # Keep tag boundaries as whitespace so adjacent fields don't run together.
    text = _TAG.sub(" ", text)
    return re.sub(r"[ \t]+", " ", text)


_PROVINCE_ABBREV = {"ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt"}


def _address_from_slug(slug: str, area: str = "") -> str:
    """Recover a street address from a REALTOR.ca URL slug.

    "123-main-st-sw-city-province" -> "123 Main St SW". The trailing city and
    province are dropped so the result matches the municipal parcel index,
    which is keyed on street address alone.
    """
    parts = [p for p in slug.split("-") if p]
    drop = {"canada"} | _PROVINCE_ABBREV
    drop.update(w.lower() for w in re.split(r"[\s,]+", area) if w)
    while parts and parts[-1].lower() in drop:
        parts.pop()
    if not parts:
        return ""

    words = []
    for part in parts:
        upper = part.upper()
        words.append(upper if upper in ("SW", "NW", "SE", "NE") else part.capitalize())
    return " ".join(words)


def _mls_id(window: str, fallback: str) -> str:
    match = _MLS.search(window)
    if match:
        return match.group(1).upper()
    # The numeric listing id from the URL is stable and unique when no MLS
    # number appears in the email body.
    return f"RCA{fallback}"


def _first_int(pattern: re.Pattern, text: str) -> int | None:
    match = pattern.search(text)
    if not match:
        return None
    try:
        value = int(match.group(1).replace(",", "").replace(" ", ""))
    except ValueError:
        return None
    return value or None


def _rooms(pattern: re.Pattern, text: str) -> float | None:
    """Handle "3", "2.5", and "3 + 1" (main plus basement)."""
    match = pattern.search(text)
    if not match:
        return None
    numbers = re.findall(r"\d+(?:\.\d+)?", match.group(1))
    if not numbers:
        return None
    return float(sum(float(n) for n in numbers))


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def _expand_for_geocoding(address: str) -> str:
    """Expand the source municipality's street-type abbreviations into words Nominatim knows.

    Derived by inverting the abbreviation table used for the parcel-assessment
    join, so the two never drift apart. Only the street-type position is
    touched — expanding every word would corrupt street *names* that happen to
    contain a type word ("Deer Lane Rd").
    """
    from homescout.filtering.quality import _QUADRANT_ABBREV

    words = address.split()
    if len(words) < 2:
        return address

    tail = len(words) - 1
    if words[tail].lower() in _QUADRANT_ABBREV:
        tail -= 1

    if tail >= 1:
        expansion = _EXPANSIONS.get(words[tail].lower())
        if expansion:
            words[tail] = expansion.capitalize()

    return " ".join(words)


# Abbreviation -> full word. Built by inverting the join table; where several
# spellings map to one abbreviation the longest is used, which is the form
# Nominatim indexes.
def _build_expansions() -> dict[str, str]:
    from homescout.filtering.quality import _STREET_TYPES

    out: dict[str, str] = {}
    for word, abbrev in _STREET_TYPES.items():
        if abbrev not in out or len(word) > len(out[abbrev]):
            out[abbrev] = word
    return out


_EXPANSIONS = _build_expansions()


def _geocode_all(listings: list[Listing], area: str = "") -> None:
    """Fill in coordinates from addresses, cached and rate-limited.

    Alert emails carry no coordinates, but the distance pre-filter and commute
    routing both need them. Nominatim allows 1 request/second; results are
    cached in SQLite, so this cost is paid once per address.
    """
    from homescout.database import get_connection
    from homescout.enrichment.geo import geocode

    pending = [l for l in listings if l.latitude is None and l.address]
    if not pending:
        return

    log.info("Geocoding %d address(es) (1/sec, cached after the first run)...", len(pending))
    conn = get_connection()
    resolved = 0
    try:
        for listing in pending:
            locality = listing.city or area
            # Try the expanded form first: REALTOR.ca slugs keep the source
            # municipality's own abbreviations ("VI" for Villas, "GR" for
            # Green), which Nominatim does not recognize, so the raw address
            # alone silently fails to resolve and the listing is then dropped
            # for missing coordinates.
            candidates = [
                f"{_expand_for_geocoding(listing.address)}, {locality}",
                f"{listing.address}, {locality}",
            ]
            for query in dict.fromkeys(candidates):
                coords = geocode(query, conn=conn)
                if coords:
                    listing.latitude, listing.longitude = coords
                    resolved += 1
                    break
            else:
                log.debug("Could not geocode %r", listing.address)
    finally:
        conn.close()

    if resolved < len(pending):
        log.warning(
            "Geocoded %d/%d addresses; the rest are dropped by the quality "
            "validator since distance cannot be computed without coordinates.",
            resolved, len(pending),
        )


# ---------------------------------------------------------------------------
# IMAP (optional)
# ---------------------------------------------------------------------------

def _imap_configured() -> bool:
    return bool(os.environ.get("HOMESCOUT_IMAP_USER") and os.environ.get("HOMESCOUT_IMAP_PASSWORD"))


def fetch_via_imap(inbox: Path) -> int:
    """Download unread REALTOR.ca alert emails into the inbox directory.

    Read-only with respect to your mailbox: messages are fetched with the
    PEEK flag so they are not marked as read, and nothing is deleted.
    """
    import imaplib

    host = os.environ.get("HOMESCOUT_IMAP_HOST", "imap.gmail.com")
    user = os.environ["HOMESCOUT_IMAP_USER"]
    password = os.environ["HOMESCOUT_IMAP_PASSWORD"]
    folder = os.environ.get("HOMESCOUT_IMAP_FOLDER", "INBOX")
    sender = os.environ.get("HOMESCOUT_IMAP_FROM", "realtor.ca")

    inbox.mkdir(parents=True, exist_ok=True)
    downloaded = 0

    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, password)
        conn.select(folder, readonly=True)

        status, data = conn.search(None, f'(FROM "{sender}")')
        if status != "OK":
            return 0

        ids = (data[0] or b"").split()
        for msg_id in ids[-50:]:  # most recent 50
            target = inbox / f"imap-{msg_id.decode()}.eml"
            if target.exists():
                continue
            status, payload = conn.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not payload or not payload[0]:
                continue
            target.write_bytes(payload[0][1])
            downloaded += 1
    finally:
        try:
            conn.logout()
        except Exception as exc:
            log.debug("IMAP logout failed: %s", exc)

    return downloaded
