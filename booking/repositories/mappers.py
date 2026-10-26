"""Booking model <-> domain mapping (LLD §2.4). Model instances never leave ``booking/repositories``.

``apply`` copies only the columns a booking can change after it is made and returns their names for
``save(update_fields=...)``, so the stay, price, terms and guest contact are written once at insert and their
ciphertext is never rewritten.
"""

from datetime import date
from typing import Any

from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money

from booking.domain.booking import Booking
from booking.domain.value_objects import BookingPrice, GuestContact
from booking.models.booking_models import Booking as BookingModel


def to_domain(row: BookingModel) -> Booking:
    currency = row.currency
    return Booking(
        id=row.id,
        guest_id=row.guest_id,
        property_id=row.property_id,
        room_type_id=row.room_type_id,
        owner_account_id=row.owner_account_id,
        stay=DateRange(row.check_in, row.check_out),
        rooms=row.rooms,
        guests=row.guests,
        booking_type=row.booking_type,
        status=row.booking_status,
        price=BookingPrice(
            nightly_rates=tuple(_rate_from_json(rate, currency) for rate in row.nightly_rates),
            rooms=row.rooms,
            total=Money(row.total_amount_paisa, currency),
            pricing_strategy=row.pricing_strategy,
        ),
        cancellation_policy=row.cancellation_policy,
        check_in_time=row.check_in_time,
        contact=GuestContact(name=row.guest_name, email=row.guest_email, phone=row.guest_phone),
        created_at=row.creation_date,
        hold_id=row.hold_id,
        hold_expires_at=row.hold_expires_at,
        payment_id=row.payment_id,
        last_payment_failure=row.last_payment_failure,
        refund_amount=None if row.refund_amount_paisa is None else Money(row.refund_amount_paisa, currency),
        refund_percent=row.refund_percent,
        refund_status=row.refund_status,
        refund_id=row.refund_id,
        cancellation_reason=row.cancellation_reason,
        confirmed_at=row.confirmed_at,
        cancelled_at=row.cancelled_at,
        expired_at=row.expired_at,
    )


def new_row(booking: Booking) -> BookingModel:
    """An unsaved model for a new booking; ``creation_date`` is stamped by the database insert."""
    row = BookingModel(
        id=booking.id,
        guest_id=booking.guest_id,
        property_id=booking.property_id,
        room_type_id=booking.room_type_id,
        owner_account_id=booking.owner_account_id,
        check_in=booking.stay.start,
        check_out=booking.stay.end,
        rooms=booking.rooms,
        guests=booking.guests,
        booking_type=booking.booking_type,
        total_amount_paisa=booking.price.total.paisa,
        currency=booking.price.total.currency,
        nightly_rates=[_rate_to_json(night, amount) for night, amount in booking.price.nightly_rates],
        pricing_strategy=booking.price.pricing_strategy,
        cancellation_policy=booking.cancellation_policy,
        check_in_time=booking.check_in_time,
        guest_name=booking.contact.name,
        guest_email=booking.contact.email,
        guest_phone=booking.contact.phone,
    )
    apply(booking, row)
    return row


def apply(booking: Booking, row: BookingModel) -> list[str]:
    """Copy the mutable state of ``booking`` onto ``row``; returns the field names written."""
    values = _mutable_values(booking)
    for name, value in values.items():
        setattr(row, name, value)
    return list(values)


def _mutable_values(booking: Booking) -> dict[str, Any]:
    return {
        "booking_status": booking.status,
        "hold_id": booking.hold_id,
        "hold_expires_at": booking.hold_expires_at,
        "payment_id": booking.payment_id,
        "last_payment_failure": booking.last_payment_failure,
        "refund_amount_paisa": None if booking.refund_amount is None else booking.refund_amount.paisa,
        "refund_percent": booking.refund_percent,
        "refund_status": booking.refund_status,
        "refund_id": booking.refund_id,
        "cancellation_reason": booking.cancellation_reason,
        "confirmed_at": booking.confirmed_at,
        "cancelled_at": booking.cancelled_at,
        "expired_at": booking.expired_at,
    }


def _rate_to_json(night: date, amount: Money) -> dict[str, Any]:
    return {"date": night.isoformat(), "amount_paisa": amount.paisa}


def _rate_from_json(rate: dict[str, Any], currency: str) -> tuple[date, Money]:
    return date.fromisoformat(rate["date"]), Money(int(rate["amount_paisa"]), currency)
