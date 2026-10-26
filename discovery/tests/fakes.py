"""In-memory doubles for discovery's ports, plus builders for listings and criteria. No Django.

``InMemoryListingRepository`` evaluates the storage-agnostic predicates in Python, mirroring the ``Q``
translation of the Django repository, so the service tests exercise the real filter pipeline.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any, Final
from uuid import UUID, uuid4

from catalog.public.enums import PricingStrategyCode, PropertyType
from discovery.domain.criteria import SearchCriteria
from discovery.domain.filters import Op, Predicate
from discovery.domain.listing import Listing
from discovery.domain.offers import OfferQuote
from discovery.interfaces.ports import AvailabilityPort, QuotePort
from discovery.interfaces.repositories import ListingRepository
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money

CHECK_IN: Final = date(2026, 11, 16)  # Monday
CHECK_OUT: Final = date(2026, 11, 18)
STAY: Final = DateRange(CHECK_IN, CHECK_OUT)


def _equals_ignoring_case(actual: Any, expected: Any) -> bool:
    return isinstance(actual, str) and actual.casefold() == str(expected).casefold()


_OPERATORS: Final[Mapping[Op, Callable[[Any, Any], bool]]] = {
    Op.EQ: lambda actual, expected: actual == expected,
    Op.IEQ: _equals_ignoring_case,
    Op.IN: lambda actual, expected: actual in expected,
    Op.GTE: lambda actual, expected: actual is not None and actual >= expected,
    Op.LTE: lambda actual, expected: actual is not None and actual <= expected,
    Op.CONTAINS_ALL: lambda actual, expected: frozenset(expected) <= frozenset(actual),
}


class InMemoryListingRepository(ListingRepository):
    """Listings keyed by room type; ``find`` applies every predicate (AND) and returns them in insertion order."""

    def __init__(self, listings: Sequence[Listing] = ()) -> None:
        self.rows: dict[UUID, Listing] = {}
        self.find_calls: list[tuple[tuple[Predicate, ...], int]] = []
        self.upsert_many(listings)

    def upsert_many(self, listings: Sequence[Listing]) -> None:
        for listing in listings:
            self.rows[listing.room_type_id] = listing

    def find(self, predicates: Sequence[Predicate], limit: int) -> list[Listing]:
        self.find_calls.append((tuple(predicates), limit))
        matches = [listing for listing in self.rows.values() if _matches_all(listing, predicates)]
        return matches[:limit]


def _matches_all(listing: Listing, predicates: Sequence[Predicate]) -> bool:
    return all(_OPERATORS[predicate.op](getattr(listing, predicate.field), predicate.value) for predicate in predicates)


class FakeAvailabilityPort(AvailabilityPort):
    """Every room type has ``default_rooms`` free unless ``rooms_by_type`` says otherwise; 0 means sold out."""

    def __init__(self, rooms_by_type: Mapping[UUID, int] | None = None, *, default_rooms: int = 5) -> None:
        self.rooms_by_type = dict(rooms_by_type or {})
        self.default_rooms = default_rooms
        self.calls: list[tuple[tuple[UUID, ...], DateRange, int]] = []

    def available_rooms(self, room_type_ids: Sequence[UUID], stay: DateRange, rooms: int) -> dict[UUID, int]:
        self.calls.append((tuple(room_type_ids), stay, rooms))
        free = {key: self.rooms_by_type.get(key, self.default_rooms) for key in room_type_ids}
        return {room_type_id: count for room_type_id, count in free.items() if count >= rooms}


class FakeQuotePort(QuotePort):
    """Prices every night at the listing's base price unless ``nightly_by_type`` lists that room type's nights."""

    def __init__(self, nightly_by_type: Mapping[UUID, Sequence[Money]] | None = None) -> None:
        self.nightly_by_type = {key: tuple(value) for key, value in (nightly_by_type or {}).items()}
        self.calls: list[tuple[tuple[UUID, ...], DateRange, int]] = []

    def quote_many(self, listings: Sequence[Listing], stay: DateRange, rooms: int) -> dict[UUID, OfferQuote]:
        self.calls.append((tuple(listing.room_type_id for listing in listings), stay, rooms))
        return {listing.room_type_id: self._quote(listing, stay, rooms) for listing in listings}

    def _quote(self, listing: Listing, stay: DateRange, rooms: int) -> OfferQuote:
        prices = self.nightly_by_type.get(listing.room_type_id, (listing.base_price,) * stay.nights)
        per_room = sum(prices, Money.zero())
        return OfferQuote(
            strategy=listing.pricing_strategy,
            nightly_rates=tuple(zip(stay.dates(), prices, strict=True)),
            total=per_room.times(rooms),
            average_nightly=per_room.divide(stay.nights),
        )


def make_listing(**overrides: Any) -> Listing:
    """A bookable Goa resort room for two at ₹4,000 a night; any field can be overridden."""
    values: dict[str, Any] = {
        "room_type_id": uuid4(),
        "property_id": uuid4(),
        "property_name": "Sea Breeze Resort",
        "property_type": PropertyType.RESORT,
        "star_rating": 4,
        "city": "Goa",
        "city_key": "goa",
        "locality": "Candolim",
        "amenity_codes": frozenset({"WIFI", "POOL"}),
        "room_type_name": "Deluxe",
        "max_occupancy": 2,
        "base_price": Money.of_rupees(4000),
        "pricing_strategy": PricingStrategyCode.STANDARD,
        "is_active": True,
    }
    values.update(overrides)
    return Listing(**values)


def make_criteria(**overrides: Any) -> SearchCriteria:
    """Two guests in Goa for two nights; any field can be overridden."""
    values: dict[str, Any] = {"city": "Goa", "stay": STAY, "guests": 2}
    values.update(overrides)
    return SearchCriteria(**values)
