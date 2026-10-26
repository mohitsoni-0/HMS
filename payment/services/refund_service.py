"""Refund use case (LLD §9.5): lock the payment, validate the amount, refund at the gateway, record both.

The domain validates before the gateway is called. A gateway rejection raises UpstreamFailure(REFUND_FAILED)
before anything is written, so the caller's whole transaction (e.g. a booking cancellation) rolls back and
can be retried; the gateway's idempotency per key makes that retry safe.
"""

import logging

from hotel_commons.domain.unit_of_work import UnitOfWork
from hotel_commons.exceptions import UpstreamFailure
from uuid6 import uuid7

from payment.domain.refund import Refund
from payment.enums import RefundStatus
from payment.interfaces.gateway import GatewayRefundRequest, PaymentGateway
from payment.interfaces.repositories import PaymentRepository, RefundRepository
from payment.public.dto import RefundCommand
from payment.status_code import REFUND_FAILED

logger = logging.getLogger(__name__)


class RefundService:
    def __init__(
        self,
        payments: PaymentRepository,
        refunds: RefundRepository,
        gateway: PaymentGateway,
        uow: UnitOfWork,
    ) -> None:
        self._payments = payments
        self._refunds = refunds
        self._gateway = gateway
        self._uow = uow

    def refund(self, cmd: RefundCommand) -> Refund:
        existing = self._refunds.find_by_idempotency_key(cmd.idempotency_key)
        if existing is not None:
            logger.info("Refund replayed refund %s for payment %s", existing.id, cmd.payment_id)
            return existing
        with self._uow:
            payment = self._payments.get_for_update(cmd.payment_id)
            # Re-checked under the payment lock: a concurrent retry with this key may have just committed.
            existing = self._refunds.find_by_idempotency_key(cmd.idempotency_key)
            if existing is not None:
                return existing
            payment.apply_refund(cmd.amount)  # validates before the gateway is called
            response = self._gateway.refund(
                GatewayRefundRequest(cmd.idempotency_key, payment.gateway_txn_id, cmd.amount)
            )
            if not response.success:
                logger.warning("Gateway rejected the refund of payment %s: %s", payment.id, response.failure_code)
                raise UpstreamFailure(REFUND_FAILED)
            refund = Refund(
                id=uuid7(),
                payment_id=payment.id,
                amount=cmd.amount,
                status=RefundStatus.SUCCEEDED,
                reason=cmd.reason,
                idempotency_key=cmd.idempotency_key,
                gateway_refund_id=response.refund_id,
            )
            self._refunds.add(refund)
            self._payments.save(payment)
        logger.info("Refund %s of payment %s recorded; payment is %s", refund.id, payment.id, payment.status.name)
        return refund
