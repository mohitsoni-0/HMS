"""Unit tests for the search pipeline (LLD §7, §14): filters, availability, quotes, grouping, sorting, paging.

Pure Python: in-memory listings and fake availability and quote ports, no Django.
"""

import logging
from datetime import timedelta
from typing import ClassVar
from uuid import UUID

import pytest

from catalog.public.enums import PricingStrategyCode, PropertyType
from discovery.domain.criteria import SearchCriteria, SortOrder
from discovery.domain.filters import FilterStage, Op, Predicate, SearchFilter, filter_registry
from discovery.domain.listing import Listing
from discovery.domain.offers import SearchPage
from discovery.domain.sorting import SORTERS
from discovery.services.search_service import SearchService
from discovery.status_code import INVALID_SEARCH
from discovery.tests.fakes import (
    STAY,
    TODAY,
    WINDOW,
    FakeAvailabilityPort,
    FakeQuotePort,
    InMemoryListingRepository,
    make_criteria,
    make_listing,
)
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money
from hotel_commons.exceptions import ValidationFailed
from hotel_commons.status_code import VALIDATION_FAILED


def _rupees(amount: int) -> Money:
    return Money.of_rupees(amount)


def _service(
    *listings: Listing,
    availability: FakeAvailabilityPort | None = None,
    quotes: FakeQuotePort | None = None,
    **kwargs: object,
) -> SearchService:
    return SearchService(
        listings=InMemoryListingRepository(listings),
        availability=availability or FakeAvailabilityPort(),
        quotes=quotes or FakeQuotePort(),
        **kwargs,
    )


def _room_type_ids(page: SearchPage) -> set[UUID]:
    return {offer.listing.room_type_id for result in page.items for offer in result.offers}


def _property_names(page: SearchPage) -> list[str]:
    return [result.property_name for result in page.items]


def test_city_and_occupancy_filters() -> None:
    double = make_listing(max_occupancy=2)
    family = make_listing(max_occupancy=4)
    other_city = make_listing(city="Mumbai", city_key="mumbai", max_occupancy=4)
    inactive = make_listing(max_occupancy=4, is_active=False)
    service = _service(double, family, other_city, inactive)

    three_guests = service.search(make_criteria(city="  GOA ", guests=3))
    four_guests_two_rooms = service.search(make_criteria(guests=4, rooms=2))

    assert _room_type_ids(three_guests) == {family.room_type_id}
    assert _room_type_ids(four_guests_two_rooms) == {double.room_type_id, family.room_type_id}


def test_locality_star_rating_and_property_type_filters() -> None:
    match = make_listing(locality="Candolim", star_rating=4, property_type=PropertyType.RESORT)
    wrong_locality = make_listing(locality="Baga", star_rating=5, property_type=PropertyType.RESORT)
    unrated = make_listing(locality="Candolim", star_rating=None, property_type=PropertyType.RESORT)
    wrong_type = make_listing(locality="Candolim", star_rating=5, property_type=PropertyType.HOSTEL)
    service = _service(match, wrong_locality, unrated, wrong_type)

    page = service.search(
        make_criteria(
            locality="candolim",
            min_star_rating=4,
            property_types=frozenset({PropertyType.RESORT, PropertyType.HOTEL}),
        )
    )

    assert _room_type_ids(page) == {match.room_type_id}


def test_amenities_contains_all() -> None:
    full = make_listing(amenity_codes=frozenset({"WIFI", "POOL", "SPA"}))
    partial = make_listing(amenity_codes=frozenset({"WIFI"}))
    service = _service(full, partial)

    page = service.search(make_criteria(amenities=frozenset({"WIFI", "POOL"})))

    assert _room_type_ids(page) == {full.room_type_id}


def test_unavailable_room_types_excluded() -> None:
    hotel_id = UUID(int=1)
    free = make_listing(property_id=hotel_id, room_type_name="Deluxe")
    sold_out = make_listing(property_id=hotel_id, room_type_name="Suite")
    sold_out_elsewhere = make_listing(property_name="Sold Out Inn")
    availability = FakeAvailabilityPort({sold_out.room_type_id: 0, sold_out_elsewhere.room_type_id: 0})
    quotes = FakeQuotePort()
    service = _service(free, sold_out, sold_out_elsewhere, availability=availability, quotes=quotes)

    page = service.search(make_criteria(rooms=1))

    assert _room_type_ids(page) == {free.room_type_id}
    assert page.total == 1
    assert page.items[0].offers[0].available_rooms == 5
    asked_ids, asked_stay, asked_rooms = availability.calls[0]
    assert set(asked_ids) == {free.room_type_id, sold_out.room_type_id, sold_out_elsewhere.room_type_id}
    assert (asked_stay, asked_rooms) == (STAY, 1)
    assert quotes.calls == [((free.room_type_id,), STAY, 1)]


