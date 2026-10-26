"""Value objects of the booking aggregate (LLD §8.2).

``RoomTypeInfo`` and ``BookingPrice`` are booking's own views of catalog and pricing data (anti-corruption):
the adapters build them from the other contexts' DTOs, so the domain never sees those types.
"""

from dataclasses import dataclass, field
from datetime import date, time
from uuid import UUID

from catalog.public.enums import CancellationPolicyCode, PricingStrategyCode
from hotel_commons.domain.money import Money
from hotel_commons.domain.validation import is_indian_mobile, is_valid_email
from hotel_commons.exceptions import ValidationFailed

from booking.constants import GUEST_NAME_MAX_LENGTH, GUEST_NAME_MIN_LENGTH
from booking.status_code import INVALID_BOOKING_REQUEST


@dataclass(frozen=True, slots=True)
class GuestContact:
    """The guest's contact details. PII: excluded from repr, encrypted at rest, masked for owners.

    Errors name the field, never its value. The e-mail and mobile rules are the shared ones of
    ``hotel_commons.domain.validation``, so a guest is checked exactly like an owner or a wallet holder.
    """

    name: str = field(repr=False)
    email: str = field(repr=False)
    phone: str = field(repr=False)

    def __post_init__(self) -> None:
        if not GUEST_NAME_MIN_LENGTH <= len(self.name.strip()) <= GUEST_NAME_MAX_LENGTH:
            raise _invalid_contact(f"guest_name must be {GUEST_NAME_MIN_LENGTH}-{GUEST_NAME_MAX_LENGTH} characters")
        if not is_valid_email(self.email):
            raise _invalid_contact("guest_email must be a valid e-mail address")
        if not is_indian_mobile(self.phone):
            raise _invalid_contact("guest_phone must be a 10-digit mobile number starting with 6-9")


@dataclass(frozen=True, slots=True)
class BookingPrice:
    """The price snapshotted at booking time: per-night rates of one room, and the total for all rooms."""

    nightly_rates: tuple[tuple[date, Money], ...]
    rooms: int
    total: Money
    pricing_strategy: PricingStrategyCode


@dataclass(frozen=True, slots=True)
class RoomTypeInfo:
    room_type_id: UUID
    property_id: UUID
    owner_account_id: UUID
    max_occupancy: int
    total_rooms: int
    base_price: Money
    pricing_strategy: PricingStrategyCode
    cancellation_policy: CancellationPolicyCode
    check_in_time: time
    is_active: bool


def _invalid_contact(reason: str) -> ValidationFailed:
    return ValidationFailed(INVALID_BOOKING_REQUEST, reason=reason)
