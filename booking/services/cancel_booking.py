"""Cancel a booking, refunding by its snapshotted policy (LLD §8.4, HLD §6.5).

The cancel, the inventory release and the refund are one unit of work. A refund the gateway rejects raises
UpstreamFailure (502) and rolls everything back: the booking stays CONFIRMED with its rooms held and no
refund row exists, so the client simply retries. The refund's idempotency key is derived from the booking,
so a retry can never refund twice.
"""

import logging
from datetime import datetime

from hotel_commons.domain.clock import Clock
from hotel_commons.domain.unit_of_work import UnitOfWork
from hotel_commons.events.interfaces import EventPublisher
from hotel_commons.exceptions import BusinessRuleViolation

from booking.constants import REFUND_REASON
from booking.domain.booking import Booking
from booking.domain.cancellation import CancellationPolicyRegistry, RefundDecision
from booking.enums import BookingStatus
from booking.interfaces.ports import InventoryPort, PaymentPort, RefundRequest
from booking.interfaces.repositories import BookingRepository
from booking.public.events import BookingCancelled
from booking.services.commands import CancelBookingCommand
from booking.status_code import CANCELLATION_WINDOW_CLOSED

logger = logging.getLogger(__name__)


class CancelBookingService:
    def __init__(
        self,
        bookings: BookingRepository,
        inventory: InventoryPort,
        payments: PaymentPort,
        policies: CancellationPolicyRegistry,
        events: EventPublisher,
        uow: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._bookings = bookings
        self._inventory = inventory
        self._payments = payments
        self._policies = policies
        self._events = events
        self._uow = uow
        self._clock = clock

    def execute(self, cmd: CancelBookingCommand) -> Booking:
        with self._uow:
            booking = self._bookings.get_for_update(cmd.booking_id)
            now = self._clock.now()
            if now >= booking.check_in_at():
                raise BusinessRuleViolation(CANCELLATION_WINDOW_CLOSED)
            decision = self._refund_decision(booking, now)
            booking.cancel(now=now, reason=cmd.reason, refund=decision)  # the state machine guards the transition
            self._inventory.release_hold(booking.hold_id, "CANCELLED")
            if decision.amount.is_positive():
                refund = self._payments.refund(
                    RefundRequest(
                        booking.payment_id, decision.amount, REFUND_REASON, idempotency_key=f"refund:{booking.id}"
                    )
                )
                booking.record_refund(refund.refund_id)
            self._bookings.save(booking)
            self._events.publish(
                BookingCancelled(booking_id=booking.id, guest_id=booking.guest_id, property_id=booking.property_id)
            )
        logger.info("Booking %s cancelled by %s (%s)", booking.id, cmd.actor_id, decision.rule)
        return booking

    def _refund_decision(self, booking: Booking, now: datetime) -> RefundDecision:
        """The policy applies only to money actually paid online; anything else refunds nothing."""
        if booking.status is not BookingStatus.CONFIRMED or booking.payment_id is None:
            return RefundDecision.nothing()
        policy = self._policies.get(booking.cancellation_policy)
        return policy.evaluate(amount_paid=booking.price.total, check_in_at=booking.check_in_at(), cancelled_at=now)
