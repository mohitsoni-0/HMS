"""``OccupancyPort`` over the inventory context's public API (LLD §6)."""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from inventory.public.api import get_inventory_api
from pricing.interfaces.ports import OccupancyPort


class InventoryOccupancyAdapter(OccupancyPort):
    def occupancy(self, room_type_id: UUID, nights: Sequence[date]) -> Mapping[date, Decimal]:
        return get_inventory_api().occupancy(room_type_id, nights)
