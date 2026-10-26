"""Outbound ports of the catalog (LLD §4.3)."""

from abc import ABC, abstractmethod
from uuid import UUID


class InventoryProvisioningPort(ABC):
    @abstractmethod
    def provision(self, room_type_id: UUID, total_rooms: int) -> None:
        """Create (or resize) the room type's sellable inventory over the booking horizon."""
