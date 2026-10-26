"""Database tests of the Django inventory repositories, the public API and the horizon command (LLD §5).

Every test needs PostgreSQL (docker compose) with the migrations applied.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from uuid import UUID, uuid4

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import transaction

from hotel_commons.domain.clock import SystemClock
from hotel_commons.domain.date_range import DateRange
from hotel_commons.exceptions import Conflict, NotFound

from inventory.domain.allotment import Allotment
from inventory.domain.hold import InventoryHold
from inventory.enums import HoldStatus, ReleaseReason
from inventory.models.inventory_models import Allotment as AllotmentModel
from inventory.models.inventory_models import InventoryHold as InventoryHoldModel
from inventory.models.inventory_models import InventoryHoldLog
from inventory.models.inventory_models import RoomNight as RoomNightModel
from inventory.public.api import get_inventory_api
from inventory.repositories.allotment_repository import DjangoAllotmentRepository
from inventory.repositories.hold_repository import DjangoHoldRepository
from inventory.repositories.room_night_repository import DjangoRoomNightRepository
from inventory.status_code import HOLD_NOT_FOUND, INVENTORY_UNAVAILABLE

pytestmark = pytest.mark.django_db


def _stay(first_night_offset: int, nights: int) -> DateRange:
    start = SystemClock().today() + timedelta(days=first_night_offset)
    return DateRange(start, start + timedelta(days=nights))


def _provisioned(total_rooms: int, stay: DateRange) -> UUID:
    room_type_id = uuid4()
    DjangoRoomNightRepository().ensure_rows(room_type_id, total_rooms, stay.dates())
    return room_type_id


def _place(room_type_id: UUID, stay: DateRange, rooms: int = 1) -> InventoryHold:
    return InventoryHold.place(
        room_type_id=room_type_id, stay=stay, rooms=rooms, reference=str(uuid4()), expires_at=None
    )


def test_allotment_upsert_inserts_then_updates() -> None:
    repository = DjangoAllotmentRepository()
    room_type_id = uuid4()

    repository.upsert(Allotment(room_type_id=room_type_id, total_rooms=3))
    repository.upsert(Allotment(room_type_id=room_type_id, total_rooms=5))

    assert repository.get(room_type_id) == Allotment(room_type_id=room_type_id, total_rooms=5)
    assert AllotmentModel.objects.filter(pk=room_type_id).count() == 1
    assert AllotmentModel.objects.get(pk=room_type_id).updation_date is not None
    assert repository.get(uuid4()) is None
    assert Allotment(room_type_id=room_type_id, total_rooms=5) in repository.all()


def test_ensure_rows_is_idempotent_and_keeps_counters() -> None:
    repository = DjangoRoomNightRepository()
    stay = _stay(1, 3)
    room_type_id = _provisioned(2, stay)
    repository.add_reserved(room_type_id, stay, 1)

    repository.ensure_rows(room_type_id, 9, (*stay.dates(), stay.end))

    rows = list(RoomNightModel.objects.filter(room_type_id=room_type_id).order_by("stay_date"))
    assert [row.stay_date for row in rows] == [*stay.dates(), stay.end]
    assert [(row.total_rooms, row.reserved_rooms) for row in rows] == [(2, 1), (2, 1), (2, 1), (9, 0)]


def test_lock_for_stay_returns_nights_in_date_order() -> None:
    repository = DjangoRoomNightRepository()
    stay = _stay(1, 3)
    room_type_id = _provisioned(2, stay)

    with transaction.atomic():
        nights = repository.lock_for_stay(room_type_id, stay)

    assert [night.stay_date for night in nights] == list(stay.dates())
    assert all(night.available == 2 for night in nights)


def test_add_reserved_beyond_total_conflicts_and_keeps_transaction_usable() -> None:
    repository = DjangoRoomNightRepository()
    stay = _stay(1, 2)
    room_type_id = _provisioned(1, stay)

    with transaction.atomic():
        repository.add_reserved(room_type_id, stay, 1)
        with pytest.raises(Conflict) as error:
            repository.add_reserved(room_type_id, stay, 1)
        assert error.value.status_code is INVENTORY_UNAVAILABLE
        assert repository.availability([room_type_id], stay) == {room_type_id: 0}  # the transaction still works

    with pytest.raises(Conflict):
        repository.add_reserved(room_type_id, stay, -2)  # below zero violates the CHECK too


def test_add_reserved_conflicts_when_a_night_is_missing() -> None:
    repository = DjangoRoomNightRepository()
    stay = _stay(1, 3)
    room_type_id = _provisioned(2, _stay(1, 2))

    with pytest.raises(Conflict):
        repository.add_reserved(room_type_id, stay, 1)

    reserved = RoomNightModel.objects.filter(room_type_id=room_type_id).values_list("reserved_rooms", flat=True)
    assert set(reserved) == {0}


def test_availability_needs_every_night_and_reports_the_minimum() -> None:
    repository = DjangoRoomNightRepository()
    stay = _stay(1, 3)
    full_room_type_id = _provisioned(3, stay)
    partial_room_type_id = _provisioned(3, _stay(1, 2))
    repository.add_reserved(full_room_type_id, _stay(2, 1), 2)

    available = repository.availability([full_room_type_id, partial_room_type_id, uuid4()], stay)

    assert available == {full_room_type_id: 1}


def test_occupancy_is_reserved_share() -> None:
    repository = DjangoRoomNightRepository()
    stay = _stay(1, 2)
    room_type_id = _provisioned(4, stay)
    repository.add_reserved(room_type_id, _stay(1, 1), 3)

    occupancy = repository.occupancy(room_type_id, stay.dates())

    assert occupancy == {stay.start: Decimal("0.75"), stay.start + timedelta(days=1): Decimal(0)}


def test_hold_round_trip_writes_log_rows() -> None:
    repository = DjangoHoldRepository()
    hold = _place(uuid4(), _stay(1, 2), rooms=2)

    with transaction.atomic():
        repository.add(hold)
        loaded = repository.get_for_update(hold.id)
        loaded.release(ReleaseReason.CANCELLED, SystemClock().now())
        repository.save(loaded)

    with transaction.atomic():
        stored = repository.get_for_update(hold.id)
    assert stored.status is HoldStatus.RELEASED
    assert stored.release_reason is ReleaseReason.CANCELLED
    assert stored.stay == hold.stay
    assert stored.rooms == 2
    log_rows = InventoryHoldLog.objects.filter(log_id=hold.id).order_by("creation_date", "id")
    statuses = list(log_rows.values_list("hold_status", flat=True))
    assert statuses == [HoldStatus.HELD, HoldStatus.RELEASED]


def test_unknown_hold_is_not_found() -> None:
    with transaction.atomic(), pytest.raises(NotFound) as error:
        DjangoHoldRepository().get_for_update(uuid4())

    assert error.value.status_code is HOLD_NOT_FOUND


def test_api_hold_confirm_and_release_by_name() -> None:
    api = get_inventory_api()
    room_type_id = uuid4()
    stay = _stay(1, 2)
    api.provision(room_type_id, 1)

    view = api.hold(room_type_id=room_type_id, stay=stay, rooms=1, reference="booking-1", expires_at=None)
    api.confirm_hold(view.hold_id)
    with pytest.raises(Conflict):
        api.hold(room_type_id=room_type_id, stay=stay, rooms=1, reference="booking-2", expires_at=None)
    api.release_hold(view.hold_id, "CANCELLED")
    api.release_hold(view.hold_id, ReleaseReason.EXPIRED)

    assert api.availability([room_type_id], stay, 1) == {room_type_id: 1}
    assert InventoryHoldModel.objects.get(pk=view.hold_id).release_reason is ReleaseReason.CANCELLED
    assert InventoryHoldModel.objects.filter(reference="booking-2").exists() is False


def test_extend_inventory_horizon_command_creates_missing_nights() -> None:
    room_type_id = uuid4()
    DjangoAllotmentRepository().upsert(Allotment(room_type_id=room_type_id, total_rooms=2))
    output = StringIO()

    call_command("extend_inventory_horizon", stdout=output)

    assert "1 room type" in output.getvalue()
    assert RoomNightModel.objects.filter(room_type_id=room_type_id).count() == settings.INVENTORY_HORIZON_DAYS
