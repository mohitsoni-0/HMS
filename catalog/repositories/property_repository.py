"""Django ``PropertyRepository`` (LLD §4.3, §2.4, §2.5): a property and its room types."""

import logging
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from hotel_commons.exceptions import Conflict, NotFound

from catalog.domain.property import Property as PropertyEntity
from catalog.domain.room_type import RoomType as RoomTypeEntity
from catalog.interfaces.repositories import PropertyRepository
from catalog.models.catalog_models import Property, RoomType
from catalog.repositories.mappers import property_to_domain, property_to_model, room_type_to_model
from catalog.status_code import DUPLICATE_ROOM_TYPE_NAME, PROPERTY_NOT_FOUND, ROOM_TYPE_NOT_FOUND

logger = logging.getLogger(__name__)

ROOM_TYPE_NAME_CONSTRAINT = "catalog_room_type_property_name_uniq"


class DjangoPropertyRepository(PropertyRepository):
    def add(self, property_: PropertyEntity) -> None:
        with transaction.atomic():
            property_to_model(property_).save(force_insert=True)
            for room_type in property_.room_types:
                room_type_to_model(room_type).save(force_insert=True)

    def add_room_type(self, room_type: RoomTypeEntity) -> None:
        try:
            with transaction.atomic():  # savepoint: a duplicate leaves the caller's transaction usable
                room_type_to_model(room_type).save(force_insert=True)
        except IntegrityError as error:
            if _violated_constraint(error) != ROOM_TYPE_NAME_CONSTRAINT:
                raise
            logger.info("Room type %s rejected: name taken in property %s", room_type.id, room_type.property_id)
            raise Conflict(DUPLICATE_ROOM_TYPE_NAME, name=room_type.name) from None

    def get(self, property_id: UUID, *, for_update: bool = False) -> PropertyEntity:
        rows = Property.objects.select_for_update() if for_update else Property.objects.all()
        row = rows.get_or_not_found(PROPERTY_NOT_FOUND, pk=property_id)
        return property_to_domain(row, _room_types_of(row.id))

    def list_for_account(self, account_id: UUID) -> list[PropertyEntity]:
        rows = (
            Property.objects.filter(owner_account_id=account_id)
            .order_by("creation_date", "id")
            .prefetch_related(Prefetch("room_types", queryset=RoomType.objects.order_by("creation_date", "id")))
        )
        return [property_to_domain(row, row.room_types.all()) for row in rows]

    def get_room_type(self, room_type_id: UUID) -> tuple[PropertyEntity, RoomTypeEntity]:
        property_id = RoomType.objects.filter(pk=room_type_id).values_list("property_id", flat=True).first()
        if property_id is None:
            raise NotFound(ROOM_TYPE_NOT_FOUND)
        property_ = self.get(property_id)
        room_type = property_.room_type(room_type_id)
        if room_type is None:  # removed between the two reads; room types are never deleted today
            raise NotFound(ROOM_TYPE_NOT_FOUND)
        return property_, room_type


def _room_types_of(property_id: UUID) -> list[RoomType]:
    return list(RoomType.objects.filter(property_id=property_id).order_by("creation_date", "id"))


def _violated_constraint(error: IntegrityError) -> str | None:
    """The name of the constraint the database reported (psycopg 3 diagnostics), if any."""
    diagnostics = getattr(error.__cause__, "diag", None)
    return getattr(diagnostics, "constraint_name", None)
