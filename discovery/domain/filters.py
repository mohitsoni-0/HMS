"""Search filters (LLD §7.1): the extension point for new ways to narrow a search.

A filter runs at one of two stages:

- ``INDEX``: it becomes a storage-agnostic ``Predicate`` on the listing read model. The Django repository
  translates predicates to ``Q`` objects; the in-memory test repository evaluates them in Python.
- ``OFFER``: it inspects a priced, available ``RoomOffer`` (e.g. the price range, which depends on the stay).

Adding a filter is one new ``SearchFilter`` class decorated with ``@register_filter``; neither the search
service nor the view changes.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from discovery.domain.criteria import SearchCriteria
from discovery.domain.listing import city_key
from discovery.domain.offers import RoomOffer
from hotel_commons.enum import Enum


class Op(Enum):
    EQ = 1
    IEQ = 2  # case-insensitive equality
    IN = 3
    GTE = 4
    LTE = 5
    CONTAINS_ALL = 6  # the listing's collection holds every value


@dataclass(frozen=True, slots=True)
class Predicate:
    """``<Listing field> <op> <value>``, ANDed with the other predicates of a search."""

    field: str
    op: Op
    value: object


class FilterStage(Enum):
    INDEX = 1
    OFFER = 2


class SearchFilter(ABC):
    """One way of narrowing a search. INDEX filters implement ``predicate``; OFFER filters ``matches``."""

    name: ClassVar[str]
    stage: ClassVar[FilterStage]

    @abstractmethod
    def is_active(self, criteria: SearchCriteria) -> bool:
        """Whether the criteria ask for this filter at all."""

    def predicate(self, criteria: SearchCriteria) -> Predicate:
        raise NotImplementedError(f"{self.name} is an {self.stage.name} filter and has no predicate")

    def matches(self, offer: RoomOffer, criteria: SearchCriteria) -> bool:
        raise NotImplementedError(f"{self.name} is an {self.stage.name} filter and does not inspect offers")


class FilterRegistry:
    """The filters a search applies, one instance per registered class, in registration order."""

    def __init__(self, filter_classes: Iterable[type[SearchFilter]] = ()) -> None:
        self._filters: dict[str, SearchFilter] = {}
        for filter_cls in filter_classes:
            self.register(filter_cls)

    def register(self, filter_cls: type[SearchFilter]) -> type[SearchFilter]:
        """Add ``filter_cls``; usable as a class decorator. A second class under a taken name is refused."""
        registered = self._filters.get(filter_cls.name)
        if registered is not None and type(registered) is not filter_cls:
            raise ValueError(f"A search filter named {filter_cls.name!r} is already registered")
        self._filters[filter_cls.name] = filter_cls()
        return filter_cls

    def active(self, criteria: SearchCriteria, stage: FilterStage) -> list[SearchFilter]:
        """The filters of ``stage`` the criteria ask for."""
        return [
            search_filter
            for search_filter in self._filters.values()
            if search_filter.stage is stage and search_filter.is_active(criteria)
        ]

    def copy(self) -> "FilterRegistry":
        """A registry with the same filters, to extend without touching this one."""
        return FilterRegistry(type(search_filter) for search_filter in self._filters.values())


filter_registry = FilterRegistry()
register_filter = filter_registry.register


@register_filter
class ActiveListingFilter(SearchFilter):
    name: ClassVar[str] = "active"
    stage: ClassVar[FilterStage] = FilterStage.INDEX

    def is_active(self, criteria: SearchCriteria) -> bool:
        return True

    def predicate(self, criteria: SearchCriteria) -> Predicate:
        return Predicate("is_active", Op.EQ, True)


@register_filter
class CityFilter(SearchFilter):
    name: ClassVar[str] = "city"
    stage: ClassVar[FilterStage] = FilterStage.INDEX

    def is_active(self, criteria: SearchCriteria) -> bool:
        return True

    def predicate(self, criteria: SearchCriteria) -> Predicate:
        return Predicate("city_key", Op.EQ, city_key(criteria.city))


@register_filter
class LocalityFilter(SearchFilter):
    name: ClassVar[str] = "locality"
    stage: ClassVar[FilterStage] = FilterStage.INDEX

    def is_active(self, criteria: SearchCriteria) -> bool:
        return bool(criteria.locality and criteria.locality.strip())

    def predicate(self, criteria: SearchCriteria) -> Predicate:
        return Predicate("locality", Op.IEQ, (criteria.locality or "").strip())


@register_filter
class OccupancyFilter(SearchFilter):
    name: ClassVar[str] = "occupancy"
    stage: ClassVar[FilterStage] = FilterStage.INDEX

    def is_active(self, criteria: SearchCriteria) -> bool:
        return True

    def predicate(self, criteria: SearchCriteria) -> Predicate:
        return Predicate("max_occupancy", Op.GTE, criteria.min_occupancy_per_room)


@register_filter
class StarRatingFilter(SearchFilter):
    """Unrated properties never match a minimum rating."""

    name: ClassVar[str] = "star_rating"
    stage: ClassVar[FilterStage] = FilterStage.INDEX

    def is_active(self, criteria: SearchCriteria) -> bool:
        return criteria.min_star_rating is not None

    def predicate(self, criteria: SearchCriteria) -> Predicate:
        return Predicate("star_rating", Op.GTE, criteria.min_star_rating)


@register_filter
class AmenitiesFilter(SearchFilter):
    name: ClassVar[str] = "amenities"
    stage: ClassVar[FilterStage] = FilterStage.INDEX

    def is_active(self, criteria: SearchCriteria) -> bool:
        return bool(criteria.amenities)

    def predicate(self, criteria: SearchCriteria) -> Predicate:
        return Predicate("amenity_codes", Op.CONTAINS_ALL, criteria.amenities)


@register_filter
class PropertyTypeFilter(SearchFilter):
    name: ClassVar[str] = "property_type"
    stage: ClassVar[FilterStage] = FilterStage.INDEX

    def is_active(self, criteria: SearchCriteria) -> bool:
        return bool(criteria.property_types)

    def predicate(self, criteria: SearchCriteria) -> Predicate:
        return Predicate("property_type", Op.IN, criteria.property_types)


@register_filter
class PriceRangeFilter(SearchFilter):
    """Bounds the average nightly price of the stay (inclusive), not the base price."""

    name: ClassVar[str] = "price_range"
    stage: ClassVar[FilterStage] = FilterStage.OFFER

    def is_active(self, criteria: SearchCriteria) -> bool:
        return criteria.min_price is not None or criteria.max_price is not None

    def matches(self, offer: RoomOffer, criteria: SearchCriteria) -> bool:
        price = offer.quote.average_nightly
        above_min = criteria.min_price is None or price >= criteria.min_price
        below_max = criteria.max_price is None or price <= criteria.max_price
        return above_min and below_max
