"""In-memory test doubles for the payment services: repositories, a counting gateway and a method registry.

The repositories store copies, so a service that forgets ``save`` is caught by the tests. Nothing here
imports Django.
"""

import dataclasses
from uuid import UUID

from hotel_commons.domain.clock import Clock
from hotel_commons.exceptions import NotFound

from payment.adapters.mock_gateway import MockPaymentGateway
from payment.domain.errors import DuplicatePayment
from payment.domain.methods import CardHandler, PaymentMethodRegistry, UpiHandler, WalletHandler
from payment.domain.payment import Payment
from payment.domain.refund import Refund
from payment.interfaces.gateway import (
    GatewayChargeRequest,
    GatewayChargeResponse,
    GatewayRefundRequest,
    GatewayRefundResponse,
    PaymentGateway,
)
from payment.interfaces.repositories import PaymentRepository, RefundRepository
from payment.status_code import PAYMENT_NOT_FOUND


class InMemoryPaymentRepository(PaymentRepository):
    """Dict of payments by id; ``add`` enforces the unique idempotency key like the database does."""

    def __init__(self) -> None:
        self.rows: dict[UUID, Payment] = {}

    def add(self, payment: Payment) -> None:
        if any(row.idempotency_key == payment.idempotency_key for row in self.rows.values()):
            raise DuplicatePayment(payment.idempotency_key)
        self.rows[payment.id] = dataclasses.replace(payment)

    def save(self, payment: Payment) -> None:
        if payment.id not in self.rows:
            raise KeyError(f"payment {payment.id} was never added")
        self.rows[payment.id] = dataclasses.replace(payment)

    def get(self, payment_id: UUID) -> Payment:
        row = self.rows.get(payment_id)
        if row is None:
            raise NotFound(PAYMENT_NOT_FOUND)
        return dataclasses.replace(row)

    def get_for_update(self, payment_id: UUID) -> Payment:
        return self.get(payment_id)

    def find_by_idempotency_key(self, key: str) -> Payment | None:
        for row in self.rows.values():
            if row.idempotency_key == key:
                return dataclasses.replace(row)
        return None


class InMemoryRefundRepository(RefundRepository):
    def __init__(self) -> None:
        self.rows: dict[UUID, Refund] = {}

    def add(self, refund: Refund) -> None:
        self.rows[refund.id] = dataclasses.replace(refund)

    def find_by_idempotency_key(self, key: str) -> Refund | None:
        for row in self.rows.values():
            if row.idempotency_key == key:
                return dataclasses.replace(row)
        return None


class CountingGateway(PaymentGateway):
    """The mock gateway, counting the calls that reach it."""

    def __init__(self) -> None:
        self._gateway = MockPaymentGateway()
        self.charge_calls = 0
        self.refund_calls = 0

    def charge(self, request: GatewayChargeRequest) -> GatewayChargeResponse:
        self.charge_calls += 1
        return self._gateway.charge(request)

    def refund(self, request: GatewayRefundRequest) -> GatewayRefundResponse:
        self.refund_calls += 1
        return self._gateway.refund(request)


def method_registry(clock: Clock) -> PaymentMethodRegistry:
    """The three production handlers, with card expiry checked against ``clock``."""
    registry = PaymentMethodRegistry()
    for handler in (CardHandler(clock), UpiHandler(), WalletHandler()):
        registry.register(handler)
    return registry
