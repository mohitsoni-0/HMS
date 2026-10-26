"""A refund against a payment (LLD §9.2). Only refunds the gateway accepted are recorded, so the one status
is SUCCEEDED; a rejected refund raises and leaves no row."""

from dataclasses import dataclass
from uuid import UUID

from hotel_commons.domain.money import Money

from payment.enums import RefundStatus


@dataclass(slots=True)
class Refund:
    id: UUID
    payment_id: UUID
    amount: Money
    status: RefundStatus
    reason: str
    idempotency_key: str
    gateway_refund_id: str | None = None
