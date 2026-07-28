"""Tests for the REALTOR.ca alert-email source.

No mailbox and no network — geocoding is disabled and messages are supplied as
bytes, so these exercise only the email-to-Listing translation.
"""

from __future__ import annotations

import pytest

from homescout.discovery.email_alerts import (
    EmailAlertSource,
    _address_from_slug,
    parse_message,
)

# A realistic alert body: table layout, entity-encoded, with the details
# following each listing link the way these emails are actually laid out.
SAMPLE_HTML = """
<html><body>
<table>
  <tr><td>
    <a href="https://www.realtor.ca/real-estate/28941234/123-main-st-sw-calgary-alberta">
      <img src="cid:photo1"/></a>
    <p>$725,000</p>
    <p>3 Bedrooms | 2 Bathrooms | 1,650 sq. ft.</p>
    <p>MLS&reg;#: A2100001</p>
  </td></tr>
  <tr><td>
    <a href="https://www.realtor.ca/real-estate/28955678/45-elbow-dr-nw-calgary-alberta">
      <img src="cid:photo2"/></a>
    <p>$649,900</p>
    <p>4 Bedrooms | 3 Bathrooms</p>
    <p>MLS&reg;#: A2100002</p>
  </td></tr>
</table>
</body></html>
"""

SAMPLE_EML = (
    "From: REALTOR.ca <noreply@realtor.ca>\r\n"
    "Subject: New listings matching your saved search\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "Content-Transfer-Encoding: quoted-printable\r\n"
    "\r\n"
) + SAMPLE_HTML


SAMPLE_EML_DATED = (
    "From: REALTOR.ca <noreply@realtor.ca>\r\n"
    "Subject: New listings matching your saved search\r\n"
    "Date: Sun, 26 Jul 2026 08:14:00 -0600\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "\r\n"
) + SAMPLE_HTML


@pytest.fixture
def source(tmp_path):
    return EmailAlertSource(inbox=tmp_path, geocode=False)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parses_listings_from_html():
    listings = parse_message(SAMPLE_HTML.encode())
    assert len(listings) == 2

    first = listings[0]
    assert first.mls_id == "A2100001"
    assert first.price == 725_000
    assert first.beds == 3.0
    assert first.baths == 2.0
    assert first.sqft == 1650
    assert first.address == "123 Main St SW"
    assert first.url.endswith("123-main-st-sw-calgary-alberta")


def test_parses_listings_from_eml_with_headers():
    listings = parse_message(SAMPLE_EML.encode())
    assert len(listings) == 2
    assert listings[1].price == 649_900


def test_missing_sqft_is_none_not_zero():
    """The second listing has no square footage; the scorer reweights around it."""
    listings = parse_message(SAMPLE_HTML.encode())
    assert listings[1].sqft is None
    assert listings[1].beds == 4.0


def test_details_are_not_taken_from_the_previous_listing():
    """Each listing's figures must come from its own block.

    A window that reached backwards would attach the first listing's price to
    the second, which is the classic failure of window-based email parsing.
    """
    listings = parse_message(SAMPLE_HTML.encode())
    assert listings[0].price == 725_000
    assert listings[1].price == 649_900


def test_quoted_printable_soft_breaks_are_repaired():
    """Real emails wrap long URLs with '=\\r\\n' soft line breaks."""
    wrapped = (
        '<a href="https://www.realtor.ca/real-estate/28941234/123-main-st-=\r\n'
        'sw-calgary">x</a><p>$725,000</p><p>3 Bedrooms 2 Bathrooms</p>'
    )
    listings = parse_message(wrapped.encode())
    assert len(listings) == 1
    assert listings[0].price == 725_000


def test_duplicate_links_collapse():
    doubled = SAMPLE_HTML + SAMPLE_HTML
    assert len(parse_message(doubled.encode())) == 2


def test_falls_back_to_url_id_when_no_mls_number():
    body = '<a href="https://www.realtor.ca/real-estate/28999999/9-test-st-se-calgary">x</a><p>$700,000</p>'
    listings = parse_message(body.encode())
    assert listings[0].mls_id == "RCA28999999"


def test_french_urls_are_recognized():
    body = '<a href="https://www.realtor.ca/immobilier/28941299/7-rue-test-sw-calgary">x</a><p>$700,000</p>'
    assert len(parse_message(body.encode())) == 1


def test_no_listings_in_unrelated_mail():
    assert parse_message(b"<html><body>Your statement is ready.</body></html>") == []


def test_empty_input():
    assert parse_message(b"") == []


