"""Repository ports of the payment context (LLD §9.3)."""

from abc import ABC, abstractmethod
from uuid import UUID

from payment.domain.payment import Payment
from payment.domain.refund import Refund


class PaymentRepository(ABC):
    @abstractmethod
    def add(self, payment: Payment) -> None:
        """Insert a new payment; DuplicatePayment when its idempotency key exists (own savepoint)."""

    @abstractmethod
    def save(self, payment: Payment) -> None:
        """Persist the mutable state of an existing payment."""

    @abstractmethod
    def get(self, payment_id: UUID) -> Payment:
        """The payment, or NotFound(PAYMENT_NOT_FOUND)."""

    @abstractmethod
    def get_for_update(self, payment_id: UUID) -> Payment:
        """The payment, row-locked until the end of the transaction; NotFound(PAYMENT_NOT_FOUND)."""

    @abstractmethod
    def find_by_idempotency_key(self, key: str) -> Payment | None: ...


class RefundRepository(ABC):
    @abstractmethod
    def add(self, refund: Refund) -> None: ...

    @abstractmethod
    def find_by_idempotency_key(self, key: str) -> Refund | None: ...
