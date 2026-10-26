"""Booking types as policies (LLD §8.2): how a booking starts and whether its hold expires.

A new type (say, deposit now and the rest at the property) is a new ``BookingTypePolicy`` registered here;
the services ask the policy instead of branching on the type.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import ClassVar

from hotel_commons.exceptions import ValidationFailed

from booking.enums import BookingStatus, BookingType
from booking.status_code import INVALID_BOOKING_REQUEST


class BookingTypePolicy(ABC):
    booking_type: ClassVar[BookingType]
    initial_status: ClassVar[BookingStatus]
    requires_online_payment: ClassVar[bool]  # whether POST /bookings/{id}/payments applies at all
    confirms_hold_on_create: ClassVar[bool]  # whether the rooms are committed when the booking is made

    @abstractmethod
    def hold_expires_at(self, now: datetime, hold_ttl: timedelta) -> datetime | None:
        """When the inventory hold lapses unless paid; ``None`` holds the rooms until cancellation."""


class PrepaidBookingPolicy(BookingTypePolicy):
    """Paid online: the rooms are held for ``hold_ttl`` while the guest pays."""

    booking_type = BookingType.PREPAID
    initial_status = BookingStatus.PENDING_PAYMENT
    requires_online_payment = True
    confirms_hold_on_create = False

    def hold_expires_at(self, now: datetime, hold_ttl: timedelta) -> datetime | None:
        return now + hold_ttl


class PayAtPropertyBookingPolicy(BookingTypePolicy):
    """Paid at the property: confirmed at once, and its hold is confirmed with it."""

    booking_type = BookingType.PAY_AT_PROPERTY
    initial_status = BookingStatus.CONFIRMED
    requires_online_payment = False
    confirms_hold_on_create = True

    def hold_expires_at(self, now: datetime, hold_ttl: timedelta) -> datetime | None:
        return None


DEFAULT_BOOKING_TYPE_POLICIES: tuple[BookingTypePolicy, ...] = (PrepaidBookingPolicy(), PayAtPropertyBookingPolicy())


class BookingTypeRegistry:
    def __init__(self, policies: Iterable[BookingTypePolicy] = DEFAULT_BOOKING_TYPE_POLICIES) -> None:
        self._policies = {policy.booking_type: policy for policy in policies}

    def get(self, booking_type: BookingType) -> BookingTypePolicy:
        try:
            return self._policies[booking_type]
        except KeyError:
            reason = f"Booking type {booking_type.name} is not supported"
            raise ValidationFailed(INVALID_BOOKING_REQUEST, reason=reason) from None
