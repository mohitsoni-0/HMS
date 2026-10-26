"""``AvailabilityPort`` over ``InventoryAPI``: one availability query per candidate batch, and inventory's
booking window (the horizon rule ``hold`` enforces)."""

from collections.abc import Sequence
from uuid import UUID

from discovery.interfaces.ports import AvailabilityPort
from hotel_commons.domain.date_range import DateRange
from inventory.public.api import InventoryAPI


class InventoryAvailabilityAdapter(AvailabilityPort):
    def __init__(self, inventory: InventoryAPI) -> None:
        self._inventory = inventory

    def available_rooms(self, room_type_ids: Sequence[UUID], stay: DateRange, rooms: int) -> dict[UUID, int]:
        return self._inventory.availability(room_type_ids, stay, rooms)

    def booking_window(self) -> DateRange:
        return self._inventory.booking_window()
