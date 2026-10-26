"""Django ``HoldRepository`` (LLD §5.3) over ``inventory.hold``; every write appends an ``inventory.hold_log`` row."""

from uuid import UUID

from inventory.domain.hold import InventoryHold
from inventory.interfaces.repositories import HoldRepository
from inventory.models.inventory_models import InventoryHold as InventoryHoldModel
from inventory.repositories.mappers import HOLD_UPDATE_FIELDS, apply_hold, hold_to_domain, hold_to_model
from inventory.status_code import HOLD_NOT_FOUND


class DjangoHoldRepository(HoldRepository):
    def add(self, hold: InventoryHold) -> None:
        hold_to_model(hold).save(force_insert=True)

    def get_for_update(self, hold_id: UUID) -> InventoryHold:
        row = InventoryHoldModel.objects.select_for_update().get_or_not_found(HOLD_NOT_FOUND, pk=hold_id)
        return hold_to_domain(row)

    def save(self, hold: InventoryHold) -> None:
        row = InventoryHoldModel.objects.get(pk=hold.id)  # already locked by get_for_update in this transaction
        apply_hold(hold, row)
        row.save(update_fields=HOLD_UPDATE_FIELDS)
