"""Charge use case (LLD §9.5): validate the method, insert the payment, call the gateway, record the outcome.

Idempotency is layered: an existing payment for the key is returned before any work; otherwise the payment
row is inserted before the gateway call, so a concurrent duplicate fails on the unique key and returns the
winner's payment; and the gateway itself is idempotent per key. A declined charge is a committed FAILED
payment, returned to the caller rather than raised.
"""

import logging

from hotel_commons.domain.unit_of_work import UnitOfWork

from payment.domain.errors import DuplicatePayment
from payment.domain.methods import PaymentMethodRegistry
from payment.domain.payment import Payment
from payment.interfaces.gateway import GatewayChargeRequest, PaymentGateway
from payment.interfaces.repositories import PaymentRepository
from payment.public.dto import ChargeCommand

logger = logging.getLogger(__name__)


class ChargeService:
    def __init__(
        self,
        payments: PaymentRepository,
        methods: PaymentMethodRegistry,
        gateway: PaymentGateway,
        uow: UnitOfWork,
    ) -> None:
        self._payments = payments
        self._methods = methods
        self._gateway = gateway
        self._uow = uow

    def charge(self, cmd: ChargeCommand) -> Payment:
        existing = self._payments.find_by_idempotency_key(cmd.idempotency_key)
        if existing is not None:
            logger.info("Charge replayed payment %s for booking %s", existing.id, cmd.booking_id)
            return existing
        handler = self._methods.get(cmd.method)
        parsed = handler.parse(cmd.method_details)
        try:
            with self._uow:
                payment = Payment.initiate(
                    booking_id=cmd.booking_id,
                    payer_id=cmd.payer_id,
                    amount=cmd.amount,
                    instrument=parsed.instrument,
                    idempotency_key=cmd.idempotency_key,
                )
                self._payments.add(payment)  # insert first: a concurrent duplicate fails here
                response = self._gateway.charge(
                    GatewayChargeRequest(
                        cmd.idempotency_key, cmd.amount, handler.method, parsed.gateway_payload, str(cmd.booking_id)
                    )
                )
                if response.success:
                    payment.mark_succeeded(response.transaction_id)
                else:
                    payment.mark_failed(response.failure_code, response.failure_message)
                self._payments.save(payment)
        except DuplicatePayment:
            # Outside the rolled-back block: the winner's row is committed and visible to a new statement.
            winner = self._payments.find_by_idempotency_key(cmd.idempotency_key)
            logger.info("Charge for booking %s lost an insert race to payment %s", cmd.booking_id, winner.id)
            return winner
        logger.info("Payment %s for booking %s is %s", payment.id, cmd.booking_id, payment.status.name)
        return payment
