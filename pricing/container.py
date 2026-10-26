"""Composition root of the pricing context: the only place that picks concrete implementations."""

from functools import cache

from pricing.adapters.inventory_occupancy import InventoryOccupancyAdapter
from pricing.domain.registry import build_default_registry
from pricing.services.pricing_service import PricingService


@cache
def pricing_service() -> PricingService:
    """The process-wide service: all strategies, with occupancy read from inventory."""
    return PricingService(build_default_registry(), InventoryOccupancyAdapter())
