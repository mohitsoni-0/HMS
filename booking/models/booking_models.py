"""``booking.booking`` and its audit log ``booking.booking_log`` (LLD §3.5, §8.7).

Money is BIGINT paisa, enums SMALLINT, guest contact details AES-GCM ciphertext with masked companions for
owner views. Ids of other contexts (property, room type, owner account, hold, payment) are plain UUIDs: no
cross-schema foreign keys, so the schema can move to its own database unchanged. CHECK constraints repeat
the domain's invariants, so they hold even if application code is wrong. The client's Idempotency-Key is
kept per guest under a partial unique constraint, so a retried create returns the first booking even when
Redis has forgotten the key.
"""

from typing import Final

from django.contrib.postgres.indexes import BrinIndex
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import F, Q
from hotel_commons.db.schema import schema_table
from hotel_commons.models.base import AddCommonField
from hotel_commons.models.fields import EncryptedTextField, EnumField
from hotel_commons.models.managers import CustomUpdateManager
from hotel_commons.security.pi_type import PiType
from uuid6 import uuid7

from booking.constants import IDEMPOTENCY_KEY_MAX_LENGTH, TEXT_MAX_LENGTH, USER_ID_MAX_LENGTH
from booking.enums import BookingRefundStatus, BookingStatus, BookingType
from catalog.public.enums import CancellationPolicyCode, PricingStrategyCode

_MASK_MAX_LENGTH: Final = 255
BOOKING_IDEMPOTENCY_KEY_CONSTRAINT: Final = "booking_guest_idempotency_key_uniq"


class CommonBooking(AddCommonField):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    guest_id = models.CharField(max_length=USER_ID_MAX_LENGTH)
    property_id = models.UUIDField()
    room_type_id = models.UUIDField()
    owner_account_id = models.UUIDField()
    check_in = models.DateField()
    check_out = models.DateField()
    rooms = models.PositiveSmallIntegerField()
    guests = models.PositiveSmallIntegerField()
    booking_type = EnumField(BookingType)
    booking_status = EnumField(BookingStatus)
    total_amount_paisa = models.BigIntegerField(db_comment="Total for all rooms and nights, in paisa (INR x 100)")
    currency = models.CharField(max_length=3, default="INR")
    nightly_rates = models.JSONField(
        encoder=DjangoJSONEncoder, db_comment='Per-room nightly rates: [{"date": "YYYY-MM-DD", "amount_paisa": n}]'
    )
    pricing_strategy = EnumField(PricingStrategyCode)
    cancellation_policy = EnumField(CancellationPolicyCode)
    check_in_time = models.TimeField()
    guest_name = EncryptedTextField(pi_type=PiType.NAME)
    masked_guest_name = models.CharField(max_length=_MASK_MAX_LENGTH, null=True, blank=True)
    guest_email = EncryptedTextField(pi_type=PiType.EMAIL)
    masked_guest_email = models.CharField(max_length=_MASK_MAX_LENGTH, null=True, blank=True)
    guest_phone = EncryptedTextField(pi_type=PiType.PHONE)
    masked_guest_phone = models.CharField(max_length=_MASK_MAX_LENGTH, null=True, blank=True)
    hold_id = models.UUIDField(null=True, blank=True)
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    payment_id = models.UUIDField(null=True, blank=True)
    last_payment_failure = models.CharField(max_length=TEXT_MAX_LENGTH, null=True, blank=True)
    refund_amount_paisa = models.BigIntegerField(null=True, blank=True, db_comment="Refunded amount in paisa")
    refund_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    refund_status = EnumField(BookingRefundStatus, default=BookingRefundStatus.NONE)
    refund_id = models.UUIDField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=TEXT_MAX_LENGTH, null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(
        max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
        null=True,
        blank=True,
        db_comment="The client's Idempotency-Key of the create request, unique per guest",
    )

    class Meta:
        abstract = True


class Booking(CommonBooking):
    CustomUpdateManager.set_logModel(logModel="booking.BookingLog", model="booking.Booking")

    class Meta:
        db_table = schema_table("booking", "booking")
        indexes = [
            models.Index(fields=["guest_id", "creation_date"], name="booking_guest_created_idx"),
            # list_for_property's order: a page is an index range scan, no sort of the property's bookings.
            models.Index(fields=["property_id", "-creation_date", "-id"], name="booking_property_created_idx"),
            # The expiry job's scan: only pending bookings have a live hold to expire.
            models.Index(
                fields=["hold_expires_at"],
                name="booking_status_hold_idx",
                condition=Q(booking_status=BookingStatus.PENDING_PAYMENT),
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["guest_id", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name=BOOKING_IDEMPOTENCY_KEY_CONSTRAINT,
            ),
            models.CheckConstraint(condition=Q(check_out__gt=F("check_in")), name="booking_dates_valid"),
            models.CheckConstraint(condition=Q(rooms__gte=1), name="booking_rooms_positive"),
            models.CheckConstraint(condition=Q(guests__gte=1), name="booking_guests_positive"),
            models.CheckConstraint(condition=Q(total_amount_paisa__gt=0), name="booking_total_positive"),
            models.CheckConstraint(
                condition=Q(refund_amount_paisa__isnull=True)
                | Q(refund_amount_paisa__gte=0, refund_amount_paisa__lte=F("total_amount_paisa")),
                name="booking_refund_within_total",
            ),
            models.CheckConstraint(
                condition=Q(refund_percent__isnull=True) | Q(refund_percent__gte=0, refund_percent__lte=100),
                name="booking_refund_percent_range",
            ),
        ]


class BookingLog(CommonBooking):
    log = models.ForeignKey(Booking, on_delete=models.RESTRICT, related_name="logs")
    updation_date = None
    updation_by = None

    class Meta:
        db_table = schema_table("booking", "booking_log")
        indexes = [
            models.Index(fields=["log", "creation_date"], name="booking_log_log_created_idx"),
            BrinIndex(fields=["creation_date"], name="booking_log_created_brin"),
        ]