# ---------------------------------------------------------------------------
# Address recovery from the URL slug
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug,expected", [
    ("123-main-st-sw-calgary-alberta", "123 Main St SW"),
    ("45-elbow-dr-nw-calgary", "45 Elbow Dr NW"),
    ("1010-5th-street-se-calgary-alberta-canada", "1010 5th Street SE"),
    ("9-deer-lane-rd-se-calgary", "9 Deer Lane Rd SE"),
])
def test_address_from_slug(slug, expected):
    assert _address_from_slug(slug) == expected


def test_address_from_slug_keeps_quadrant_uppercase():
    """The quadrant must stay uppercase — the assessment join is exact-match."""
    assert _address_from_slug("7-test-st-nw-calgary").endswith("NW")


def test_address_from_slug_empty():
    assert _address_from_slug("") == ""
    assert _address_from_slug("calgary-alberta") == ""


# ---------------------------------------------------------------------------
# The parsed address must survive normalization into the assessment key
# ---------------------------------------------------------------------------

def test_parsed_address_normalizes_to_city_parcel_format():
    """End-to-end check of the join path: URL slug -> address -> parcel key."""
    from homescout.filtering.quality import normalize_address

    listings = parse_message(SAMPLE_HTML.encode())
    assert normalize_address(listings[0].address) == "123 MAIN ST SW"
    assert normalize_address(listings[1].address) == "45 ELBOW DR NW"


# ---------------------------------------------------------------------------
# Source behaviour
# ---------------------------------------------------------------------------

def test_empty_inbox_returns_nothing(source, tmp_path):
    from homescout.models import SearchCriteria
    assert source.fetch(SearchCriteria()) == []


def test_reads_files_from_inbox(source, tmp_path):
    from homescout.models import SearchCriteria
    (tmp_path / "alert.html").write_text(SAMPLE_HTML)
    listings = source.fetch(SearchCriteria())
    assert len(listings) == 2


def test_later_email_supersedes_earlier_for_same_listing(source, tmp_path):
    """A re-alert usually signals a price change, so the newer figure wins."""
    from homescout.models import SearchCriteria

    (tmp_path / "1-old.html").write_text(SAMPLE_HTML)
    (tmp_path / "2-new.html").write_text(
        SAMPLE_HTML.replace("$725,000", "$699,000")
    )
    listings = {l.mls_id: l for l in source.fetch(SearchCriteria())}
    assert listings["A2100001"].price == 699_000


def test_unparseable_file_does_not_abort_the_run(source, tmp_path):
    from homescout.models import SearchCriteria

    (tmp_path / "bad.eml").write_bytes(b"\xff\xfe not a real message")
    (tmp_path / "good.html").write_text(SAMPLE_HTML)
    assert len(source.fetch(SearchCriteria())) == 2


# ---------------------------------------------------------------------------
# Freshness comes from the email's own Date header
# ---------------------------------------------------------------------------

def test_list_date_taken_from_message_date():
    """Alert emails carry no list date, so the send date is the freshness signal.

    Without this every listing would have an unavailable freshness sub-score.
    """
    listings = parse_message(SAMPLE_EML_DATED.encode())
    assert listings[0].list_date == "2026-07-26"


def test_bare_html_has_no_list_date():
    """A saved HTML fragment has no headers; freshness is then simply absent."""
    listings = parse_message(SAMPLE_HTML.encode())
    assert listings[0].list_date is None


def test_message_date_survives_reconciliation_into_days_on_market():
    from homescout.filtering.quality import reconcile_age

    listing = parse_message(SAMPLE_EML_DATED.encode())[0]
    reconcile_age(listing)
    assert listing.days_on_market is not None and listing.days_on_market >= 0


# ---------------------------------------------------------------------------
# Geocoding query expansion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("address,expected", [
    ("71 Edgepark Vi NW", "71 Edgepark Villas NW"),
    ("424 53 Av SW", "424 53 Avenue SW"),
    ("88 Parkland Gr SE", "88 Parkland Green SE"),
    ("12 Deer Ridge Co SE", "12 Deer Ridge Court SE"),
])
def test_expand_for_geocoding(address, expected):
    """Calgary's own abbreviations are not resolvable by Nominatim.

    Left unexpanded, these addresses fail to geocode and the listing is then
    dropped for missing coordinates.
    """
    from homescout.discovery.email_alerts import _expand_for_geocoding
    assert _expand_for_geocoding(address) == expected


def test_expansion_does_not_corrupt_street_names():
    """Only the street-type position may be expanded."""
    from homescout.discovery.email_alerts import _expand_for_geocoding
    assert _expand_for_geocoding("9 Deer Lane Rd SE") == "9 Deer Lane Road SE"


def test_expansion_leaves_unknown_types_alone():
    from homescout.discovery.email_alerts import _expand_for_geocoding
    assert _expand_for_geocoding("5 Somewhere Xyz SW") == "5 Somewhere Xyz SW"
