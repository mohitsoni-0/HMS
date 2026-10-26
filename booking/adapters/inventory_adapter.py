"""``InventoryPort`` over the inventory public API (LLD §8.3)."""

from datetime import datetime
from uuid import UUID

from hotel_commons.domain.date_range import DateRange

from booking.interfaces.ports import HoldReleaseReason, InventoryPort
from inventory.public.api import get_inventory_api
from inventory.public.enums import ReleaseReason


class InventoryAdapter(InventoryPort):
    def hold(
        self, *, room_type_id: UUID, stay: DateRange, rooms: int, reference: str, expires_at: datetime | None
    ) -> UUID:
        hold = get_inventory_api().hold(
            room_type_id=room_type_id, stay=stay, rooms=rooms, reference=reference, expires_at=expires_at
        )
        return hold.hold_id

    def confirm_hold(self, hold_id: UUID) -> None:
        get_inventory_api().confirm_hold(hold_id)

    def release_hold(self, hold_id: UUID, reason: HoldReleaseReason) -> None:
        get_inventory_api().release_hold(hold_id, ReleaseReason[reason])
