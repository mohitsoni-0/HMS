"""Catalog events -> listing projection: the anti-corruption layer between the catalog and discovery.

``CatalogEventSubscriber`` handles ``PropertyOnboarded`` and ``RoomTypeAdded`` by mapping the event's
``PropertySnapshot`` to discovery's ``Listing`` values and handing them to ``ListingProjector``, so a change
in the catalog's DTOs stops here and never reaches discovery's services.
"""

from catalog.public.dto import PropertySnapshot, RoomTypeSummary
from catalog.public.events import PropertyOnboarded, RoomTypeAdded
from discovery.domain.listing import Listing, city_key
from discovery.services.listing_projector import ListingProjector


class CatalogEventSubscriber:
    def __init__(self, projector: ListingProjector) -> None:
        self._projector = projector

    def on_property_onboarded(self, event: PropertyOnboarded) -> None:
        self._project(event.property)

    def on_room_type_added(self, event: RoomTypeAdded) -> None:
        self._project(event.property)

    def _project(self, snapshot: PropertySnapshot) -> None:
        self._projector.project(snapshot.property_id, listings_of(snapshot))


def listings_of(snapshot: PropertySnapshot) -> list[Listing]:
    """One ``Listing`` per room type of the property."""
    return [_listing(snapshot, room_type) for room_type in snapshot.room_types]


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
