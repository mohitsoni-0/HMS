"""Composition root of the discovery context: the only place that picks implementations (LLD §2.2)."""

from functools import cache

from discovery.adapters.inventory_availability import InventoryAvailabilityAdapter
from discovery.adapters.pricing_quotes import PricingQuoteAdapter
from discovery.repositories.listing_repository import DjangoListingRepository
from discovery.services.listing_projector import ListingProjector
from discovery.services.search_service import SearchService
from inventory.public.api import get_inventory_api
from pricing.public.api import get_pricing_api


@cache
def listing_repository() -> DjangoListingRepository:
    return DjangoListingRepository()


@cache
def search_service() -> SearchService:
    """The process-wide search, with the registered filters and sorters over live inventory and pricing."""
    return SearchService(
        listings=listing_repository(),
        availability=InventoryAvailabilityAdapter(get_inventory_api()),
        quotes=PricingQuoteAdapter(get_pricing_api()),
    )


@cache
def listing_projector() -> ListingProjector:
    """One instance, so subscribing its handlers again (a second ``ready()``) is ignored by the bus."""
    return ListingProjector(listing_repository())
