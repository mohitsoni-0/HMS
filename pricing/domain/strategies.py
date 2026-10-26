"""Pricing strategies (LLD §6): how a room type's base price becomes a rate for each night of a stay.

Factors multiply as Decimals and each night is rounded ROUND_HALF_UP to a whole paisa once, after all
factors, by ``Money.times``. Only ``DynamicPricing`` reads occupancy, with one lookup for the whole stay.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import ClassVar, Final, Protocol
from uuid import UUID

from catalog.public.enums import PricingStrategyCode
from pricing.domain.quote import NightlyRate, PricingRequest

WEEKEND_WEEKDAYS: Final = frozenset({4, 5})  # Friday and Saturday nights
WEEKEND_FACTOR: Final = Decimal("1.20")
NO_ADJUSTMENT: Final = Decimal(1)
# (minimum occupancy, factor), highest tier first: the first tier the occupancy reaches applies.
SURGE_TIERS: Final = (
    (Decimal("0.90"), Decimal("1.25")),
    (Decimal("0.70"), Decimal("1.10")),
)


class OccupancyLookup(Protocol):
    """Occupancy (0..1) of a room type for each requested night."""

    def __call__(self, room_type_id: UUID, nights: Sequence[date]) -> Mapping[date, Decimal]: ...


class PricingStrategy(ABC):
    code: ClassVar[PricingStrategyCode]

    @abstractmethod
    def nightly_rates(self, request: PricingRequest, occupancy: OccupancyLookup) -> tuple[NightlyRate, ...]:
        """One rate per night of ``request.stay``, in date order, for a single room."""


class StandardPricing(PricingStrategy):
    """The base price every night."""

    code = PricingStrategyCode.STANDARD

    def nightly_rates(self, request: PricingRequest, occupancy: OccupancyLookup) -> tuple[NightlyRate, ...]:
        return tuple(NightlyRate(night, request.base_price) for night in request.stay.dates())


class WeekendPricing(PricingStrategy):
    """Friday and Saturday nights at the base price × 1.20; other nights at the base price."""

    code = PricingStrategyCode.WEEKEND

    def nightly_rates(self, request: PricingRequest, occupancy: OccupancyLookup) -> tuple[NightlyRate, ...]:
        return tuple(
            NightlyRate(night, request.base_price.times(weekend_factor(night))) for night in request.stay.dates()
        )


class DynamicPricing(PricingStrategy):
    """The weekend factor, then an occupancy surge: ≥ 0.90 × 1.25, ≥ 0.70 × 1.10.

    A night the lookup does not report counts as empty, so it gets no surge.
    """

    code = PricingStrategyCode.DYNAMIC

    def nightly_rates(self, request: PricingRequest, occupancy: OccupancyLookup) -> tuple[NightlyRate, ...]:
        nights = request.stay.dates()
        occupancy_by_night = occupancy(request.room_type_id, nights)
        return tuple(
            NightlyRate(night, request.base_price.times(_dynamic_factor(night, occupancy_by_night)))
            for night in nights
        )


def weekend_factor(night: date) -> Decimal:
    return WEEKEND_FACTOR if night.weekday() in WEEKEND_WEEKDAYS else NO_ADJUSTMENT


def surge_factor(occupancy: Decimal) -> Decimal:
    """The factor of the highest surge tier ``occupancy`` reaches, or no adjustment below every tier."""
    for threshold, factor in SURGE_TIERS:
        if occupancy >= threshold:
            return factor
    return NO_ADJUSTMENT


def _dynamic_factor(night: date, occupancy_by_night: Mapping[date, Decimal]) -> Decimal:
    return weekend_factor(night) * surge_factor(occupancy_by_night.get(night, Decimal(0)))
