"""Pay for a prepaid booking (LLD §8.4, HLD §6.4).

Declined and expired payments are outcomes, not errors: the state they leave (the failure recorded, the
booking expired and its rooms released) must commit, so the service returns them and the view answers
402 or 409 with the booking in ``data``. The durable replay check comes first, under the booking's row lock:
a retry with the same Idempotency-Key gets the original result even after Redis has forgotten the key, and
a key reused with a different method or instrument is refused (422 IDEMPOTENCY_KEY_REUSED) just as the
Redis layer refuses it. Whether a booking can be paid online at all is its type policy's call.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from hotel_commons.domain.clock import Clock
from hotel_commons.domain.unit_of_work import UnitOfWork
from hotel_commons.events.interfaces import EventPublisher
from hotel_commons.exceptions import BusinessRuleViolation, Conflict

from booking.domain.booking import Booking
from booking.domain.booking_types import BookingTypeRegistry
from booking.enums import BookingStatus
from booking.interfaces.ports import ChargeRequest, ChargeResult, InventoryPort, PaymentPort
from booking.interfaces.repositories import BookingRepository
from booking.public.events import BookingConfirmed, BookingExpired
from booking.services.commands import PayBookingCommand
from booking.status_code import BOOKING_NOT_PAYABLE, INVALID_STATE_TRANSITION

logger = logging.getLogger(__name__)

PayOutcome = Literal["PAID", "DECLINED", "EXPIRED"]


@dataclass(frozen=True, slots=True)
class PayBookingResult:
    booking: Booking
    outcome: PayOutcome
    payment: ChargeResult | None


class PayBookingService:
    def __init__(
        self,
        bookings: BookingRepository,
        payments: PaymentPort,
        inventory: InventoryPort,
        booking_types: BookingTypeRegistry,
        events: EventPublisher,
        uow: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._bookings = bookings
        self._payments = payments
        self._inventory = inventory
        self._booking_types = booking_types
        self._events = events
        self._uow = uow
        self._clock = clock

    def execute(self, cmd: PayBookingCommand) -> PayBookingResult:
        key = f"pay:{cmd.booking_id}:{cmd.idempotency_key}"
        with self._uow:
            booking = self._bookings.get_for_update(cmd.booking_id)
            prior = self._payments.find_charge(key, cmd.method, cmd.method_details)
            if prior:
                logger.info("Booking %s payment replayed from payment %s", booking.id, prior.payment_id)
                return PayBookingResult(booking, "PAID" if prior.succeeded else "DECLINED", prior)
            if not self._booking_types.get(booking.booking_type).requires_online_payment:
                raise BusinessRuleViolation(BOOKING_NOT_PAYABLE)
            if booking.status is BookingStatus.EXPIRED:
                return PayBookingResult(booking, "EXPIRED", None)
            now = self._clock.now()
            if booking.status is BookingStatus.PENDING_PAYMENT and booking.hold_expired(now):
                return self._expire(booking, now)
            if booking.status is not BookingStatus.PENDING_PAYMENT:
                raise Conflict(INVALID_STATE_TRANSITION, action="pay", state=booking.status.name)
            result = self._payments.charge(
                ChargeRequest(booking.id, cmd.payer_id, booking.price.total, cmd.method, cmd.method_details, key)
            )
            if result.succeeded:
                return self._confirm(booking, result, now)
            booking.record_payment_failure(result.failure_code)
            self._bookings.save(booking)
            logger.info("Booking %s payment %s declined", booking.id, result.payment_id)
            return PayBookingResult(booking, "DECLINED", result)

    def _expire(self, booking: Booking, now: datetime) -> PayBookingResult:
        booking.expire(now)
        self._inventory.release_hold(booking.hold_id, "EXPIRED")
        self._bookings.save(booking)
        self._events.publish(
            BookingExpired(booking_id=booking.id, guest_id=booking.guest_id, property_id=booking.property_id)
        )
        logger.info("Booking %s expired on a payment attempt", booking.id)
        return PayBookingResult(booking, "EXPIRED", None)

    def _confirm(self, booking: Booking, result: ChargeResult, now: datetime) -> PayBookingResult:
        booking.confirm_payment(result.payment_id, now)
        self._inventory.confirm_hold(booking.hold_id)
        self._bookings.save(booking)
        self._events.publish(
            BookingConfirmed(booking_id=booking.id, guest_id=booking.guest_id, property_id=booking.property_id)
        )
        logger.info("Booking %s confirmed by payment %s", booking.id, result.payment_id)
        return PayBookingResult(booking, "PAID", result)
