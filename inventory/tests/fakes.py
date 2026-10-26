"""In-memory implementations of the inventory repositories, for unit tests without a database.

They keep the contracts of the Django repositories: stored entities are copies, ``add_reserved`` is all or
nothing and refuses to leave ``0..total_rooms`` (the database CHECK), and an unknown hold is NotFound.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import UUID

from hotel_commons.domain.date_range import DateRange
from hotel_commons.exceptions import Conflict, NotFound

from inventory.domain.allotment import Allotment
from inventory.domain.hold import InventoryHold
from inventory.domain.room_night import RoomNight
from inventory.interfaces.repositories import AllotmentRepository, HoldRepository, RoomNightRepository
from inventory.status_code import HOLD_NOT_FOUND, INVENTORY_UNAVAILABLE


class InMemoryAllotmentRepository(AllotmentRepository):
    def __init__(self) -> None:
        self.rows: dict[UUID, Allotment] = {}

    def upsert(self, allotment: Allotment) -> None:
        self.rows[allotment.room_type_id] = replace(allotment)

    def get(self, room_type_id: UUID) -> Allotment | None:
        stored = self.rows.get(room_type_id)
        return None if stored is None else replace(stored)

    def all(self) -> list[Allotment]:
        return [replace(allotment) for allotment in self.rows.values()]


class InMemoryRoomNightRepository(RoomNightRepository):
    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, date], RoomNight] = {}
        self.locked: list[tuple[UUID, DateRange]] = []

    def ensure_rows(self, room_type_id: UUID, total_rooms: int, dates: Sequence[date]) -> None:
        for stay_date in dates:
            self.rows.setdefault(
                (room_type_id, stay_date),
                RoomNight(room_type_id=room_type_id, stay_date=stay_date, total_rooms=total_rooms, reserved_rooms=0),
            )

    def lock_for_stay(self, room_type_id: UUID, stay: DateRange) -> list[RoomNight]:
        self.locked.append((room_type_id, stay))
        return [replace(night) for night in self._nights(room_type_id, stay)]

    def add_reserved(self, room_type_id: UUID, stay: DateRange, delta: int) -> None:
        nights = self._nights(room_type_id, stay)
        out_of_range = any(not 0 <= night.reserved_rooms + delta <= night.total_rooms for night in nights)
        if len(nights) != stay.nights or out_of_range:
            raise Conflict(INVENTORY_UNAVAILABLE)
        for night in nights:
            night.reserved_rooms += delta

    def availability(self, room_type_ids: Sequence[UUID], stay: DateRange) -> dict[UUID, int]:
        result: dict[UUID, int] = {}
        for room_type_id in room_type_ids:
            nights = self._nights(room_type_id, stay)
            if nights and len(nights) == stay.nights:
                result[room_type_id] = min(night.available for night in nights)
        return result

    def occupancy_many(self, room_type_ids: Sequence[UUID], dates: Sequence[date]) -> dict[UUID, dict[date, Decimal]]:
        occupancy: dict[UUID, dict[date, Decimal]] = {}
        for room_type_id in room_type_ids:
            known = {d: self.rows[(room_type_id, d)].occupancy for d in dates if (room_type_id, d) in self.rows}
            if known:
                occupancy[room_type_id] = known
        return occupancy

    def night(self, room_type_id: UUID, stay_date: date) -> RoomNight | None:
        """Test helper: the stored night, or None when it was never provisioned."""
        return self.rows.get((room_type_id, stay_date))

    def _nights(self, room_type_id: UUID, stay: DateRange) -> list[RoomNight]:
        return [self.rows[key] for key in ((room_type_id, d) for d in stay.dates()) if key in self.rows]


class InMemoryHoldRepository(HoldRepository):
    def __init__(self) -> None:
        self.rows: dict[UUID, InventoryHold] = {}

    def add(self, hold: InventoryHold) -> None:
        if hold.id in self.rows:
            raise AssertionError(f"hold {hold.id} added twice")
        self.rows[hold.id] = replace(hold)

    def get_for_update(self, hold_id: UUID) -> InventoryHold:
        stored = self.rows.get(hold_id)
        if stored is None:
            raise NotFound(HOLD_NOT_FOUND)
        return replace(stored)

    def save(self, hold: InventoryHold) -> None:
        if hold.id not in self.rows:
            raise AssertionError(f"hold {hold.id} saved before it was added")
        self.rows[hold.id] = replace(hold)
