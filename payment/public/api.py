"""``PaymentAPI``, the facade other contexts call (LLD §9.6). It speaks DTOs and ``Money``, never entities.

Booking charges through it (its replay check uses ``find_by_idempotency_key``) and refunds on cancellation;
a refund the gateway rejects raises ``UpstreamFailure(REFUND_FAILED)``.
"""

from functools import lru_cache
from uuid import UUID

from payment import container
from payment.domain.payment import Payment
from payment.public.dto import ChargeCommand, PaymentDTO, RefundCommand, RefundResultDTO
from payment.services.charge_service import ChargeService
from payment.services.query_service import PaymentQueryService
from payment.services.refund_service import RefundService


class PaymentAPI:
    def __init__(self, charges: ChargeService, refunds: RefundService, queries: PaymentQueryService) -> None:
        self._charges = charges
        self._refunds = refunds
        self._queries = queries

    def find_by_idempotency_key(self, key: str) -> PaymentDTO | None:
        payment = self._queries.find_by_idempotency_key(key)
        return None if payment is None else to_payment_dto(payment)

    def charge(self, command: ChargeCommand) -> PaymentDTO:
        """The payment for the command's key: SUCCEEDED or FAILED (a decline is returned, not raised)."""
        return to_payment_dto(self._charges.charge(command))

    def refund(self, command: RefundCommand) -> RefundResultDTO:
        refund = self._refunds.refund(command)
        return RefundResultDTO(refund_id=refund.id, amount=refund.amount)

    def get_payment(self, payment_id: UUID) -> PaymentDTO:
        """The payment, or NotFound(PAYMENT_NOT_FOUND)."""
        return to_payment_dto(self._queries.get(payment_id))


@lru_cache(maxsize=1)
def get_payment_api() -> PaymentAPI:
    return PaymentAPI(container.charge_service(), container.refund_service(), container.payment_query_service())


def to_payment_dto(payment: Payment) -> PaymentDTO:
    return PaymentDTO(
        payment_id=payment.id,
        booking_id=payment.booking_id,
        payer_id=payment.payer_id,
        amount=payment.amount,
        refunded_amount=payment.refunded_amount,
        status=payment.status,
        method=payment.instrument.method,
        instrument_display=payment.instrument.display,
        gateway_txn_id=payment.gateway_txn_id,
        failure_code=payment.failure_code,
        failure_message=payment.failure_message,
    )
