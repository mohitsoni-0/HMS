"""Price quote value objects (LLD §6): the request a strategy prices and the per-night result.

All amounts are ``Money`` (integer paisa). A quote stores only its nightly rates; totals and averages are
derived from them, so they can never disagree.
"""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from catalog.public.enums import PricingStrategyCode
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money


@dataclass(frozen=True, slots=True)
class NightlyRate:
    night: date
    amount: Money


@dataclass(frozen=True, slots=True)
class PricingRequest:
    """Price ``rooms`` rooms of one room type for ``stay`` with the room type's strategy and base price."""

    room_type_id: UUID
    base_price: Money
    strategy: PricingStrategyCode
    stay: DateRange
    rooms: int


@dataclass(frozen=True, slots=True)
class PriceQuote:
    room_type_id: UUID
    strategy: PricingStrategyCode
    rooms: int
    nightly_rates: tuple[NightlyRate, ...]

    @property
    def per_room_total(self) -> Money:
        """The sum of the nightly rates for one room."""
        return sum((rate.amount for rate in self.nightly_rates), Money.zero())

    @property
    def total(self) -> Money:
        """What the whole booking costs: the per-room total times the number of rooms."""
        return self.per_room_total.times(self.rooms)

    @property
    def average_nightly(self) -> Money:
        """The per-room total spread over the nights, ROUND_HALF_UP to a whole paisa."""
        return self.per_room_total.divide(len(self.nightly_rates))
