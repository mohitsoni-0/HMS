"""Inputs of the booking use cases (LLD §8.4). PII and payment details are excluded from repr."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from booking.domain.value_objects import GuestContact
from booking.enums import BookingType


@dataclass(frozen=True, slots=True)
class CreateBookingCommand:
    guest_id: str
    room_type_id: UUID
    check_in: date
    check_out: date
    rooms: int
    guests: int
    booking_type: BookingType
    contact: GuestContact = field(repr=False)


@dataclass(frozen=True, slots=True)
class PayBookingCommand:
    booking_id: UUID
    payer_id: str
    method: str  # a payment method name: "CARD", "UPI" or "WALLET"
    method_details: Mapping[str, Any] = field(repr=False)
    idempotency_key: str  # the client's Idempotency-Key


@dataclass(frozen=True, slots=True)
class CancelBookingCommand:
    booking_id: UUID
    actor_id: str
    reason: str | None
