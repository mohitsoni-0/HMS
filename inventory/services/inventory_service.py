"""Inventory use cases (LLD §5.4): provision room types, keep the horizon filled, hold, confirm and release.

Provisioned nights are always ``[today, today + horizon_days)``. A hold first creates any of its nights that
are missing (idempotent), then locks them in ``stay_date`` order, checks and increments the counters and
records the hold, all in one unit of work. One unit of work never locks the nights of more than one hold,
which together with the fixed lock order rules out deadlocks between holds.
"""

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from hotel_commons.domain.clock import Clock
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.unit_of_work import UnitOfWork
from hotel_commons.exceptions import BusinessRuleViolation, Conflict

from inventory.constants import DEFAULT_HORIZON_DAYS
from inventory.domain.allotment import Allotment
from inventory.domain.hold import InventoryHold
from inventory.domain.room_night import ensure_bookable
from inventory.enums import ReleaseReason
from inventory.interfaces.repositories import AllotmentRepository, HoldRepository, RoomNightRepository
from inventory.public.dto import HoldView
from inventory.status_code import INVENTORY_UNAVAILABLE, STAY_OUTSIDE_HORIZON

logger = logging.getLogger(__name__)


class InventoryService:
    def __init__(
        self,
        allotments: AllotmentRepository,
        room_nights: RoomNightRepository,
        holds: HoldRepository,
        uow: UnitOfWork,
        clock: Clock,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
    ) -> None:
        if horizon_days < 1:
            raise ValueError("horizon_days must be at least 1")
        self._allotments = allotments
        self._room_nights = room_nights
        self._holds = holds
        self._uow = uow
        self._clock = clock
        self._horizon_days = horizon_days

    def provision(self, room_type_id: UUID, total_rooms: int) -> None:
        """Record the room type's allotment and create its nights over the horizon."""
        allotment = Allotment(room_type_id=room_type_id, total_rooms=total_rooms)
        with self._uow:
            self._allotments.upsert(allotment)
            self._room_nights.ensure_rows(room_type_id, total_rooms, self._provisioned_dates())
        logger.info("Inventory provisioned room_type_id=%s", room_type_id)

    def extend_horizon(self) -> int:
        """Create the missing nights of every room type up to today's horizon; returns the room types covered.

        Each room type gets its own unit of work, so the daily job never holds one long transaction.
        """
        dates = self._provisioned_dates()
        allotments = self._allotments.all()
        for allotment in allotments:
            with self._uow:
                self._room_nights.ensure_rows(allotment.room_type_id, allotment.total_rooms, dates)
        logger.info("Inventory horizon extended room_types=%d last_night=%s", len(allotments), dates[-1])
        return len(allotments)

    def hold(
        self, *, room_type_id: UUID, stay: DateRange, rooms: int, reference: str, expires_at: datetime | None
    ) -> HoldView:
        """Reserve ``rooms`` rooms on every night of ``stay``.

        Raises BusinessRuleViolation(STAY_OUTSIDE_HORIZON) for a stay outside ``[today, today + horizon)``
        and Conflict(INVENTORY_UNAVAILABLE) when the room type is unknown or any night lacks the rooms.
        """
        self._require_within_horizon(stay)
        with self._uow:
            allotment = self._allotments.get(room_type_id)
            if allotment is None:
                raise Conflict(INVENTORY_UNAVAILABLE)
            self._room_nights.ensure_rows(room_type_id, allotment.total_rooms, stay.dates())
            nights = self._room_nights.lock_for_stay(room_type_id, stay)
            ensure_bookable(nights, stay, rooms)
            self._room_nights.add_reserved(room_type_id, stay, rooms)
            hold = InventoryHold.place(
                room_type_id=room_type_id, stay=stay, rooms=rooms, reference=reference, expires_at=expires_at
            )
            self._holds.add(hold)
        logger.info("Inventory hold placed hold_id=%s room_type_id=%s", hold.id, room_type_id)
        return _view(hold)

    def confirm_hold(self, hold_id: UUID) -> None:
        """HELD -> CONFIRMED (idempotent); a released hold raises Conflict(INVALID_HOLD_TRANSITION)."""
        with self._uow:
            hold = self._holds.get_for_update(hold_id)
            hold.confirm()
            self._holds.save(hold)
        logger.info("Inventory hold confirmed hold_id=%s", hold_id)

    def release_hold(self, hold_id: UUID, reason: ReleaseReason) -> None:
        """Return the hold's rooms to every night of its stay. Releasing a released hold changes nothing."""
        with self._uow:
            hold = self._holds.get_for_update(hold_id)
            if not hold.release(reason, self._clock.now()):
                logger.info("Inventory hold already released hold_id=%s", hold_id)
                return
            self._room_nights.lock_for_stay(hold.room_type_id, hold.stay)
            self._room_nights.add_reserved(hold.room_type_id, hold.stay, -hold.rooms)
            self._holds.save(hold)
        logger.info("Inventory hold released hold_id=%s reason=%s", hold_id, reason.name)

    def availability(self, room_type_ids: Sequence[UUID], stay: DateRange, rooms: int) -> dict[UUID, int]:
        """Room type -> rooms free on every night of ``stay``, only for room types with at least ``rooms`` free."""
        if not room_type_ids:
            return {}
        available = self._room_nights.availability(room_type_ids, stay)
        return {room_type_id: free for room_type_id, free in available.items() if free >= rooms}

    def occupancy(self, room_type_id: UUID, dates: Sequence[date]) -> dict[date, Decimal]:
        """Night -> share of rooms reserved (0..1) for every requested night; a night without a row is 0."""
        return self.occupancy_many([room_type_id], dates)[room_type_id]

    def occupancy_many(self, room_type_ids: Sequence[UUID], dates: Sequence[date]) -> dict[UUID, dict[date, Decimal]]:
        """``occupancy`` for every requested room type in one query: each gets every requested night."""
        known = self._room_nights.occupancy_many(room_type_ids, dates) if room_type_ids else {}
        return {
            room_type_id: {night: known.get(room_type_id, {}).get(night, Decimal(0)) for night in dates}
            for room_type_id in room_type_ids
        }

    def booking_window(self) -> DateRange:
        """The stays inventory sells: first night today or later, check-out at most ``today + horizon_days``.

        ``hold`` refuses any stay outside it; search uses it to refuse such stays up front.
        """
        today = self._clock.today()
        return DateRange(today, today + timedelta(days=self._horizon_days))

    def _provisioned_dates(self) -> tuple[date, ...]:
        return self.booking_window().dates()

    def _require_within_horizon(self, stay: DateRange) -> None:
        window = self.booking_window()
        if stay.start < window.start or stay.end > window.end:
            raise BusinessRuleViolation(STAY_OUTSIDE_HORIZON, days=self._horizon_days)


def _view(hold: InventoryHold) -> HoldView:
    return HoldView(hold_id=hold.id, status=hold.status, expires_at=hold.expires_at)
