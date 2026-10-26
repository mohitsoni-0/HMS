"""Django ``AllotmentRepository`` (LLD §5.3) over ``inventory.allotment``."""

from uuid import UUID

from django.db import IntegrityError, transaction

from inventory.domain.allotment import Allotment
from inventory.interfaces.repositories import AllotmentRepository
from inventory.models.inventory_models import Allotment as AllotmentModel
from inventory.repositories.mappers import (
    ALLOTMENT_UPDATE_FIELDS,
    allotment_to_domain,
    allotment_to_model,
    apply_allotment,
)


class DjangoAllotmentRepository(AllotmentRepository):
    def upsert(self, allotment: Allotment) -> None:
        """Insert the allotment; when the room type already has one (or a concurrent call inserts it first),
        lock that row and update it instead."""
        if self._insert_if_absent(allotment):
            return
        row = AllotmentModel.objects.select_for_update().get(pk=allotment.room_type_id)
        apply_allotment(allotment, row)
        row.save(update_fields=ALLOTMENT_UPDATE_FIELDS)

    def get(self, room_type_id: UUID) -> Allotment | None:
        row = AllotmentModel.objects.filter(pk=room_type_id).first()
        return None if row is None else allotment_to_domain(row)

    def all(self) -> list[Allotment]:
        return [allotment_to_domain(row) for row in AllotmentModel.objects.order_by("room_type_id")]

    @staticmethod
    def _insert_if_absent(allotment: Allotment) -> bool:
        if AllotmentModel.objects.filter(pk=allotment.room_type_id).exists():
            return False
        try:
            with transaction.atomic():  # a savepoint, so a lost race leaves the enclosing transaction usable
                allotment_to_model(allotment).save(force_insert=True)
        except IntegrityError:
            return False  # inserted by a concurrent provision of the same room type
        return True
