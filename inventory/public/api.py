"""``InventoryAPI``: the facade catalog, pricing, discovery and booking call (LLD §5.5).

The module imports no Django; ``get_inventory_api`` reaches the container only when it is called.
"""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from hotel_commons.domain.date_range import DateRange

from inventory.public.dto import HoldView
from inventory.public.enums import ReleaseReason
from inventory.services.inventory_service import InventoryService


class InventoryAPI:
    def __init__(self, service: InventoryService) -> None:
        self._service = service

    def provision(self, room_type_id: UUID, total_rooms: int) -> None:
        self._service.provision(room_type_id, total_rooms)

    def extend_horizon(self) -> int:
        return self._service.extend_horizon()

    def hold(
        self, *, room_type_id: UUID, stay: DateRange, rooms: int, reference: str, expires_at: datetime | None
    ) -> HoldView:
        """Conflict(INVENTORY_UNAVAILABLE) when any night lacks the rooms; 422 STAY_OUTSIDE_HORIZON past the horizon."""
        return self._service.hold(
            room_type_id=room_type_id, stay=stay, rooms=rooms, reference=reference, expires_at=expires_at
        )

    def confirm_hold(self, hold_id: UUID) -> None:
        self._service.confirm_hold(hold_id)

    def release_hold(self, hold_id: UUID, reason: ReleaseReason | str) -> None:
        """Idempotent. ``reason`` is a ``ReleaseReason`` or its name ("CANCELLED", "EXPIRED")."""
        self._service.release_hold(hold_id, _release_reason(reason))

    def availability(self, room_type_ids: Sequence[UUID], stay: DateRange, rooms: int) -> dict[UUID, int]:
        return self._service.availability(room_type_ids, stay, rooms)

    def occupancy(self, room_type_id: UUID, dates: Sequence[date]) -> dict[date, Decimal]:
        return self._service.occupancy(room_type_id, dates)


def get_inventory_api() -> InventoryAPI:
    from inventory import container

    return InventoryAPI(container.inventory_service())


def _release_reason(reason: ReleaseReason | str) -> ReleaseReason:
    if isinstance(reason, ReleaseReason):
        return reason
    try:
        return ReleaseReason[reason]
    except KeyError:
        allowed = ", ".join(member.name for member in ReleaseReason)
        raise ValueError(f"{reason!r} is not a release reason; expected one of {allowed}") from None
