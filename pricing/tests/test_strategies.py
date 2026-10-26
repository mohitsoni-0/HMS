"""Unit tests for the pricing strategies, the registry and the quote arithmetic (LLD §6). No Django."""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from catalog.public.enums import PricingStrategyCode
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money
from hotel_commons.exceptions import BusinessRuleViolation
from pricing.domain.quote import NightlyRate, PriceQuote, PricingRequest
from pricing.domain.registry import PricingStrategyRegistry, build_default_registry
from pricing.domain.strategies import DynamicPricing, StandardPricing, WeekendPricing
from pricing.interfaces.ports import OccupancyPort
from pricing.services.pricing_service import PricingService
from pricing.status_code import UNSUPPORTED_PRICING_STRATEGY

THURSDAY = date(2026, 11, 19)
FRIDAY = date(2026, 11, 20)
SATURDAY = date(2026, 11, 21)
SUNDAY = date(2026, 11, 22)
MONDAY = date(2026, 11, 23)
BASE = Money.of_rupees(4000)


class RecordingOccupancy(OccupancyPort):
    """Returns fixed occupancy per night and records every lookup."""

    def __init__(self, by_night: Mapping[date, Decimal] | None = None) -> None:
        self.by_night = dict(by_night or {})
        self.calls: list[tuple[UUID, tuple[date, ...]]] = []

        self.batch_calls: list[tuple[tuple[UUID, ...], tuple[date, ...]]] = []

    def occupancy(self, room_type_id: UUID, nights: Sequence[date]) -> Mapping[date, Decimal]:
        self.calls.append((room_type_id, tuple(nights)))
        return {night: self.by_night.get(night, Decimal(0)) for night in nights}

    def occupancy_many(
        self, room_type_ids: Sequence[UUID], nights: Sequence[date]
    ) -> Mapping[UUID, Mapping[date, Decimal]]:
        self.batch_calls.append((tuple(room_type_ids), tuple(nights)))
        by_night = {night: self.by_night.get(night, Decimal(0)) for night in nights}
        return {room_type_id: dict(by_night) for room_type_id in room_type_ids}


def _request(
    strategy: PricingStrategyCode,
    start: date,
    end: date,
    *,
    base: Money = BASE,
    rooms: int = 1,
) -> PricingRequest:
    return PricingRequest(
        room_type_id=uuid4(), base_price=base, strategy=strategy, stay=DateRange(start, end), rooms=rooms
    )


def _paisa(rates: tuple[NightlyRate, ...]) -> list[int]:
    return [rate.amount.paisa for rate in rates]


def test_standard_every_night_base() -> None:
    request = _request(PricingStrategyCode.STANDARD, THURSDAY, MONDAY)

    rates = StandardPricing().nightly_rates(request, RecordingOccupancy().occupancy)

    assert [rate.night for rate in rates] == [THURSDAY, FRIDAY, SATURDAY, SUNDAY]
    assert _paisa(rates) == [400000, 400000, 400000, 400000]


def test_weekend_friday_saturday_times_1_20() -> None:
    request = _request(PricingStrategyCode.WEEKEND, THURSDAY, MONDAY)

    rates = WeekendPricing().nightly_rates(request, RecordingOccupancy().occupancy)

    assert _paisa(rates) == [400000, 480000, 480000, 400000]


def test_weekend_rounds_each_night_half_up() -> None:
    request = _request(PricingStrategyCode.WEEKEND, FRIDAY, SATURDAY, base=Money(1001))

    rates = WeekendPricing().nightly_rates(request, RecordingOccupancy().occupancy)

    assert _paisa(rates) == [1201]  # 1001 × 1.20 = 1201.2


def test_total_is_per_room_total_times_rooms() -> None:
    service = PricingService(build_default_registry(), RecordingOccupancy())

    quote = service.quote(_request(PricingStrategyCode.WEEKEND, THURSDAY, SUNDAY, rooms=3))

    assert quote.per_room_total == Money(400000 + 480000 + 480000)
    assert quote.total == Money(1360000 * 3)
    assert quote.rooms == 3
    assert quote.strategy is PricingStrategyCode.WEEKEND


def test_average_nightly_half_up() -> None:
    quote = PriceQuote(
        room_type_id=uuid4(),
        strategy=PricingStrategyCode.STANDARD,
        rooms=1,
        nightly_rates=(NightlyRate(FRIDAY, Money(100)), NightlyRate(SATURDAY, Money(101))),
    )

    assert quote.average_nightly == Money(101)  # 201 / 2 = 100.5 -> 101


@pytest.mark.parametrize(
    ("occupancy", "expected_paisa"),
    [
        (Decimal("0.00"), 400000),
        (Decimal("0.69"), 400000),
        (Decimal("0.70"), 440000),
        (Decimal("0.89"), 440000),
        (Decimal("0.90"), 500000),
        (Decimal("1.00"), 500000),
    ],
)
def test_dynamic_surge_tiers(occupancy: Decimal, expected_paisa: int) -> None:
    request = _request(PricingStrategyCode.DYNAMIC, THURSDAY, FRIDAY)

    rates = DynamicPricing().nightly_rates(request, RecordingOccupancy({THURSDAY: occupancy}).occupancy)

    assert _paisa(rates) == [expected_paisa]


