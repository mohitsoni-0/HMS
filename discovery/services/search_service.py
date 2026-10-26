"""Property search (LLD §7.2, HLD §6.2).

0. A stay outside inventory's booking window (first night before today, check-out past the horizon) is
   refused with VALIDATION_FAILED: nothing outside it can be booked.
1. INDEX filters become predicates and select candidate listings from the read model, ``batch_size`` at a
   time (keyset paging), so every candidate of the city is considered and none is silently dropped.
2. For each batch, live availability for the stay drops room types without enough free rooms.
3. The remaining room types of the batch are quoted for the stay, and OFFER filters run on the priced offers.
4. The offers of all batches are grouped by property, sorted and paged, so totals and order are exact.

The service knows no individual filter or sorter: both come from registries, so adding one is a new class.
Read-only, so there is no unit of work.
"""

import logging
from collections.abc import Iterator, Mapping, Sequence
from uuid import UUID

from discovery.domain.criteria import SearchCriteria, SortOrder
from discovery.domain.filters import FilterRegistry, FilterStage, Predicate, filter_registry
from discovery.domain.listing import Listing
from discovery.domain.offers import PropertyResult, RoomOffer, SearchPage, group_by_property
from discovery.domain.sorting import SORTERS, ResultSorter
from discovery.interfaces.ports import AvailabilityPort, QuotePort
from discovery.interfaces.repositories import ListingRepository
from hotel_commons.domain.date_range import DateRange
from hotel_commons.exceptions import ValidationFailed
from hotel_commons.status_code import VALIDATION_FAILED

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500  # candidate listings per availability query and pricing batch
LARGE_SEARCH_CANDIDATES = 5000  # beyond this many candidates a search is logged as a WARNING


class SearchService:
    def __init__(
        self,
        listings: ListingRepository,
        availability: AvailabilityPort,
        quotes: QuotePort,
        filters: FilterRegistry = filter_registry,
        sorters: Mapping[SortOrder, ResultSorter] = SORTERS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        large_search_candidates: int = LARGE_SEARCH_CANDIDATES,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._listings = listings
        self._availability = availability
        self._quotes = quotes
        self._filters = filters
        self._sorters = sorters
        self._batch_size = batch_size
        self._large_search_candidates = large_search_candidates

    def search(self, criteria: SearchCriteria) -> SearchPage:
        """The page of ``criteria``; ValidationFailed(VALIDATION_FAILED) for a stay outside the booking window."""
        self._require_bookable(criteria.stay)
        offers, candidates = self._offers(criteria)
        results = self._sorters[criteria.sort].sort(group_by_property(offers))
        if candidates > self._large_search_candidates:
            logger.warning("Search read %d candidate listings in batches of %d", candidates, self._batch_size)
        logger.debug("Search found %d candidates, %d offers, %d properties", candidates, len(offers), len(results))
        return _page(results, criteria)

    def _require_bookable(self, stay: DateRange) -> None:
        window = self._availability.booking_window()
        errors: dict[str, list[str]] = {}
        if stay.start < window.start:
            errors["check_in"] = ["check_in cannot be in the past."]
        if stay.end > window.end:
            errors["check_out"] = [f"check_out can be at most {window.nights} days from today."]
        if errors:
            raise ValidationFailed(VALIDATION_FAILED, errors=errors)

    def _offers(self, criteria: SearchCriteria) -> tuple[list[RoomOffer], int]:
        """The matching offers of every candidate, and how many candidates there were."""
        index_filters = self._filters.active(criteria, FilterStage.INDEX)
        predicates = [search_filter.predicate(criteria) for search_filter in index_filters]
        offers: list[RoomOffer] = []
        candidates = 0
        for batch in self._candidate_batches(predicates):
            candidates += len(batch)
            offers.extend(self._matching_offers(self._priced_offers(batch, criteria), criteria))
        return offers, candidates

    def _candidate_batches(self, predicates: Sequence[Predicate]) -> Iterator[list[Listing]]:
        after: tuple[UUID, UUID] | None = None
        while True:
            batch = self._listings.find(predicates, limit=self._batch_size, after=after)
            if batch:
                yield batch
            if len(batch) < self._batch_size:
                return
            after = (batch[-1].property_id, batch[-1].room_type_id)

    def _priced_offers(self, candidates: Sequence[Listing], criteria: SearchCriteria) -> list[RoomOffer]:
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
