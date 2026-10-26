"""The search read model (LLD §7.1): one ``Listing`` per room type, denormalised from the catalog."""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from catalog.public.enums import PricingStrategyCode, PropertyType
from hotel_commons.domain.money import Money

MAX_AMENITY_CODE_LENGTH: Final = 40


@dataclass(frozen=True, slots=True)
class Listing:
    """A room type with the property facts search filters on.

    ``amenity_codes`` holds the property's amenities and the room type's own, so an amenity filter matches
    a room that has WIFI at a property with a POOL. ``is_active`` is false when either the property or the
    room type is inactive.
    """

    room_type_id: UUID
    property_id: UUID
    property_name: str
    property_type: PropertyType
    star_rating: int | None
    city: str
    city_key: str
    locality: str
    amenity_codes: frozenset[str]
    room_type_name: str
    max_occupancy: int
    base_price: Money
    pricing_strategy: PricingStrategyCode
    is_active: bool


def city_key(city: str) -> str:
    """The normalised city a listing is indexed and searched by: " Goa " -> "goa"."""
    return city.strip().lower()
