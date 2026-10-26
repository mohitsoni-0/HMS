"""``PaymentPort`` over the payment public API (LLD §8.3): booking's requests -> payment commands, and
``PaymentDTO`` -> ``ChargeResult``."""

from typing import Final

from booking.interfaces.ports import ChargeRequest, ChargeResult, PaymentPort, RefundRequest, RefundResult
from payment.public.api import get_payment_api
from payment.public.dto import ChargeCommand, PaymentDTO, RefundCommand
from payment.public.enums import PaymentStatus

# A payment later refunded was still a successful charge: replaying its key must answer PAID.
_CHARGED: Final = frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED})


class PaymentAdapter(PaymentPort):
    def find_charge(self, idempotency_key: str) -> ChargeResult | None:
        payment = get_payment_api().find_by_idempotency_key(idempotency_key)
        return None if payment is None else _charge_result(payment)

    def charge(self, request: ChargeRequest) -> ChargeResult:
        payment = get_payment_api().charge(
            ChargeCommand(
                booking_id=request.booking_id,
                payer_id=request.payer_id,
                amount=request.amount,
                method=request.method,
                method_details=request.method_details,
                idempotency_key=request.idempotency_key,
            )
        )
        return _charge_result(payment)

    def refund(self, request: RefundRequest) -> RefundResult:
        refund = get_payment_api().refund(
            RefundCommand(
                payment_id=request.payment_id,
                amount=request.amount,
                reason=request.reason,
                idempotency_key=request.idempotency_key,
            )
        )
        return RefundResult(refund_id=refund.refund_id)


def _charge_result(payment: PaymentDTO) -> ChargeResult:
    return ChargeResult(
        payment_id=payment.payment_id,
        succeeded=payment.status in _CHARGED,
        status=payment.status.name,
        instrument_display=payment.instrument_display,
        failure_code=payment.failure_code,
        failure_message=payment.failure_message,
    )
