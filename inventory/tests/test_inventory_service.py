"""Unit tests of ``InventoryService`` over the in-memory repositories (LLD §5, §14).

Pure Python: no Django settings and no database. The clock is fixed at 2026-01-05 12:00 Asia/Kolkata and the
horizon is 30 days, so the provisioned nights are 2026-01-05 .. 2026-02-03.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from hotel_commons.domain.date_range import DateRange
from hotel_commons.exceptions import BusinessRuleViolation, Conflict, NotFound, ValidationFailed
from hotel_commons.testing.clock import FixedClock
from hotel_commons.testing.unit_of_work import NoopUnitOfWork

from inventory.enums import HoldStatus, ReleaseReason
from inventory.services.inventory_service import InventoryService
from inventory.status_code import HOLD_NOT_FOUND, INVALID_HOLD_TRANSITION, INVENTORY_UNAVAILABLE, STAY_OUTSIDE_HORIZON
from inventory.tests.fakes import InMemoryAllotmentRepository, InMemoryHoldRepository, InMemoryRoomNightRepository

HORIZON_DAYS = 30


@dataclass
class Harness:
    service: InventoryService
    allotments: InMemoryAllotmentRepository
    room_nights: InMemoryRoomNightRepository
    holds: InMemoryHoldRepository
    uow: NoopUnitOfWork
    clock: FixedClock

    @property
    def today(self) -> date:
        return self.clock.today()

    def stay(self, first_night_offset: int, nights: int) -> DateRange:
        start = self.today + timedelta(days=first_night_offset)
        return DateRange(start, start + timedelta(days=nights))

    def hold(self, room_type_id: UUID, stay: DateRange, rooms: int = 1):
        return self.service.hold(
            room_type_id=room_type_id,
            stay=stay,
            rooms=rooms,
            reference=str(uuid4()),
            expires_at=self.clock.now() + timedelta(minutes=15),
        )


@pytest.fixture
def harness() -> Harness:
    allotments = InMemoryAllotmentRepository()
    room_nights = InMemoryRoomNightRepository()
    holds = InMemoryHoldRepository()
    uow = NoopUnitOfWork()
    clock = FixedClock()
    service = InventoryService(allotments, room_nights, holds, uow, clock, horizon_days=HORIZON_DAYS)
    return Harness(service, allotments, room_nights, holds, uow, clock)


@pytest.fixture
def room_type_id(harness: Harness) -> UUID:
    room_type_id = uuid4()
    harness.service.provision(room_type_id, 2)
    return room_type_id


def test_provision_creates_every_night_of_the_horizon(harness: Harness, room_type_id: UUID) -> None:
    last_night = harness.today + timedelta(days=HORIZON_DAYS - 1)

    assert harness.allotments.get(room_type_id).total_rooms == 2
    assert harness.room_nights.night(room_type_id, harness.today).total_rooms == 2
    assert harness.room_nights.night(room_type_id, last_night) is not None
    assert harness.room_nights.night(room_type_id, last_night + timedelta(days=1)) is None


def test_hold_then_release_restores_availability(harness: Harness, room_type_id: UUID) -> None:
    stay = harness.stay(1, 2)

    view = harness.hold(room_type_id, stay, rooms=2)

    assert view.status is HoldStatus.HELD
    assert view.expires_at == harness.clock.now() + timedelta(minutes=15)
    assert harness.service.availability([room_type_id], stay, 1) == {}

    harness.service.release_hold(view.hold_id, ReleaseReason.CANCELLED)

    assert harness.service.availability([room_type_id], stay, 1) == {room_type_id: 2}
    released = harness.holds.get_for_update(view.hold_id)
    assert released.status is HoldStatus.RELEASED
    assert released.release_reason is ReleaseReason.CANCELLED
    assert released.released_at == harness.clock.now()


def test_release_is_idempotent(harness: Harness, room_type_id: UUID) -> None:
    stay = harness.stay(1, 2)
    view = harness.hold(room_type_id, stay)

    harness.service.release_hold(view.hold_id, ReleaseReason.EXPIRED)
    harness.service.release_hold(view.hold_id, ReleaseReason.CANCELLED)

    assert harness.service.availability([room_type_id], stay, 1) == {room_type_id: 2}
    assert harness.holds.get_for_update(view.hold_id).release_reason is ReleaseReason.EXPIRED


def test_release_of_a_confirmed_hold_returns_the_rooms(harness: Harness, room_type_id: UUID) -> None:
    stay = harness.stay(3, 1)
    view = harness.hold(room_type_id, stay, rooms=2)
    harness.service.confirm_hold(view.hold_id)

    harness.service.release_hold(view.hold_id, ReleaseReason.CANCELLED)

    assert harness.service.availability([room_type_id], stay, 2) == {room_type_id: 2}


def test_hold_rejects_when_any_night_full(harness: Harness, room_type_id: UUID) -> None:
    harness.hold(room_type_id, harness.stay(2, 1), rooms=2)  # the middle night is sold out
    holds_before = dict(harness.holds.rows)

    with pytest.raises(Conflict) as error:
        harness.hold(room_type_id, harness.stay(1, 3))

    assert error.value.status_code is INVENTORY_UNAVAILABLE
    assert harness.holds.rows == holds_before
    assert harness.room_nights.night(room_type_id, harness.today + timedelta(days=1)).reserved_rooms == 0
    assert harness.uow.rollbacks == 1


def test_hold_without_allotment_conflicts(harness: Harness) -> None:
    with pytest.raises(Conflict) as error:
        harness.hold(uuid4(), harness.stay(1, 1))

    assert error.value.status_code is INVENTORY_UNAVAILABLE


def test_hold_creates_missing_rows_within_horizon(harness: Harness, room_type_id: UUID) -> None:
    harness.clock.advance(timedelta(days=10))  # provisioned 10 days ago; the horizon job has not run since
    last_stay = harness.stay(HORIZON_DAYS - 2, 2)  # ends on the last day of today's horizon
    assert harness.room_nights.night(room_type_id, last_stay.start + timedelta(days=1)) is None

    view = harness.hold(room_type_id, last_stay)

    assert view.status is HoldStatus.HELD
    for night in last_stay.dates():
        assert harness.room_nights.night(room_type_id, night).reserved_rooms == 1
        assert harness.room_nights.night(room_type_id, night).total_rooms == 2


@pytest.mark.parametrize(("first_night_offset", "nights"), [(-1, 2), (HORIZON_DAYS - 1, 2), (HORIZON_DAYS, 1)])
def test_stay_outside_horizon_is_rejected(
    harness: Harness, room_type_id: UUID, first_night_offset: int, nights: int
) -> None:
    with pytest.raises(BusinessRuleViolation) as error:
        harness.hold(room_type_id, harness.stay(first_night_offset, nights))

    assert error.value.status_code is STAY_OUTSIDE_HORIZON
    assert error.value.message == f"Bookings can be made up to {HORIZON_DAYS} days ahead"
    assert harness.holds.rows == {}


def test_hold_rejects_zero_rooms(harness: Harness, room_type_id: UUID) -> None:
    with pytest.raises(ValidationFailed):
        harness.hold(room_type_id, harness.stay(1, 1), rooms=0)

    assert harness.service.availability([room_type_id], harness.stay(1, 1), 2) == {room_type_id: 2}


def test_confirm_hold_is_idempotent(harness: Harness, room_type_id: UUID) -> None:
    stay = harness.stay(1, 1)
    view = harness.hold(room_type_id, stay)

    harness.service.confirm_hold(view.hold_id)
    harness.service.confirm_hold(view.hold_id)

    assert harness.holds.get_for_update(view.hold_id).status is HoldStatus.CONFIRMED
    assert harness.service.availability([room_type_id], stay, 1) == {room_type_id: 1}


def test_confirm_released_hold_conflicts(harness: Harness, room_type_id: UUID) -> None:
    view = harness.hold(room_type_id, harness.stay(1, 1))
    harness.service.release_hold(view.hold_id, ReleaseReason.EXPIRED)

    with pytest.raises(Conflict) as error:
        harness.service.confirm_hold(view.hold_id)

    assert error.value.status_code is INVALID_HOLD_TRANSITION
    assert error.value.message == "Hold is RELEASED"
    assert harness.holds.get_for_update(view.hold_id).status is HoldStatus.RELEASED


def test_unknown_hold_is_not_found(harness: Harness) -> None:
    with pytest.raises(NotFound) as error:
        harness.service.release_hold(uuid4(), ReleaseReason.CANCELLED)

    assert error.value.status_code is HOLD_NOT_FOUND


def test_extend_horizon_adds_missing_dates(harness: Harness, room_type_id: UUID) -> None:
    other_room_type_id = uuid4()
    harness.service.provision(other_room_type_id, 5)
    harness.clock.advance(timedelta(days=3))
    new_nights = [harness.today + timedelta(days=HORIZON_DAYS - offset) for offset in (3, 2, 1)]

    extended = harness.service.extend_horizon()

    assert extended == 2
    for night in new_nights:
        assert harness.room_nights.night(room_type_id, night).total_rooms == 2
        assert harness.room_nights.night(other_room_type_id, night).total_rooms == 5
    assert harness.room_nights.night(room_type_id, harness.today + timedelta(days=HORIZON_DAYS)) is None


def test_extend_horizon_keeps_reserved_counters(harness: Harness, room_type_id: UUID) -> None:
    stay = harness.stay(1, 1)
    harness.hold(room_type_id, stay)

    harness.service.extend_horizon()

    assert harness.room_nights.night(room_type_id, stay.start).reserved_rooms == 1


def test_availability_requires_every_night(harness: Harness, room_type_id: UUID) -> None:
    harness.clock.advance(timedelta(days=5))  # the last 5 nights of today's horizon have no rows yet
    unprovisioned_tail = harness.stay(HORIZON_DAYS - 7, 4)
    fully_provisioned = harness.stay(HORIZON_DAYS - 9, 4)

    assert harness.service.availability([room_type_id], unprovisioned_tail, 1) == {}
    assert harness.service.availability([room_type_id], fully_provisioned, 1) == {room_type_id: 2}


def test_availability_keeps_only_room_types_with_enough_rooms(harness: Harness, room_type_id: UUID) -> None:
    roomy_room_type_id = uuid4()
    harness.service.provision(roomy_room_type_id, 4)
    stay = harness.stay(1, 2)
    harness.hold(roomy_room_type_id, harness.stay(2, 1))

    available = harness.service.availability([room_type_id, roomy_room_type_id, uuid4()], stay, 3)

    assert available == {roomy_room_type_id: 3}
    assert harness.service.availability([], stay, 1) == {}


def test_occupancy_reports_reserved_share_per_night(harness: Harness, room_type_id: UUID) -> None:
    held_night = harness.today + timedelta(days=1)
    free_night = harness.today + timedelta(days=2)
    unprovisioned_night = harness.today + timedelta(days=HORIZON_DAYS)
    harness.hold(room_type_id, harness.stay(1, 1))

    occupancy = harness.service.occupancy(room_type_id, [held_night, free_night, unprovisioned_night])

    assert occupancy == {held_night: Decimal("0.5"), free_night: Decimal(0), unprovisioned_night: Decimal(0)}


def test_hold_locks_the_nights_of_its_stay(harness: Harness, room_type_id: UUID) -> None:
    stay = harness.stay(1, 2)

    harness.hold(room_type_id, stay)

    assert harness.room_nights.locked == [(room_type_id, stay)]
    assert harness.uow.commits >= 1
