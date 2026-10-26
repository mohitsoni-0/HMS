"""Expire unpaid prepaid bookings and release their rooms (LLD §8.4, HLD §6.6).

Run by ``manage.py expire_pending_bookings`` from the scheduler. Candidates are read without locks, a batch
at a time, until a batch comes back short or the run's time budget is spent; each one is then re-checked and
handled in its own unit of work under FOR UPDATE SKIP LOCKED, so a booking being paid right now is skipped
rather than waited on, and one failure never undoes or blocks the others. A booking that failed or was
skipped is not picked again in the same run, so a few bad rows cannot fill every batch.

What depends on this job, precisely: the booking and payment state never does, because the pay flow expires
a lapsed hold itself and never charges for it. Availability does: inventory keeps a lapsed hold's rooms
reserved until this job releases them, so with the scheduler running they are back on sale within one
scheduler interval (about 60 s) of the hold's expiry.
"""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from hotel_commons.domain.clock import Clock
from hotel_commons.domain.unit_of_work import UnitOfWork
from hotel_commons.events.interfaces import EventPublisher

from booking.constants import EXPIRE_BATCH_SIZE, EXPIRE_TIME_BUDGET_SECONDS
from booking.interfaces.ports import InventoryPort
from booking.interfaces.repositories import BookingRepository
from booking.public.events import BookingExpired

logger = logging.getLogger(__name__)

DEFAULT_TIME_BUDGET = timedelta(seconds=EXPIRE_TIME_BUDGET_SECONDS)


class ExpireBookingsService:
    def __init__(
        self,
        bookings: BookingRepository,
        inventory: InventoryPort,
        events: EventPublisher,
        uow: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._bookings = bookings
        self._inventory = inventory
        self._events = events
        self._uow = uow
        self._clock = clock

    def execute(self, batch_size: int = EXPIRE_BATCH_SIZE, time_budget: timedelta = DEFAULT_TIME_BUDGET) -> int:
        """Expire lapsed bookings, ``batch_size`` at a time, until a batch is short or ``time_budget`` has
        passed (checked between batches, so at least one batch runs); returns how many were expired."""
        deadline = self._clock.now() + time_budget
        not_expired: set[UUID] = set()  # failed or skipped in this run: never picked again by it
        expired = 0
        while True:
            now = self._clock.now()
            candidates = self._bookings.find_expired_pending_ids(now, batch_size, exclude=not_expired)
            for booking_id in candidates:
                if self._expire_one(booking_id, now):
                    expired += 1
                else:
                    not_expired.add(booking_id)
            if len(candidates) < batch_size or self._clock.now() >= deadline:
                break
        logger.info("Expired %d booking(s); %d left for the next run", expired, len(not_expired))
        return expired

    def _expire_one(self, booking_id: UUID, now: datetime) -> bool:
        try:
            with self._uow:
                booking = self._bookings.lock_if_expired_pending(booking_id, now)
                if booking is None:
                    return False  # paid, cancelled or expired meanwhile, or locked by another worker
                booking.expire(now)
                self._inventory.release_hold(booking.hold_id, "EXPIRED")
                self._bookings.save(booking)
                self._events.publish(
                    BookingExpired(booking_id=booking.id, guest_id=booking.guest_id, property_id=booking.property_id)
                )
        except Exception:  # one bad booking must not stop the batch; its unit of work has rolled back
            logger.exception("Could not expire booking %s; it will be retried on the next run", booking_id)
            return False
        logger.info("Booking %s expired", booking_id)
        return True