def test_dynamic_surge_combines_with_weekend() -> None:
    request = _request(PricingStrategyCode.DYNAMIC, THURSDAY, SUNDAY)
    occupancy = RecordingOccupancy({THURSDAY: Decimal("0.95"), FRIDAY: Decimal("0.75"), SATURDAY: Decimal("0.92")})

    rates = DynamicPricing().nightly_rates(request, occupancy.occupancy)

    # Thu ×1.25; Fri ×1.20 ×1.10 = ×1.32; Sat ×1.20 ×1.25 = ×1.50
    assert _paisa(rates) == [500000, 528000, 600000]


def test_dynamic_rounds_once_after_all_factors() -> None:
    request = _request(PricingStrategyCode.DYNAMIC, FRIDAY, SATURDAY, base=Money(1002))

    rates = DynamicPricing().nightly_rates(request, RecordingOccupancy({FRIDAY: Decimal("0.70")}).occupancy)

    assert _paisa(rates) == [1323]  # 1002 × 1.32 = 1322.64; rounding per factor would give 1202 × 1.10 -> 1322


def test_dynamic_looks_up_occupancy_once_for_all_nights() -> None:
    request = _request(PricingStrategyCode.DYNAMIC, THURSDAY, MONDAY)
    occupancy = RecordingOccupancy()

    DynamicPricing().nightly_rates(request, occupancy.occupancy)

    assert occupancy.calls == [(request.room_type_id, (THURSDAY, FRIDAY, SATURDAY, SUNDAY))]


@pytest.mark.parametrize("code", [PricingStrategyCode.STANDARD, PricingStrategyCode.WEEKEND])
def test_standard_never_calls_occupancy(code: PricingStrategyCode) -> None:
    occupancy = RecordingOccupancy()
    service = PricingService(build_default_registry(), occupancy)

    service.quote(_request(code, THURSDAY, MONDAY))

    assert occupancy.calls == []


def test_every_strategy_code_registered() -> None:
    registry = build_default_registry()

    assert registry.codes() == frozenset(PricingStrategyCode)
    for code in PricingStrategyCode:
        assert registry.get(code).code is code


def test_unregistered_strategy_is_unsupported() -> None:
    registry = PricingStrategyRegistry()
    registry.register(StandardPricing())

    with pytest.raises(BusinessRuleViolation) as raised:
        registry.get(PricingStrategyCode.DYNAMIC)

    assert raised.value.status_code is UNSUPPORTED_PRICING_STRATEGY
    assert raised.value.http_status == 422


def test_quote_many_keys_quotes_by_room_type() -> None:
    service = PricingService(build_default_registry(), RecordingOccupancy())
    first = _request(PricingStrategyCode.STANDARD, THURSDAY, FRIDAY)
    second = _request(PricingStrategyCode.WEEKEND, FRIDAY, SATURDAY)

    quotes = service.quote_many([first, second])

    assert quotes[first.room_type_id].total == Money(400000)
    assert quotes[second.room_type_id].total == Money(480000)


def test_quote_many_reads_occupancy_once_for_every_dynamic_request() -> None:
    occupancy = RecordingOccupancy({THURSDAY: Decimal("0.95"), FRIDAY: Decimal("0.75")})
    service = PricingService(build_default_registry(), occupancy)
    dynamic = [_request(PricingStrategyCode.DYNAMIC, THURSDAY, SATURDAY) for _ in range(3)]
    standard = _request(PricingStrategyCode.STANDARD, THURSDAY, SATURDAY)

    quotes = service.quote_many([*dynamic, standard])

    assert occupancy.calls == []
    assert occupancy.batch_calls == [(tuple(request.room_type_id for request in dynamic), (THURSDAY, FRIDAY))]
    for request in [*dynamic, standard]:
        assert quotes[request.room_type_id] == service.quote(request)


def test_quote_many_without_dynamic_requests_reads_no_occupancy() -> None:
    occupancy = RecordingOccupancy()
    service = PricingService(build_default_registry(), occupancy)

    service.quote_many([_request(PricingStrategyCode.WEEKEND, THURSDAY, MONDAY)])

    assert (occupancy.calls, occupancy.batch_calls) == ([], [])


def test_quote_many_rejects_an_unregistered_strategy_before_reading_occupancy() -> None:
    registry = PricingStrategyRegistry()
    registry.register(DynamicPricing())
    occupancy = RecordingOccupancy()
    service = PricingService(registry, occupancy)

    requests = [_request(code, THURSDAY, FRIDAY) for code in (PricingStrategyCode.DYNAMIC, PricingStrategyCode.STANDARD)]

    with pytest.raises(BusinessRuleViolation):
        service.quote_many(requests)

    assert occupancy.batch_calls == []
