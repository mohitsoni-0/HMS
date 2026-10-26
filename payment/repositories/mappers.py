"""Model <-> domain mapping for payments and refunds (LLD §2.4).

``apply_payment`` copies only the columns a payment changes after it is created; ``PAYMENT_MUTABLE_FIELDS``
names them for ``save(update_fields=...)``.
"""

from typing import Final

from hotel_commons.domain.money import Money

from payment.domain.instrument import PaymentInstrument
from payment.domain.payment import Payment
from payment.domain.refund import Refund
from payment.models.payment_models import Payment as PaymentRow
from payment.models.payment_models import Refund as RefundRow

PAYMENT_MUTABLE_FIELDS: Final = (
    "payment_status",
    "refunded_amount_paisa",
    "gateway_txn_id",
    "failure_code",
    "failure_message",
)


def new_payment_row(payment: Payment) -> PaymentRow:
    row = PaymentRow(
        id=payment.id,
        booking_id=payment.booking_id,
        payer_id=payment.payer_id,
        amount_paisa=payment.amount.paisa,
        currency=payment.amount.currency,
        method=payment.instrument.method,
        instrument_display=payment.instrument.display,
        instrument_ref=payment.instrument.sensitive_ref,
        idempotency_key=payment.idempotency_key,
    )
    apply_payment(payment, row)
    return row


def apply_payment(payment: Payment, row: PaymentRow) -> None:
    row.payment_status = payment.status
    row.refunded_amount_paisa = payment.refunded_amount.paisa
    row.gateway_txn_id = payment.gateway_txn_id
    row.failure_code = payment.failure_code
    row.failure_message = payment.failure_message


def payment_from_row(row: PaymentRow) -> Payment:
    return Payment(
        id=row.id,
        booking_id=row.booking_id,
        payer_id=row.payer_id,
        amount=Money(row.amount_paisa, row.currency),
        instrument=PaymentInstrument(row.method, row.instrument_display, sensitive_ref=row.instrument_ref),
        status=row.payment_status,
        idempotency_key=row.idempotency_key,
        refunded_amount=Money(row.refunded_amount_paisa, row.currency),
        gateway_txn_id=row.gateway_txn_id,
        failure_code=row.failure_code,
        failure_message=row.failure_message,
    )


def new_refund_row(refund: Refund) -> RefundRow:
    return RefundRow(
        id=refund.id,
        payment_id=refund.payment_id,
        amount_paisa=refund.amount.paisa,
        currency=refund.amount.currency,
        refund_status=refund.status,
        reason=refund.reason,
        gateway_refund_id=refund.gateway_refund_id,
        idempotency_key=refund.idempotency_key,
    )


def refund_from_row(row: RefundRow) -> Refund:
    return Refund(
        id=row.id,
        payment_id=row.payment_id,
        amount=Money(row.amount_paisa, row.currency),
        status=row.refund_status,
        reason=row.reason,
        idempotency_key=row.idempotency_key,
        gateway_refund_id=row.gateway_refund_id,
    )
