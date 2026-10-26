"""``AvailabilityPort`` over ``InventoryAPI.availability``: one query for every candidate room type."""

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
