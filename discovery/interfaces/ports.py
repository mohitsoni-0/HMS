"""Ports to the contexts search reads live data from (LLD §7.2): inventory for rooms and the bookable window,
pricing for quotes, the catalog for the amenity codes a search may filter on."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from uuid import UUID

from discovery.domain.listing import Listing
from discovery.domain.offers import OfferQuote
from hotel_commons.domain.date_range import DateRange


class AvailabilityPort(ABC):
    @abstractmethod
    def available_rooms(self, room_type_ids: Sequence[UUID], stay: DateRange, rooms: int) -> dict[UUID, int]:
        """Free rooms per room type for every night of ``stay``, only for room types with at least ``rooms``."""

    @abstractmethod
    def booking_window(self) -> DateRange:
        """The stays inventory sells: first night today or later, check-out at most at the horizon's end."""


class QuotePort(ABC):
    @abstractmethod
    def quote_many(self, listings: Sequence[Listing], stay: DateRange, rooms: int) -> dict[UUID, OfferQuote]:
        """The price of ``rooms`` rooms of each listing for ``stay``, keyed by room type id."""


class AmenityCatalogPort(ABC):
    @abstractmethod
    def known_amenity_codes(self) -> frozenset[str]:
        """Codes of the amenities a listing can have."""
