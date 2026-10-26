"""Pricing DTOs, the only pricing types other contexts see (LLD §6).

``PricingRequest`` is re-exported from the domain so callers build requests without importing
``pricing.domain``. Nothing here imports Django.
"""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from catalog.public.enums import PricingStrategyCode
from hotel_commons.domain.money import Money
from pricing.domain.quote import PricingRequest

__all__ = ["PricingRequest", "QuoteDTO"]


@dataclass(frozen=True, slots=True)
class QuoteDTO:
    room_type_id: UUID
    strategy: PricingStrategyCode
    rooms: int
    nightly_rates: tuple[tuple[date, Money], ...]
    total: Money
    average_nightly: Money
