"""Property search (LLD §7.2, HLD §6.2).

1. INDEX filters become predicates and select candidate listings from the read model.
2. Live availability for the stay drops room types without enough free rooms.
3. The remaining room types are quoted for the stay.
4. OFFER filters run on the priced offers, which are grouped by property, sorted and paged.

The service knows no individual filter or sorter: both come from registries, so adding one is a new class.
Read-only, so there is no unit of work.
"""

import logging
from collections.abc import Mapping, Sequence

from discovery.domain.criteria import SearchCriteria, SortOrder
from discovery.domain.filters import FilterRegistry, FilterStage, filter_registry
from discovery.domain.listing import Listing
from discovery.domain.offers import PropertyResult, RoomOffer, SearchPage, group_by_property
from discovery.domain.sorting import SORTERS, ResultSorter
from discovery.interfaces.ports import AvailabilityPort, QuotePort
from discovery.interfaces.repositories import ListingRepository

logger = logging.getLogger(__name__)


class SearchService:
    def __init__(
        self,
        listings: ListingRepository,
        availability: AvailabilityPort,
        quotes: QuotePort,
        filters: FilterRegistry = filter_registry,
        sorters: Mapping[SortOrder, ResultSorter] = SORTERS,
        max_candidates: int = 2000,
    ) -> None:
        self._listings = listings
        self._availability = availability
        self._quotes = quotes
        self._filters = filters
        self._sorters = sorters
        self._max_candidates = max_candidates

    def search(self, criteria: SearchCriteria) -> SearchPage:
        candidates = self._candidates(criteria)
        offers = self._matching_offers(self._priced_offers(candidates, criteria), criteria)
        results = self._sorters[criteria.sort].sort(group_by_property(offers))
        logger.debug(
            "Search found %d candidates, %d offers, %d properties", len(candidates), len(offers), len(results)
        )
        return _page(results, criteria)

    def _candidates(self, criteria: SearchCriteria) -> list[Listing]:
        index_filters = self._filters.active(criteria, FilterStage.INDEX)
        candidates = self._listings.find(
            [search_filter.predicate(criteria) for search_filter in index_filters], limit=self._max_candidates
        )
        if len(candidates) >= self._max_candidates:
            logger.warning("Search hit the candidate cap of %d listings; further matches were skipped",
                           self._max_candidates)
        return candidates

    def _priced_offers(self, candidates: Sequence[Listing], criteria: SearchCriteria) -> list[RoomOffer]:
        if not candidates:
            return []
        free_rooms = self._availability.available_rooms(
            [listing.room_type_id for listing in candidates], criteria.stay, criteria.rooms
        )
        available = [listing for listing in candidates if free_rooms.get(listing.room_type_id, 0) >= criteria.rooms]
        if not available:
            return []
        quotes = self._quotes.quote_many(available, criteria.stay, criteria.rooms)
        unpriced = [listing.room_type_id for listing in available if listing.room_type_id not in quotes]
        if unpriced:
            logger.warning("Search skipped room types without a quote: %s", unpriced)
        return [
            RoomOffer(listing=listing, available_rooms=free_rooms[listing.room_type_id],
                      quote=quotes[listing.room_type_id])
            for listing in available
            if listing.room_type_id in quotes
        ]

    def _matching_offers(self, offers: Sequence[RoomOffer], criteria: SearchCriteria) -> list[RoomOffer]:
        offer_filters = self._filters.active(criteria, FilterStage.OFFER)
        return [offer for offer in offers if all(f.matches(offer, criteria) for f in offer_filters)]


def _page(results: Sequence[PropertyResult], criteria: SearchCriteria) -> SearchPage:
    start = (criteria.page - 1) * criteria.page_size
    return SearchPage(
        items=tuple(results[start : start + criteria.page_size]),
        page=criteria.page,
        page_size=criteria.page_size,
        total=len(results),
    )
