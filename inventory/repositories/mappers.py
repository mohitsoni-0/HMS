"""Model <-> domain mapping for the inventory repositories. Model instances never leave ``repositories/``."""

from datetime import date
from typing import Final
from uuid import UUID

from hotel_commons.domain.date_range import DateRange

from inventory.domain.allotment import Allotment
from inventory.domain.hold import InventoryHold
from inventory.domain.room_night import RoomNight
from inventory.models.inventory_models import Allotment as AllotmentModel
from inventory.models.inventory_models import InventoryHold as InventoryHoldModel
from inventory.models.inventory_models import RoomNight as RoomNightModel

# Columns ``apply_allotment`` / ``apply_hold`` copy onto a loaded row: the update_fields of a repository save.
ALLOTMENT_UPDATE_FIELDS: Final = ("total_rooms",)
HOLD_UPDATE_FIELDS: Final = ("hold_status", "expires_at", "release_reason", "released_at")


def allotment_to_domain(row: AllotmentModel) -> Allotment:
    return Allotment(room_type_id=row.room_type_id, total_rooms=row.total_rooms)


def allotment_to_model(allotment: Allotment) -> AllotmentModel:
    return AllotmentModel(room_type_id=allotment.room_type_id, total_rooms=allotment.total_rooms)


def apply_allotment(allotment: Allotment, row: AllotmentModel) -> None:
    row.total_rooms = allotment.total_rooms


def room_night_to_domain(row: RoomNightModel) -> RoomNight:
    return RoomNight(
        room_type_id=row.room_type_id,
        stay_date=row.stay_date,
        total_rooms=row.total_rooms,
        reserved_rooms=row.reserved_rooms,
    )


def new_room_night_model(room_type_id: UUID, stay_date: date, total_rooms: int) -> RoomNightModel:
    return RoomNightModel(room_type_id=room_type_id, stay_date=stay_date, total_rooms=total_rooms, reserved_rooms=0)


def hold_to_domain(row: InventoryHoldModel) -> InventoryHold:
    return InventoryHold(
        id=row.id,
        room_type_id=row.room_type_id,
        stay=DateRange(row.check_in, row.check_out),
        rooms=row.rooms,
        reference=row.reference,
        status=row.hold_status,
        expires_at=row.expires_at,
        release_reason=row.release_reason,
        released_at=row.released_at,
    )


def hold_to_model(hold: InventoryHold) -> InventoryHoldModel:
    return InventoryHoldModel(
        id=hold.id,
        room_type_id=hold.room_type_id,
        check_in=hold.stay.start,
        check_out=hold.stay.end,
        rooms=hold.rooms,
        reference=hold.reference,
        hold_status=hold.status,
        expires_at=hold.expires_at,
        release_reason=hold.release_reason,
        released_at=hold.released_at,
    )


def apply_hold(hold: InventoryHold, row: InventoryHoldModel) -> None:
    row.hold_status = hold.status
    row.expires_at = hold.expires_at
    row.release_reason = hold.release_reason
    row.released_at = hold.released_at
