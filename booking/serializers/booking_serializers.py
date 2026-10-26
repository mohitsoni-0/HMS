"""Booking API serializers (LLD §3.14, §10.3).

Amounts are rupee strings on the wire and integer paisa inside (``HotelAmountField``), with a sibling
``currency``; enums are member names (``HotelEnumField``). The guest's contact details are readable by the
guest and by admins; everyone else (the property's owner) sees them masked. Response serializers render the
domain ``Booking`` and the pay outcome directly, and mark every field that can be null as nullable so the
published schema matches what is sent; request serializers only validate shape, the domain enforces the
business rules. The pay body is the payment context's published ``PaymentMethodRequestSerializer``: booking
holds no per-method knowledge, so a new payment method needs no change here.
"""

from datetime import date
from typing import Any, Final

from django.conf import settings
from django.core.validators import RegexValidator
from hotel_commons.api.pagination import MAX_PAGE_SIZE
from hotel_commons.domain.money import Money
from hotel_commons.security.pi_type import PiType
from hotel_commons.security.principal import Role
from hotel_commons.serializers.base import BaseHotelSerializer
from hotel_commons.serializers.fields import (
    HotelAmountField,
    HotelCharField,
    HotelDateField,
    HotelDateTimeField,
    HotelEmailField,
    HotelEncryptedCharField,
    HotelEnumField,
    HotelIntegerField,
    HotelPhoneField,
    HotelUUIDField,
)
from rest_framework import serializers

from booking.constants import (
    GUEST_NAME_MAX_LENGTH,
    GUEST_NAME_MIN_LENGTH,
    MAX_GUESTS_PER_BOOKING,
    MAX_ROOMS_PER_BOOKING,
    TEXT_MAX_LENGTH,
    USER_ID_MAX_LENGTH,
)
from booking.enums import BookingRefundStatus, BookingStatus, BookingType
from catalog.public.enums import CancellationPolicyCode, PricingStrategyCode
from payment.public.serializers import PaymentMethodRequestSerializer

NightlyRate = tuple[date, Money]

_USER_ID: Final = RegexValidator(r"^[A-Za-z0-9_.@-]{1,64}$", "Enter a valid user id.")
_DEFAULT_PAGE_SIZE: Final = 20


# --- Requests ----------------------------------------------------------------------------------------------


