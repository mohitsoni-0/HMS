"""Composition root of the catalog (LLD §2.2): the only module that picks implementations.

Services are stateless, so each is built once per process and shared across threads.
"""

from functools import cache

from hotel_commons.db.unit_of_work import DjangoUnitOfWork
from hotel_commons.events.in_process import event_bus

from catalog.adapters.inventory_provisioning import InventoryApiProvisioning
from catalog.repositories.amenity_repository import DjangoAmenityRepository
from catalog.repositories.owner_account_repository import DjangoOwnerAccountRepository
from catalog.repositories.property_repository import DjangoPropertyRepository
from catalog.services.onboarding_service import PropertyOnboardingService
from catalog.services.owner_account_service import OwnerAccountService
from catalog.services.query_service import CatalogQueryService
from catalog.services.room_type_service import RoomTypeService


@cache
def owner_account_service() -> OwnerAccountService:
    return OwnerAccountService(DjangoOwnerAccountRepository(), DjangoUnitOfWork())


@cache
def onboarding_service() -> PropertyOnboardingService:
    return PropertyOnboardingService(
        accounts=DjangoOwnerAccountRepository(),
        properties=DjangoPropertyRepository(),
        amenities=DjangoAmenityRepository(),
        provisioning=InventoryApiProvisioning(),
        events=event_bus,
        uow=DjangoUnitOfWork(),
    )


@cache
def room_type_service() -> RoomTypeService:
    return RoomTypeService(
        properties=DjangoPropertyRepository(),
        amenities=DjangoAmenityRepository(),
        provisioning=InventoryApiProvisioning(),
        events=event_bus,
        uow=DjangoUnitOfWork(),
    )


@cache
def query_service() -> CatalogQueryService:
    return CatalogQueryService(DjangoOwnerAccountRepository(), DjangoPropertyRepository(), DjangoAmenityRepository())
