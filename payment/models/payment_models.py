"""Payment tables in schema ``payment`` (LLD §9.7): payment/payment_log and refund/refund_log.

Amounts are BIGINT paisa, enums SMALLINT. The UPI VPA or wallet phone is ``instrument_ref``, encrypted at
rest; card data is never stored, only ``request_fingerprint``, an HMAC of the charge request that tells a
replay of an idempotency key from its reuse for another request (NULL on rows from before it). The unique
idempotency keys live on the live tables only, so log snapshots never collide.
"""

from django.contrib.postgres.indexes import BrinIndex
from django.db import models
from django.db.models import F, Q
from hotel_commons.db.schema import schema_table
from hotel_commons.models.base import AddCommonField
from hotel_commons.models.fields import EncryptedTextField, EnumField
from hotel_commons.models.managers import CustomUpdateManager
from hotel_commons.security.pi_type import PiType
from uuid6 import uuid7

from payment.enums import PaymentMethod, PaymentStatus, RefundStatus

PAYMENT_IDEMPOTENCY_KEY_CONSTRAINT = "payment_payment_idempotency_key_uniq"
REFUND_IDEMPOTENCY_KEY_CONSTRAINT = "payment_refund_idempotency_key_uniq"


class CommonPayment(AddCommonField):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    booking_id = models.UUIDField()
    payer_id = models.CharField(max_length=64)
    amount_paisa = models.BigIntegerField()
    refunded_amount_paisa = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=3)
    method = EnumField(PaymentMethod)
    payment_status = EnumField(PaymentStatus)
    instrument_display = models.CharField(max_length=64)
    instrument_ref = EncryptedTextField(pi_type=PiType.OTHER, null=True, blank=True)
    gateway_txn_id = models.CharField(max_length=64, null=True, blank=True)
    failure_code = models.CharField(max_length=64, null=True, blank=True)
    failure_message = models.CharField(max_length=255, null=True, blank=True)
    idempotency_key = models.CharField(max_length=200)
    request_fingerprint = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        abstract = True


class Payment(CommonPayment):
    CustomUpdateManager.set_logModel(logModel="payment.PaymentLog", model="payment.Payment")

    class Meta:
        db_table = schema_table("payment", "payment")
        indexes = [models.Index(fields=["booking_id"], name="payment_payment_booking_idx")]
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key"], name=PAYMENT_IDEMPOTENCY_KEY_CONSTRAINT),
            models.CheckConstraint(condition=Q(amount_paisa__gt=0), name="payment_payment_amount_positive"),
            models.CheckConstraint(
                condition=Q(refunded_amount_paisa__gte=0, refunded_amount_paisa__lte=F("amount_paisa")),
                name="payment_payment_refunded_within_amount",
            ),
        ]


class PaymentLog(CommonPayment):
    log = models.ForeignKey(Payment, on_delete=models.RESTRICT, related_name="logs")
    updation_date = None
    updation_by = None

    class Meta:
        db_table = schema_table("payment", "payment_log")
        indexes = [
            models.Index(fields=["log", "creation_date"], name="payment_payment_log_log_idx"),
            BrinIndex(fields=["creation_date"], name="payment_payment_log_brin"),
        ]


class CommonRefund(AddCommonField):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    payment = models.ForeignKey("payment.Payment", on_delete=models.RESTRICT, related_name="+")
    amount_paisa = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    refund_status = EnumField(RefundStatus)
    reason = models.CharField(max_length=255)
    gateway_refund_id = models.CharField(max_length=64, null=True, blank=True)
    idempotency_key = models.CharField(max_length=200)

    class Meta:
        abstract = True


class Refund(CommonRefund):
    CustomUpdateManager.set_logModel(logModel="payment.RefundLog", model="payment.Refund")

    class Meta:
        db_table = schema_table("payment", "refund")
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key"], name=REFUND_IDEMPOTENCY_KEY_CONSTRAINT),
            models.CheckConstraint(condition=Q(amount_paisa__gt=0), name="payment_refund_amount_positive"),
        ]


class RefundLog(CommonRefund):
    log = models.ForeignKey(Refund, on_delete=models.RESTRICT, related_name="logs")
    updation_date = None
    updation_by = None

    class Meta:
        db_table = schema_table("payment", "refund_log")
        indexes = [
            models.Index(fields=["log", "creation_date"], name="payment_refund_log_log_idx"),
            BrinIndex(fields=["creation_date"], name="payment_refund_log_brin"),
        ]