class BookingCreateSerializer(BaseHotelSerializer):
    room_type_id = HotelUUIDField()
    check_in = HotelDateField()
    check_out = HotelDateField(help_text="Exclusive: the check-out day is not a night of the stay.")
    rooms = HotelIntegerField(min_value=1, max_value=MAX_ROOMS_PER_BOOKING, default=1)
    guests = HotelIntegerField(min_value=1, max_value=MAX_GUESTS_PER_BOOKING)
    booking_type = HotelEnumField(BookingType, default=BookingType.PREPAID)
    guest_name = HotelEncryptedCharField(
        min_length=GUEST_NAME_MIN_LENGTH, max_length=GUEST_NAME_MAX_LENGTH, mask_as=PiType.NAME
    )
    guest_email = HotelEmailField()
    guest_phone = HotelPhoneField()
    on_behalf_of_user_id = HotelCharField(
        required=False,
        max_length=USER_ID_MAX_LENGTH,
        validators=[_USER_ID],
        restrict_roles=[Role.ADMIN],
        help_text="Book for this guest instead of the caller.",
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        nights = (attrs["check_out"] - attrs["check_in"]).days
        if nights < 1:
            raise serializers.ValidationError({"check_out": "Must be after check_in."})
        if nights > settings.MAX_STAY_NIGHTS:
            limit = settings.MAX_STAY_NIGHTS
            raise serializers.ValidationError({"check_out": f"A stay can be at most {limit} nights."})
        return attrs


class BookingListQuerySerializer(BaseHotelSerializer):
    property_id = HotelUUIDField(required=False, help_text="Required for owners: one of their properties.")
    guest_id = HotelCharField(
        required=False, max_length=USER_ID_MAX_LENGTH, validators=[_USER_ID], restrict_roles=[Role.ADMIN]
    )
    page = HotelIntegerField(min_value=1, default=1)
    page_size = HotelIntegerField(min_value=1, max_value=MAX_PAGE_SIZE, default=_DEFAULT_PAGE_SIZE)


class BookingPaymentSerializer(PaymentMethodRequestSerializer):
    """The pay body: the method and exactly one matching details object (payment's published shape);
    ``method_payload()`` hands them on, and the payment context validates the details."""


class BookingCancelSerializer(BaseHotelSerializer):
    reason = HotelCharField(required=False, allow_null=True, allow_blank=True, max_length=TEXT_MAX_LENGTH)


# --- Responses ---------------------------------------------------------------------------------------------


class BookingNightlyRateSerializer(BaseHotelSerializer):
    date = HotelDateField(read_only=True)
    price = HotelAmountField(read_only=True)

    def to_representation(self, instance: NightlyRate) -> dict[str, Any]:
        night, price = instance
        return super().to_representation({"date": night, "price": price})


class BookingRefundSerializer(BaseHotelSerializer):
    amount = HotelAmountField(source="refund_amount", read_only=True, allow_null=True)
    percent = HotelCharField(
        source="refund_percent", read_only=True, allow_null=True, help_text='Share of the total, e.g. "50.00".'
    )
    status = HotelEnumField(BookingRefundStatus, source="refund_status", read_only=True)


class BookingSerializer(BaseHotelSerializer):
    booking_id = HotelUUIDField(source="id", read_only=True)
    status = HotelEnumField(BookingStatus, read_only=True)
    booking_type = HotelEnumField(BookingType, read_only=True)
    property_id = HotelUUIDField(read_only=True)
    room_type_id = HotelUUIDField(read_only=True)
    check_in = HotelDateField(source="stay.start", read_only=True)
    check_out = HotelDateField(source="stay.end", read_only=True)
    nights = HotelIntegerField(source="stay.nights", read_only=True)
    rooms = HotelIntegerField(read_only=True)
    guests = HotelIntegerField(read_only=True)
    total = HotelAmountField(source="price.total", read_only=True)
    currency = HotelCharField(source="price.total.currency", read_only=True)
    nightly_rates = BookingNightlyRateSerializer(source="price.nightly_rates", many=True, read_only=True)
    pricing_strategy = HotelEnumField(PricingStrategyCode, source="price.pricing_strategy", read_only=True)
    cancellation_policy = HotelEnumField(CancellationPolicyCode, read_only=True)
    hold_expires_at = HotelDateTimeField(read_only=True, allow_null=True, help_text="Null unless prepaid.")
    payment_id = HotelUUIDField(read_only=True, allow_null=True)
    refund = BookingRefundSerializer(source="*", read_only=True)
    guest_name = HotelEncryptedCharField(
        source="contact.name", read_only=True, read_roles=[Role.ADMIN], subject_attr="guest_id", mask_as=PiType.NAME
    )
    guest_email = HotelEncryptedCharField(
        source="contact.email", read_only=True, read_roles=[Role.ADMIN], subject_attr="guest_id", mask_as=PiType.EMAIL
    )
    guest_phone = HotelEncryptedCharField(
        source="contact.phone", read_only=True, read_roles=[Role.ADMIN], subject_attr="guest_id", mask_as=PiType.PHONE
    )
    last_payment_failure = HotelCharField(read_only=True, allow_null=True)
    created_at = HotelDateTimeField(read_only=True)
    confirmed_at = HotelDateTimeField(read_only=True, allow_null=True)
    cancelled_at = HotelDateTimeField(read_only=True, allow_null=True)
    expired_at = HotelDateTimeField(read_only=True, allow_null=True)


class PaymentSummarySerializer(BaseHotelSerializer):
    payment_id = HotelUUIDField(read_only=True)
    status = HotelCharField(read_only=True, help_text="Payment status, e.g. SUCCEEDED or FAILED.")
    instrument = HotelCharField(source="instrument_display", read_only=True, help_text='e.g. "VISA •••• 4242".')
    failure_code = HotelCharField(read_only=True, allow_null=True)
    failure_message = HotelCharField(read_only=True, allow_null=True)


class BookingPaymentResultSerializer(BaseHotelSerializer):
    """``data`` of a pay response, whatever the outcome (200 paid, 402 declined, 409 expired)."""

    booking = BookingSerializer(read_only=True)
    payment = PaymentSummarySerializer(read_only=True, allow_null=True)
