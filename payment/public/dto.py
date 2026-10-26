"""Commands and DTOs, the only payment types other contexts see (LLD §9.6).

Immutable and Django-free. Amounts are ``Money`` (integer paisa). ``method_details`` carries raw card, UPI or
wallet data, so it is excluded from ``repr`` and never logged.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from hotel_commons.domain.money import Money

from payment.public.enums import PaymentMethod, PaymentStatus


@dataclass(frozen=True, slots=True)
class ChargeCommand:
    """Charge ``amount`` for a booking. ``method`` is a ``PaymentMethod`` name ("CARD", "UPI", "WALLET") and
    ``method_details`` the fields of that method, e.g. ``{"vpa": "rahul@okaxis"}`` for UPI."""

    booking_id: UUID
    payer_id: str
    amount: Money
    method: str
    method_details: Mapping[str, Any] = field(repr=False)
    idempotency_key: str = ""


@dataclass(frozen=True, slots=True)
class RefundCommand:
    payment_id: UUID
    amount: Money
    reason: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PaymentDTO:
    payment_id: UUID
    booking_id: UUID
    payer_id: str
    amount: Money
    refunded_amount: Money
    status: PaymentStatus
    method: PaymentMethod
    instrument_display: str
    gateway_txn_id: str | None
    failure_code: str | None
    failure_message: str | None


@dataclass(frozen=True, slots=True)
class RefundResultDTO:
    refund_id: UUID
    amount: Money
