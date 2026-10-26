"""The booking aggregate (LLD §8.2).

A booking is one room type x ``rooms`` identical rooms for a stay (assumption A3). It snapshots the price,
the cancellation policy and the check-in time when it is made, so later catalog edits never change its
terms. Every status change goes through ``BookingStateMachine``.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from catalog.public.enums import CancellationPolicyCode
from hotel_commons.domain.clock import BUSINESS_TIME_ZONE
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money
from hotel_commons.exceptions import BusinessRuleViolation, ValidationFailed
from uuid6 import uuid7

from booking.constants import MAX_ROOMS_PER_BOOKING
from booking.domain.booking_types import BookingTypePolicy
from booking.domain.cancellation import RefundDecision
from booking.domain.state_machine import BookingStateMachine
from booking.domain.value_objects import BookingPrice, GuestContact, RoomTypeInfo
from booking.enums import BookingRefundStatus, BookingStatus, BookingTrigger, BookingType
from booking.status_code import GUEST_CAPACITY_EXCEEDED, INVALID_BOOKING_REQUEST, ROOM_TYPE_NOT_BOOKABLE

_PERCENT_PLACES: Final = Decimal("0.01")  # refund_percent is numeric(5,2)


class BookingLimits(Protocol):
    """The limits ``validate_request`` reads; ``booking.services.settings.BookingSettings`` provides them."""

    @property
    def max_nights(self) -> int: ...

    @property
    def horizon_days(self) -> int: ...


@dataclass(slots=True)
class Booking:
    id: UUID
    guest_id: str
    property_id: UUID
    room_type_id: UUID
    owner_account_id: UUID
    stay: DateRange
    rooms: int
    guests: int
    booking_type: BookingType
    status: BookingStatus
    price: BookingPrice
    cancellation_policy: CancellationPolicyCode
    check_in_time: time
    contact: GuestContact
    created_at: datetime
    hold_id: UUID | None = None
    hold_expires_at: datetime | None = None
    payment_id: UUID | None = None
    last_payment_failure: str | None = None
    refund_amount: Money | None = None
    refund_percent: Decimal | None = None
    refund_status: BookingRefundStatus = BookingRefundStatus.NONE
    refund_id: UUID | None = None
    cancellation_reason: str | None = None
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
    expired_at: datetime | None = None
    idempotency_key: str | None = None  # the client's key for the create request, unique per guest

    @staticmethod
    def validate_request(
        *, room_type: RoomTypeInfo, stay: DateRange, rooms: int, guests: int, today: date, settings: BookingLimits
    ) -> None:
        """Reject a request that can never be booked; cheap, so it runs before any pricing or inventory work."""
        if not room_type.is_active:
            raise BusinessRuleViolation(ROOM_TYPE_NOT_BOOKABLE)
        if stay.nights > settings.max_nights:
            raise _invalid_request(f"A stay can be at most {settings.max_nights} nights")
        if stay.start < today:
            raise _invalid_request("check_in cannot be in the past")
        if stay.end > today + timedelta(days=settings.horizon_days):
            raise _invalid_request(f"Bookings can be made up to {settings.horizon_days} days ahead")
        max_rooms = min(MAX_ROOMS_PER_BOOKING, room_type.total_rooms)
        if not 1 <= rooms <= max_rooms:
            raise _invalid_request(f"rooms must be between 1 and {max_rooms}")
        if guests < 1:
            raise _invalid_request("guests must be at least 1")
        capacity = rooms * room_type.max_occupancy
        if guests > capacity:
            raise BusinessRuleViolation(GUEST_CAPACITY_EXCEEDED, guests=guests, capacity=capacity)

    @classmethod
    def request(
        cls,
        *,
        guest_id: str,
        room_type: RoomTypeInfo,
        stay: DateRange,
        rooms: int,
        guests: int,
        type_policy: BookingTypePolicy,
        contact: GuestContact,
        price: BookingPrice,
        now: datetime,
        idempotency_key: str | None = None,
    ) -> "Booking":
        """A new booking in the type's initial status, with the room type's terms snapshotted."""
        status = type_policy.initial_status
        return cls(
            id=uuid7(),
            guest_id=guest_id,
            property_id=room_type.property_id,
            room_type_id=room_type.room_type_id,
            owner_account_id=room_type.owner_account_id,
            stay=stay,
            rooms=rooms,
            guests=guests,
            booking_type=type_policy.booking_type,
            status=status,
            price=price,
            cancellation_policy=room_type.cancellation_policy,
            check_in_time=room_type.check_in_time,
            contact=contact,
            created_at=now,
            confirmed_at=now if status is BookingStatus.CONFIRMED else None,
            idempotency_key=idempotency_key,
        )

    def attach_hold(self, hold_id: UUID, expires_at: datetime | None) -> None:
        self.hold_id = hold_id
        self.hold_expires_at = expires_at

    def hold_expired(self, now: datetime) -> bool:
        """True from the expiry instant on: ``now == hold_expires_at`` already counts as expired."""
        return self.hold_expires_at is not None and now >= self.hold_expires_at

    def check_in_at(self) -> datetime:
        """The check-in instant: the first night's date at the snapshotted check-in time, in Asia/Kolkata."""
        return datetime.combine(self.stay.start, self.check_in_time, tzinfo=BUSINESS_TIME_ZONE)

    def confirm_payment(self, payment_id: UUID, now: datetime) -> None:
        self.status = BookingStateMachine.next_state(self.status, BookingTrigger.CONFIRM_PAYMENT)
        self.payment_id = payment_id
        self.confirmed_at = now
        self.last_payment_failure = None

    def record_payment_failure(self, reason: str) -> None:
        """Keep the booking PENDING_PAYMENT so the guest can retry until the hold expires (assumption A6)."""
        self.last_payment_failure = reason

    def cancel(self, *, now: datetime, reason: str | None, refund: RefundDecision) -> None:
        self.status = BookingStateMachine.next_state(self.status, BookingTrigger.CANCEL)
        self.cancelled_at = now
        self.cancellation_reason = reason
        self.refund_amount = refund.amount
        self.refund_percent = refund.percent.quantize(_PERCENT_PLACES)

    def expire(self, now: datetime) -> None:
        self.status = BookingStateMachine.next_state(self.status, BookingTrigger.EXPIRE)
        self.expired_at = now

    def record_refund(self, refund_id: UUID) -> None:
        self.refund_id = refund_id
        self.refund_status = BookingRefundStatus.REFUNDED


def _invalid_request(reason: str) -> ValidationFailed:
    return ValidationFailed(INVALID_BOOKING_REQUEST, reason=reason)
