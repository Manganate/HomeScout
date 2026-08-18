"""Normalized data models shared across every pipeline stage.

`Listing` is the contract between sources and the rest of the pipeline: any
ListingSource must emit these fields, so swapping a source changes nothing
downstream. Enrichment fields start as None and are filled by the analyze stage.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime

# ---------------------------------------------------------------------------
# Search criteria
# ---------------------------------------------------------------------------

@dataclass
class SearchCriteria:
    """User search parameters, loaded from search.yaml."""

    area: str = ""
    cbd_lat: float = 0.0
    cbd_lon: float = 0.0

    price_min: int = 600_000
    price_max: int = 800_000
    beds_min: int = 3
    beds_max: int | None = None
    baths_min: int = 2
    baths_max: int | None = None
    sqft_min: int | None = None
    sqft_max: int | None = None

    max_commute_min: int = 20
    commute_mode: str = "driving"
    max_listing_age_days: int = 30

    property_types: list[str] = field(default_factory=lambda: ["House", "Townhouse", "Duplex"])

    # Ranking weights — normalized at load time so they always sum to 1.0.
    weight_value: float = 0.34
    weight_location: float = 0.33
    weight_freshness: float = 0.33

    def __post_init__(self) -> None:
        total = self.weight_value + self.weight_location + self.weight_freshness
        if total <= 0:
            self.weight_value = self.weight_location = self.weight_freshness = 1 / 3
        else:
            self.weight_value /= total
            self.weight_location /= total
            self.weight_freshness /= total

    @property
    def geo_bound_km(self) -> float:
        """Generous haversine bound for the Phase-A distance pre-filter.

        The criterion is minutes, but the cheap signal is kilometres. Assume an
        optimistic speed ceiling so this only discards listings that *cannot*
        qualify; real drive time is computed later on the survivors.
        """
        speed_kmh = 80.0 if self.commute_mode == "driving" else 40.0
        return (self.max_commute_min / 60.0) * speed_kmh

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    """A single property listing, normalized across sources."""

    # -- Identity / source --
    mls_id: str
    source: str
    url: str

    # -- Address --
    address: str
    city: str = ""
    postal_code: str = ""

    # -- Core facts --
    price: int | None = None
    beds: float | None = None
    baths: float | None = None
    sqft: int | None = None
    lot_sqft: int | None = None
    property_type: str = ""

    # -- Timing --
    list_date: str | None = None          # ISO date string
    days_on_market: int | None = None

    # -- Geo --
    latitude: float | None = None
    longitude: float | None = None

    # -- Enrichment (filled by the analyze stage) --
    distance_km: float | None = None
    commute_min: float | None = None
    commute_estimated: bool = False       # True when OSRM was unavailable
    walk_proxy: float | None = None
    schools_n: int | None = None
    cafes_n: int | None = None
    parks_n: int | None = None
    groceries_n: int | None = None
    transit_n: int | None = None
    assessed_value: int | None = None
    assessment_ratio: float | None = None

    # -- Scoring (filled by the rank stage) --
    score_value: float | None = None
    score_location: float | None = None
    score_freshness: float | None = None
    score_total: float | None = None
    data_completeness: float | None = None
    explanation: str = ""

    @property
    def price_per_sqft(self) -> float | None:
        if not self.price or not self.sqft or self.sqft <= 0:
            return None
        return self.price / self.sqft

    def to_row(self) -> dict:
        """Flatten to a dict suitable for a SQLite upsert."""
        row = asdict(self)
        row["price_per_sqft"] = self.price_per_sqft
        return row


def days_since(iso_date: str | None) -> int | None:
    """Whole days between an ISO date string and today (UTC). None if unparseable."""
    if not iso_date:
        return None
    text = str(iso_date).strip()
    if not text:
        return None
    # Tolerate full timestamps, trailing Z, and bare dates.
    text = text.replace("Z", "+00:00")
    parsed: date | None = None
    try:
        parsed = datetime.fromisoformat(text).date()
    except ValueError:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(text[:10], fmt).date()
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    delta = (datetime.now(UTC).date() - parsed).days
    return max(delta, 0)
