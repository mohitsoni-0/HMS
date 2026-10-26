"""Quoting use cases (LLD §6): pick the room type's strategy and price every night of the stay."""

import logging
from collections.abc import Sequence
from uuid import UUID

from pricing.domain.quote import PriceQuote, PricingRequest
from pricing.domain.registry import PricingStrategyRegistry
from pricing.interfaces.ports import OccupancyPort

logger = logging.getLogger(__name__)


class PricingService:
    def __init__(self, registry: PricingStrategyRegistry, occupancy: OccupancyPort) -> None:
        self._registry = registry
        self._occupancy = occupancy

    def quote(self, request: PricingRequest) -> PriceQuote:
        """Price ``request``; an unregistered strategy raises BusinessRuleViolation (422)."""
        strategy = self._registry.get(request.strategy)
        nightly_rates = strategy.nightly_rates(request, self._occupancy.occupancy)
        logger.debug("Quoted room type %s with strategy %s", request.room_type_id, strategy.code.name)
        return PriceQuote(
            room_type_id=request.room_type_id,
            strategy=strategy.code,
            rooms=request.rooms,
            nightly_rates=nightly_rates,
        )

    def quote_many(self, requests: Sequence[PricingRequest]) -> dict[UUID, PriceQuote]:
        """One quote per request, keyed by room type id."""
        return {request.room_type_id: self.quote(request) for request in requests}