def test_no_candidates_skips_availability_and_quotes() -> None:
    availability = FakeAvailabilityPort()
    quotes = FakeQuotePort()
    service = _service(make_listing(), availability=availability, quotes=quotes)

    page = service.search(make_criteria(city="Pune"))

    assert page == SearchPage(items=(), page=1, page_size=20, total=0)
    assert availability.calls == []
    assert quotes.calls == []


def test_price_range_uses_average_nightly() -> None:
    averages_4000 = make_listing(base_price=_rupees(9999))  # the base price is not what the filter reads
    averages_4500 = make_listing()
    quotes = FakeQuotePort(
        {
            averages_4000.room_type_id: (_rupees(3000), _rupees(5000)),
            averages_4500.room_type_id: (_rupees(4000), _rupees(5000)),
        }
    )
    service = _service(averages_4000, averages_4500, quotes=quotes)

    exactly_4000 = service.search(make_criteria(min_price=_rupees(4000), max_price=_rupees(4000)))
    from_4100 = service.search(make_criteria(min_price=_rupees(4100)))
    up_to_4500 = service.search(make_criteria(max_price=_rupees(4500)))

    assert _room_type_ids(exactly_4000) == {averages_4000.room_type_id}
    assert _room_type_ids(from_4100) == {averages_4500.room_type_id}
    assert _room_type_ids(up_to_4500) == {averages_4000.room_type_id, averages_4500.room_type_id}


def test_offers_are_grouped_per_property_cheapest_first() -> None:
    hotel_id = UUID(int=7)
    suite = make_listing(property_id=hotel_id, room_type_name="Suite", base_price=_rupees(6000))
    deluxe = make_listing(
        property_id=hotel_id,
        room_type_name="Deluxe",
        base_price=_rupees(4000),
        amenity_codes=frozenset({"WIFI", "POOL", "BALCONY"}),
    )
    service = _service(suite, deluxe)

    page = service.search(make_criteria())

    assert page.total == 1
    [result] = page.items
    assert result.property_id == hotel_id
    assert [offer.listing.room_type_name for offer in result.offers] == ["Deluxe", "Suite"]
    assert result.lowest_nightly_price == _rupees(4000)
    assert result.amenity_codes == frozenset({"WIFI", "POOL"})  # what every offered room comes with


def test_sort_price_and_rating() -> None:
    mid_rated = make_listing(property_name="A", star_rating=3, base_price=_rupees(5000))
    unrated = make_listing(property_name="B", star_rating=None, base_price=_rupees(3000))
    top_rated = make_listing(property_name="C", star_rating=5, base_price=_rupees(4000))
    top_rated_cheaper = make_listing(property_name="D", star_rating=5, base_price=_rupees(3500))
    service = _service(mid_rated, unrated, top_rated, top_rated_cheaper)

    def names(order: SortOrder) -> list[str]:
        return _property_names(service.search(make_criteria(sort=order)))

    assert names(SortOrder.PRICE_LOW_TO_HIGH) == ["B", "D", "C", "A"]
    assert names(SortOrder.PRICE_HIGH_TO_LOW) == ["A", "C", "D", "B"]
    assert names(SortOrder.RATING_HIGH_TO_LOW) == ["D", "C", "A", "B"]


def test_every_sort_order_has_a_sorter() -> None:
    assert set(SORTERS) == set(SortOrder)


def test_pagination_totals() -> None:
    listings = [
        make_listing(property_name=f"P{rupees}", base_price=_rupees(rupees)) for rupees in range(1000, 6000, 1000)
    ]
    service = _service(*listings)

    first = service.search(make_criteria(page=1, page_size=2))
    last = service.search(make_criteria(page=3, page_size=2))
    beyond = service.search(make_criteria(page=4, page_size=2))

    assert (_property_names(first), first.page, first.page_size, first.total) == (["P1000", "P2000"], 1, 2, 5)
    assert (_property_names(last), last.total) == (["P5000"], 5)
    assert (beyond.items, beyond.total) == ((), 5)


def test_new_filter_registers_without_service_change() -> None:
    standard = make_listing(pricing_strategy=PricingStrategyCode.STANDARD)
    weekend = make_listing(pricing_strategy=PricingStrategyCode.WEEKEND)
    registry = filter_registry.copy()

    @registry.register
    class StandardPricingOnlyFilter(SearchFilter):
        name: ClassVar[str] = "standard_pricing_only"
        stage: ClassVar[FilterStage] = FilterStage.INDEX

        def is_active(self, criteria: SearchCriteria) -> bool:
            return True

        def predicate(self, criteria: SearchCriteria) -> Predicate:
            return Predicate("pricing_strategy", Op.EQ, PricingStrategyCode.STANDARD)

    extended = _service(standard, weekend, filters=registry)
    default = _service(standard, weekend)

    assert _room_type_ids(extended.search(make_criteria())) == {standard.room_type_id}
    assert _room_type_ids(default.search(make_criteria())) == {standard.room_type_id, weekend.room_type_id}


