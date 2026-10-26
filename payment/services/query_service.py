"""Read-side payment use cases (LLD §9.5)."""

from uuid import UUID

from payment.domain.payment import Payment
from payment.interfaces.repositories import PaymentRepository


class PaymentQueryService:
    def __init__(self, payments: PaymentRepository) -> None:
        self._payments = payments

    def get(self, payment_id: UUID) -> Payment:
        """The payment, or NotFound(PAYMENT_NOT_FOUND)."""
        return self._payments.get(payment_id)
