"""The ListingSource seam.

Every source emits `Listing` objects with the same normalized fields, so
swapping a source — scraper, licensed MLS feed, paid API — changes nothing
downstream. Register new sources in SOURCES below.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from homescout.models import Listing, SearchCriteria

log = logging.getLogger(__name__)


@runtime_checkable
class ListingSource(Protocol):
    """Contract every listing source must satisfy."""

    name: str

    def fetch(self, criteria: SearchCriteria) -> list[Listing]:
        """Return listings matching the criteria, best-effort.

        Sources should push filters upstream where the provider supports it,
        but must not assume they were honored — the filter stage re-applies
        every criterion locally.
        """
        ...


def get_source(name: str) -> ListingSource:
    """Resolve a source by name. Imports lazily so that a missing optional
    dependency (e.g. Playwright) only breaks the source that needs it."""
    name = name.strip().lower()

    if name == "fixtures":
        from homescout.discovery.fixtures import FixtureSource
        return FixtureSource()

    if name in ("realtor_ca", "realtor.ca", "realtorca"):
        from homescout.discovery.realtor_ca import RealtorCaSource
        return RealtorCaSource()

    raise ValueError(f"Unknown listing source: {name!r}. Available: {', '.join(available_sources())}")


def available_sources() -> list[str]:
    return ["realtor_ca", "fixtures"]
