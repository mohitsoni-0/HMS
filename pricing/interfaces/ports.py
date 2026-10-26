"""Outbound ports of the pricing context (LLD §6)."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID


class OccupancyPort(ABC):
    @abstractmethod
    def occupancy(self, room_type_id: UUID, nights: Sequence[date]) -> Mapping[date, Decimal]:
        """Occupancy (reserved / total rooms, 0..1) of the room type for each of ``nights``."""

    @abstractmethod
    def occupancy_many(
        self, room_type_ids: Sequence[UUID], nights: Sequence[date]
    ) -> Mapping[UUID, Mapping[date, Decimal]]:
        """``occupancy`` of every room type in one lookup: room type -> night -> occupancy."""
