"""One room type on one night (LLD §5.2): the counter that bookings reserve against.

The database enforces ``0 <= reserved_rooms <= total_rooms`` as well, so a bug elsewhere can never oversell.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from hotel_commons.domain.date_range import DateRange
from hotel_commons.exceptions import Conflict

from inventory.status_code import INVENTORY_UNAVAILABLE


@dataclass(slots=True)
class RoomNight:
    room_type_id: UUID
    stay_date: date
    total_rooms: int
    reserved_rooms: int

    @property
    def available(self) -> int:
        return self.total_rooms - self.reserved_rooms

    @property
    def occupancy(self) -> Decimal:
        """Share of the rooms reserved, 0..1; a night with no rooms counts as full."""
        if self.total_rooms <= 0:
            return Decimal(1)
        return Decimal(self.reserved_rooms) / Decimal(self.total_rooms)


def ensure_bookable(nights: Sequence[RoomNight], stay: DateRange, rooms: int) -> None:
    """Every night of ``stay`` is present and has ``rooms`` free, else Conflict(INVENTORY_UNAVAILABLE)."""
    if len(nights) != stay.nights or any(night.available < rooms for night in nights):
        raise Conflict(INVENTORY_UNAVAILABLE)
