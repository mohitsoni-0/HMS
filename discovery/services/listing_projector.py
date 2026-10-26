"""Keeps the listing read model in step with the catalog (LLD §7.2, HLD §6.1).

Subscribed to the in-process event bus in ``DiscoveryConfig.ready()``, so a projection runs inside the
catalog's transaction and commits or rolls back with it. Each event carries the whole property snapshot,
and every room type of it is re-projected: upserts are idempotent and property-level changes reach every
listing.
"""

import logging

from catalog.public.dto import PropertySnapshot, RoomTypeSummary
from catalog.public.events import PropertyOnboarded, RoomTypeAdded
from discovery.domain.listing import Listing, city_key
from discovery.interfaces.repositories import ListingRepository

logger = logging.getLogger(__name__)


class ListingProjector:
    def __init__(self, listings: ListingRepository) -> None:
        self._listings = listings

    def on_property_onboarded(self, event: PropertyOnboarded) -> None:
        self._project(event.property)

    def on_room_type_added(self, event: RoomTypeAdded) -> None:
        self._project(event.property)

    def _project(self, snapshot: PropertySnapshot) -> None:
        listings = [_listing(snapshot, room_type) for room_type in snapshot.room_types]
        self._listings.upsert_many(listings)
        logger.info("Projected %d listings for property %s", len(listings), snapshot.property_id)


def _listing(snapshot: PropertySnapshot, room_type: RoomTypeSummary) -> Listing:
    return Listing(
        room_type_id=room_type.room_type_id,
        property_id=snapshot.property_id,
        property_name=snapshot.name,
        property_type=snapshot.property_type,
        star_rating=snapshot.star_rating,
        city=snapshot.city.strip(),
        city_key=city_key(snapshot.city),
        locality=snapshot.locality,
        amenity_codes=snapshot.amenity_codes | room_type.amenity_codes,
        room_type_name=room_type.name,
        max_occupancy=room_type.max_occupancy,
        base_price=room_type.base_price,
        pricing_strategy=snapshot.pricing_strategy,
        is_active=snapshot.is_active and room_type.is_active,
    )
