"""Result ordering (LLD §7.1): one ``ResultSorter`` per ``SortOrder``, looked up in ``SORTERS``.

Every order ends with the property name and id as tie-breakers, so equal prices or ratings still page
deterministically.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import ClassVar, Final

from discovery.domain.criteria import SortOrder
from discovery.domain.offers import PropertyResult


class ResultSorter(ABC):
    order: ClassVar[SortOrder]

    def sort(self, results: Sequence[PropertyResult]) -> list[PropertyResult]:
        return sorted(results, key=lambda result: (*self.key(result), result.property_name, str(result.property_id)))

    @abstractmethod
    def key(self, result: PropertyResult) -> tuple[int, ...]:
        """The leading sort key; smaller sorts first."""


class PriceLowToHighSorter(ResultSorter):
    order: ClassVar[SortOrder] = SortOrder.PRICE_LOW_TO_HIGH

    def key(self, result: PropertyResult) -> tuple[int, ...]:
        return (result.lowest_nightly_price.paisa,)


class PriceHighToLowSorter(ResultSorter):
    order: ClassVar[SortOrder] = SortOrder.PRICE_HIGH_TO_LOW

    def key(self, result: PropertyResult) -> tuple[int, ...]:
        return (-result.lowest_nightly_price.paisa,)


class RatingHighToLowSorter(ResultSorter):
    """Highest star rating first, unrated properties last, then cheapest first."""

    order: ClassVar[SortOrder] = SortOrder.RATING_HIGH_TO_LOW

    def key(self, result: PropertyResult) -> tuple[int, ...]:
        unrated = result.star_rating is None
        return int(unrated), -(result.star_rating or 0), result.lowest_nightly_price.paisa


def _by_order(*sorters: ResultSorter) -> Mapping[SortOrder, ResultSorter]:
    return MappingProxyType({sorter.order: sorter for sorter in sorters})


SORTERS: Final[Mapping[SortOrder, ResultSorter]] = _by_order(
    PriceLowToHighSorter(), PriceHighToLowSorter(), RatingHighToLowSorter()
)
