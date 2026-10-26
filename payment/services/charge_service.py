"""Charge use case (LLD §9.5): validate the method, insert the payment, call the gateway, record the outcome.

Idempotency is layered: an existing payment for the key is returned before any work; otherwise the payment
row is inserted before the gateway call, so a concurrent duplicate fails on the unique key and returns the
winner's payment; and the gateway itself is idempotent per key. A payment is returned for its key only when
the request matches the one it was made for (same method and details, compared by fingerprint); a key reused
for another request is BusinessRuleViolation(IDEMPOTENCY_KEY_REUSED). A declined charge is a committed
FAILED payment, returned to the caller rather than raised.
"""

import logging
from collections.abc import Mapping
from typing import Any

from hotel_commons.domain.unit_of_work import UnitOfWork

from payment.domain.errors import DuplicatePayment
from payment.domain.fingerprint import RequestFingerprinter
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
        fingerprinter: RequestFingerprinter,
    ) -> None:
        self._payments = payments
        self._methods = methods
        self._gateway = gateway
        self._uow = uow
        self._fingerprinter = fingerprinter

    def find_by_idempotency_key(
        self, key: str, *, method: str | None = None, method_details: Mapping[str, Any] | None = None
    ) -> Payment | None:
        """The payment made with ``key``, if any.

        When ``method`` is given, the payment must have been made for that method and ``method_details``,
        otherwise BusinessRuleViolation(IDEMPOTENCY_KEY_REUSED).
        """
        payment = self._payments.find_by_idempotency_key(key)
        if payment is not None and method is not None:
            payment.ensure_same_request(self._fingerprinter.fingerprint(method, method_details or {}))
        return payment

    def charge(self, cmd: ChargeCommand) -> Payment:
        fingerprint = self._fingerprinter.fingerprint(cmd.method, cmd.method_details)
        existing = self._payments.find_by_idempotency_key(cmd.idempotency_key)
        if existing is not None:
            existing.ensure_same_request(fingerprint)
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
                    request_fingerprint=fingerprint,
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
            winner.ensure_same_request(fingerprint)
            return winner
        logger.info("Payment %s for booking %s is %s", payment.id, cmd.booking_id, payment.status.name)
        return payment
