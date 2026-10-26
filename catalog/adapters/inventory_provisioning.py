"""``InventoryProvisioningPort`` over the inventory context's public API (LLD §4.3)."""

from uuid import UUID

from inventory.public.api import get_inventory_api

from catalog.interfaces.ports import InventoryProvisioningPort


class InventoryApiProvisioning(InventoryProvisioningPort):
    """Provisions through ``InventoryAPI.provision``, inside the caller's unit of work (same connection)."""

    def provision(self, room_type_id: UUID, total_rooms: int) -> None:
        get_inventory_api().provision(room_type_id, total_rooms)
