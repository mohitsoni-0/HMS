"""Composition root of the inventory context: the only place that picks implementations (LLD §2.2)."""

from functools import lru_cache

from django.conf import settings

from hotel_commons.db.unit_of_work import DjangoUnitOfWork
from hotel_commons.domain.clock import SystemClock

from inventory.repositories.allotment_repository import DjangoAllotmentRepository
from inventory.repositories.hold_repository import DjangoHoldRepository
from inventory.repositories.room_night_repository import DjangoRoomNightRepository
from inventory.services.inventory_service import InventoryService


@lru_cache(maxsize=1)
def inventory_service() -> InventoryService:
    """The process-wide service; its repositories and unit of work hold no per-request state."""
    return InventoryService(
        allotments=DjangoAllotmentRepository(),
        room_nights=DjangoRoomNightRepository(),
        holds=DjangoHoldRepository(),
        uow=DjangoUnitOfWork(),
        clock=SystemClock(),
        horizon_days=settings.INVENTORY_HORIZON_DAYS,
    )
