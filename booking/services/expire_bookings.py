"""Expire unpaid prepaid bookings and release their rooms (LLD §8.4, HLD §6.6).

Run by ``manage.py expire_pending_bookings`` from the scheduler. Candidates are read without locks; each one
is then re-checked and handled in its own unit of work under FOR UPDATE SKIP LOCKED, so a booking being paid
right now is skipped rather than waited on, and one failure never undoes or blocks the others. The pay flow
expires lazily too, so correctness never depends on when this job runs.
"""

import logging
from datetime import datetime
from uuid import UUID

from hotel_commons.domain.clock import Clock
from hotel_commons.domain.unit_of_work import UnitOfWork
from hotel_commons.events.interfaces import EventPublisher

from booking.constants import EXPIRE_BATCH_SIZE
from booking.interfaces.ports import InventoryPort
from booking.interfaces.repositories import BookingRepository
from booking.public.events import BookingExpired

logger = logging.getLogger(__name__)


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

    def execute(self, batch_size: int = EXPIRE_BATCH_SIZE) -> int:
        """Expire up to ``batch_size`` bookings; returns how many were expired."""
        now = self._clock.now()
        candidates = self._bookings.find_expired_pending_ids(now, batch_size)
        expired = sum(self._expire_one(booking_id, now) for booking_id in candidates)
        logger.info("Expired %d of %d candidate booking(s)", expired, len(candidates))
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
