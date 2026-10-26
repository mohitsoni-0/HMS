"""The Payment aggregate and its state machine (LLD §9.2, HLD §7.2).

INITIATED -> SUCCEEDED | FAILED; SUCCEEDED -> PARTIALLY_REFUNDED | REFUNDED; PARTIALLY_REFUNDED ->
PARTIALLY_REFUNDED | REFUNDED. Anything else is Conflict(INVALID_PAYMENT_TRANSITION). A payment is
created before the gateway call, so its unique idempotency key stops a concurrent duplicate charge.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Self
from uuid import UUID

from hotel_commons.domain.money import Money
from hotel_commons.exceptions import BusinessRuleViolation, Conflict, ValidationFailed
from hotel_commons.status_code import VALIDATION_FAILED
from uuid6 import uuid7

from payment.domain.instrument import PaymentInstrument
from payment.enums import PaymentStatus
from payment.status_code import INVALID_PAYMENT_TRANSITION, PAYMENT_NOT_REFUNDABLE, REFUND_EXCEEDS_PAYMENT

IDEMPOTENCY_KEY_MAX_LENGTH: Final = 200  # payment.idempotency_key is varchar(200)

PAYMENT_TRANSITIONS: Final[Mapping[PaymentStatus, frozenset[PaymentStatus]]] = {
    PaymentStatus.INITIATED: frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.FAILED}),
    PaymentStatus.SUCCEEDED: frozenset({PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}),
    PaymentStatus.PARTIALLY_REFUNDED: frozenset({PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}),
}
_REFUNDABLE: Final = frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.PARTIALLY_REFUNDED})


@dataclass(slots=True)
class Payment:
    id: UUID
    booking_id: UUID
    payer_id: str
    amount: Money
    instrument: PaymentInstrument
    status: PaymentStatus
    idempotency_key: str
    refunded_amount: Money
    gateway_txn_id: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None

    @classmethod
    def initiate(
        cls,
        *,
        booking_id: UUID,
        payer_id: str,
        amount: Money,
        instrument: PaymentInstrument,
        idempotency_key: str,
    ) -> Self:
        """A new INITIATED payment of a positive amount, identified for retries by ``idempotency_key``."""
        if not amount.is_positive():
            raise ValidationFailed(VALIDATION_FAILED, errors={"amount": "must be positive"})
        if not 0 < len(idempotency_key) <= IDEMPOTENCY_KEY_MAX_LENGTH:
            raise ValidationFailed(
                VALIDATION_FAILED, errors={"idempotency_key": f"must be 1-{IDEMPOTENCY_KEY_MAX_LENGTH} characters"}
            )
        return cls(
            id=uuid7(),
            booking_id=booking_id,
            payer_id=payer_id,
            amount=amount,
            instrument=instrument,
            status=PaymentStatus.INITIATED,
            idempotency_key=idempotency_key,
            refunded_amount=Money.zero(amount.currency),
        )

    def mark_succeeded(self, gateway_txn_id: str) -> None:
        self._transition(PaymentStatus.SUCCEEDED)
        self.gateway_txn_id = gateway_txn_id

    def mark_failed(self, code: str, message: str) -> None:
        self._transition(PaymentStatus.FAILED)
        self.failure_code = code
        self.failure_message = message

    def refundable_amount(self) -> Money:
        return self.amount - self.refunded_amount

    def apply_refund(self, amount: Money) -> None:
        """Record a refund of ``amount``: REFUNDED once nothing is left, else PARTIALLY_REFUNDED."""
        if self.status not in _REFUNDABLE:
            raise BusinessRuleViolation(PAYMENT_NOT_REFUNDABLE, state=self.status.name)
        if not amount.is_positive():
            raise ValidationFailed(VALIDATION_FAILED, errors={"amount": "must be positive"})
        if amount > self.refundable_amount():
            raise BusinessRuleViolation(REFUND_EXCEEDS_PAYMENT)
        refunded = self.refunded_amount + amount
        self._transition(PaymentStatus.REFUNDED if refunded == self.amount else PaymentStatus.PARTIALLY_REFUNDED)
        self.refunded_amount = refunded

    def _transition(self, target: PaymentStatus) -> None:
        if target not in PAYMENT_TRANSITIONS.get(self.status, frozenset()):
            raise Conflict(INVALID_PAYMENT_TRANSITION, current=self.status.name, target=target.name)
        self.status = target
