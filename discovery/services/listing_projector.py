"""Keeps the listing read model in step with the catalog (LLD §7.2, HLD §6.1).

The catalog's events reach ``discovery.adapters.catalog_events``, which maps each property snapshot to
discovery's own ``Listing`` values and calls ``project``; this service knows nothing of the catalog. The
adapter is subscribed to the in-process event bus in ``DiscoveryConfig.ready()``, so a projection runs inside
the catalog's transaction and commits or rolls back with it. Every event re-projects every room type of its
property: upserts are idempotent and property-level changes reach every listing.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from discovery.domain.listing import Listing
from discovery.interfaces.repositories import ListingRepository

logger = logging.getLogger(__name__)


class ListingProjector:
    def __init__(self, listings: ListingRepository) -> None:
        self._listings = listings

    def project(self, property_id: UUID, listings: Sequence[Listing]) -> None:
        """Insert or replace the listings of one property (one per room type)."""
        self._listings.upsert_many(listings)
        logger.info("Projected %d listings for property %s", len(listings), property_id)
