"""Composition root of the discovery context: the only place that picks implementations (LLD §2.2)."""

from functools import cache

from catalog.public.api import get_catalog_api
from discovery.adapters.catalog_amenities import CatalogAmenityAdapter
from discovery.adapters.catalog_events import CatalogEventSubscriber
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
    return ListingProjector(listing_repository())


@cache
def catalog_event_subscriber() -> CatalogEventSubscriber:
    """One instance, so subscribing its handlers again (a second ``ready()``) is ignored by the bus."""
    return CatalogEventSubscriber(listing_projector())


@cache
def amenity_catalog() -> CatalogAmenityAdapter:
    """The amenity codes the catalog knows, for validating a search's amenity filter."""
    return CatalogAmenityAdapter(get_catalog_api())
