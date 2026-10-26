"""Quoting use cases (LLD §6): pick the room type's strategy and price every night of the stay.

``quote_many`` reads the occupancy of every request whose strategy needs it in one batch
(``OccupancyPort.occupancy_many``), so a search over many DYNAMIC room types costs one inventory query.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from pricing.domain.quote import PriceQuote, PricingRequest
from pricing.domain.registry import PricingStrategyRegistry
from pricing.domain.strategies import OccupancyLookup, PricingStrategy
from pricing.interfaces.ports import OccupancyPort

logger = logging.getLogger(__name__)


class PricingService:
    def __init__(self, registry: PricingStrategyRegistry, occupancy: OccupancyPort) -> None:
        self._registry = registry
        self._occupancy = occupancy

    def quote(self, request: PricingRequest) -> PriceQuote:
        """Price ``request``; an unregistered strategy raises BusinessRuleViolation (422)."""
        return _price(request, self._registry.get(request.strategy), self._occupancy.occupancy)

    def quote_many(self, requests: Sequence[PricingRequest]) -> dict[UUID, PriceQuote]:
        """One quote per request, keyed by room type id; occupancy is read once for all requests that need it.

        An unregistered strategy in any request raises BusinessRuleViolation (422) before anything is read.
        """
        priced = [(request, self._registry.get(request.strategy)) for request in requests]
        occupancy = self._prefetched_occupancy([request for request, strategy in priced if strategy.reads_occupancy])
        return {request.room_type_id: _price(request, strategy, occupancy) for request, strategy in priced}

    def _prefetched_occupancy(self, requests: Sequence[PricingRequest]) -> OccupancyLookup:
        """A lookup answering from one ``occupancy_many`` call over every room type and night of ``requests``."""
        if not requests:
            return self._occupancy.occupancy
        room_type_ids = list(dict.fromkeys(request.room_type_id for request in requests))
        nights = sorted({night for request in requests for night in request.stay.dates()})
        known = self._occupancy.occupancy_many(room_type_ids, nights)
        return _MapLookup(known)


class _MapLookup:
    """An ``OccupancyLookup`` over prefetched occupancy; a night it does not know counts as empty."""

    def __init__(self, known: Mapping[UUID, Mapping[date, Decimal]]) -> None:
        self._known = known

    def __call__(self, room_type_id: UUID, nights: Sequence[date]) -> Mapping[date, Decimal]:
        by_night = self._known.get(room_type_id, {})
        return {night: by_night.get(night, Decimal(0)) for night in nights}


def _price(request: PricingRequest, strategy: PricingStrategy, occupancy: OccupancyLookup) -> PriceQuote:
    nightly_rates = strategy.nightly_rates(request, occupancy)
    logger.debug("Quoted room type %s with strategy %s", request.room_type_id, strategy.code.name)
    return PriceQuote(
        room_type_id=request.room_type_id,
        strategy=strategy.code,
        rooms=request.rooms,
        nightly_rates=nightly_rates,
    )