def test_registering_a_second_filter_under_a_taken_name_fails() -> None:
    registry = filter_registry.copy()

    class Impostor(SearchFilter):
        name: ClassVar[str] = "city"
        stage: ClassVar[FilterStage] = FilterStage.INDEX

        def is_active(self, criteria: SearchCriteria) -> bool:
            return False

    with pytest.raises(ValueError, match="city"):
        registry.register(Impostor)


def test_every_candidate_is_read_in_batches_so_nothing_is_dropped() -> None:
    sold_out = [make_listing(property_name=f"Sold out {n}") for n in range(5)]
    bookable = make_listing(property_name="Bookable")
    repository = InMemoryListingRepository([*sold_out, bookable])
    availability = FakeAvailabilityPort({listing.room_type_id: 0 for listing in sold_out})
    service = SearchService(listings=repository, availability=availability, quotes=FakeQuotePort(), batch_size=2)

    page = service.search(make_criteria())

    assert _property_names(page) == ["Bookable"]
    assert page.total == 1
    assert [len(call[0]) for call in availability.calls] == [2, 2, 2]
    assert [call[1:] for call in repository.find_calls] == [
        (2, None),
        (2, _position(repository, 2)),
        (2, _position(repository, 4)),
        (2, _position(repository, 6)),
    ]


def test_price_sort_and_total_cover_every_batch() -> None:
    prices = [9000, 3000, 7000, 1000, 5000]
    listings = [make_listing(property_name=f"P{rupees}", base_price=_rupees(rupees)) for rupees in prices]
    service = _service(*listings, batch_size=2)

    page = service.search(make_criteria(page_size=2))

    assert _property_names(page) == ["P1000", "P3000"]
    assert page.total == 5


def test_a_very_large_search_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    service = _service(make_listing(), make_listing(), make_listing(), batch_size=2, large_search_candidates=2)

    with caplog.at_level(logging.WARNING, logger="discovery.services.search_service"):
        page = service.search(make_criteria())

    assert page.total == 3
    assert "3 candidate listings" in caplog.text


@pytest.mark.parametrize(
    ("stay", "field"),
    [
        (DateRange(TODAY - timedelta(days=1), TODAY + timedelta(days=1)), "check_in"),
        (DateRange(WINDOW.end - timedelta(days=1), WINDOW.end + timedelta(days=1)), "check_out"),
    ],
)
def test_a_stay_outside_the_booking_window_is_refused(stay: DateRange, field: str) -> None:
    repository = InMemoryListingRepository([make_listing()])
    service = SearchService(listings=repository, availability=FakeAvailabilityPort(), quotes=FakeQuotePort())

    with pytest.raises(ValidationFailed) as raised:
        service.search(make_criteria(stay=stay))

    assert raised.value.status_code is VALIDATION_FAILED
    assert set(raised.value.errors) == {field}
    assert repository.find_calls == []


def test_the_first_and_last_bookable_stays_are_searched() -> None:
    service = _service(make_listing())
    first = DateRange(TODAY, TODAY + timedelta(days=1))
    last = DateRange(WINDOW.end - timedelta(days=1), WINDOW.end)

    assert service.search(make_criteria(stay=first)).total == 1
    assert service.search(make_criteria(stay=last)).total == 1


def _position(repository: InMemoryListingRepository, index: int) -> tuple[UUID, UUID]:
    """The keyset position of the ``index``-th listing (1-based) in the repository's order."""
    ordered = sorted(repository.rows.values(), key=lambda listing: (listing.property_id, listing.room_type_id))
    listing = ordered[index - 1]
    return listing.property_id, listing.room_type_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"city": "   "},
        {"guests": 0},
        {"rooms": 0},
        {"page": 0},
        {"page_size": 51},
        {"min_star_rating": 6},
        {"min_price": Money.of_rupees(5000), "max_price": Money.of_rupees(4000)},
    ],
)
def test_invalid_criteria_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationFailed) as raised:
        make_criteria(**overrides)

    assert raised.value.status_code is INVALID_SEARCH


def test_min_occupancy_per_room_rounds_up() -> None:
    assert make_criteria(guests=5, rooms=2).min_occupancy_per_room == 3
    assert make_criteria(guests=4, rooms=2).min_occupancy_per_room == 2
