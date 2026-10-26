"""Search results (LLD §7.1): priced, available room offers grouped by property, and the page of them.

``OfferQuote`` is discovery's own view of a price (an anti-corruption layer), so a change to pricing's DTOs
stops at the quote adapter.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from catalog.public.enums import PricingStrategyCode, PropertyType
from discovery.domain.listing import Listing
from hotel_commons.domain.money import Money


@dataclass(frozen=True, slots=True)
class OfferQuote:
    strategy: PricingStrategyCode
    nightly_rates: tuple[tuple[date, Money], ...]
    total: Money
    average_nightly: Money


@dataclass(frozen=True, slots=True)
class RoomOffer:
    listing: Listing
    available_rooms: int
    quote: OfferQuote


@dataclass(frozen=True, slots=True)
class PropertyResult:
    """One property with its matching offers, cheapest first; ``offers`` is never empty."""

    property_id: UUID
    property_name: str
    property_type: PropertyType
    star_rating: int | None
    city: str
    locality: str
    amenity_codes: frozenset[str]
    offers: tuple[RoomOffer, ...]

    @property
    def lowest_nightly_price(self) -> Money:
        """The lowest average nightly price among the offers."""
        return min(offer.quote.average_nightly for offer in self.offers)


@dataclass(frozen=True, slots=True)
class SearchPage:
    items: tuple[PropertyResult, ...]
    page: int
    page_size: int
    total: int


def group_by_property(offers: Iterable[RoomOffer]) -> list[PropertyResult]:
    """One ``PropertyResult`` per property, in order of first appearance.

    A result's amenities are those every one of its offers comes with: the property's own plus any the
    offered room types share.
    """
    by_property: dict[UUID, list[RoomOffer]] = {}
    for offer in offers:
        by_property.setdefault(offer.listing.property_id, []).append(offer)
    return [_property_result(property_offers) for property_offers in by_property.values()]


def _property_result(offers: list[RoomOffer]) -> PropertyResult:
    ordered = tuple(sorted(offers, key=_offer_order))
    first = ordered[0].listing
    return PropertyResult(
        property_id=first.property_id,
        property_name=first.property_name,
        property_type=first.property_type,
        star_rating=first.star_rating,
        city=first.city,
        locality=first.locality,
        amenity_codes=frozenset.intersection(*(offer.listing.amenity_codes for offer in ordered)),
        offers=ordered,
    )


def _offer_order(offer: RoomOffer) -> tuple[int, str, str]:
    return offer.quote.average_nightly.paisa, offer.listing.room_type_name, str(offer.listing.room_type_id)
