"""Repository ports of the inventory context (LLD §5.3).

``inventory.repositories`` implements them over the Django ORM and ``inventory.tests.fakes`` in memory.
Every method runs inside the caller's unit of work.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from hotel_commons.domain.date_range import DateRange

from inventory.domain.allotment import Allotment
from inventory.domain.hold import InventoryHold
from inventory.domain.room_night import RoomNight


class AllotmentRepository(ABC):
    @abstractmethod
    def upsert(self, allotment: Allotment) -> None:
        """Insert the allotment, or overwrite the stored one of the same room type."""

    @abstractmethod
    def get(self, room_type_id: UUID) -> Allotment | None: ...

    @abstractmethod
    def all(self) -> list[Allotment]: ...


class RoomNightRepository(ABC):
    @abstractmethod
    def ensure_rows(self, room_type_id: UUID, total_rooms: int, dates: Sequence[date]) -> None:
        """Create the missing nights with ``total_rooms`` and nothing reserved; existing nights are left alone."""

    @abstractmethod
    def lock_for_stay(self, room_type_id: UUID, stay: DateRange) -> list[RoomNight]:
        """The existing nights of ``stay``, locked for update in ``stay_date`` order (the global lock order)."""

    @abstractmethod
    def add_reserved(self, room_type_id: UUID, stay: DateRange, delta: int) -> None:
        """Add ``delta`` to ``reserved_rooms`` of every night of ``stay``, all or nothing.

        A counter leaving ``0..total_rooms`` raises Conflict(INVENTORY_UNAVAILABLE) and changes nothing.
        """

    @abstractmethod
    def availability(self, room_type_ids: Sequence[UUID], stay: DateRange) -> dict[UUID, int]:
        """Room type -> fewest rooms free on any night of ``stay``, for room types with a row for every night."""

    @abstractmethod
    def occupancy_many(self, room_type_ids: Sequence[UUID], dates: Sequence[date]) -> dict[UUID, dict[date, Decimal]]:
        """Room type -> night -> share of rooms reserved (0..1), only for the requested nights that have a row."""


class HoldRepository(ABC):
    @abstractmethod
    def add(self, hold: InventoryHold) -> None: ...

    @abstractmethod
    def get_for_update(self, hold_id: UUID) -> InventoryHold:
        """The hold, locked until the unit of work ends; NotFound(HOLD_NOT_FOUND) when there is none."""

    @abstractmethod
    def save(self, hold: InventoryHold) -> None:
        """Persist the state of a hold loaded with ``get_for_update``."""
