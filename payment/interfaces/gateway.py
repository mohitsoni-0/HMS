"""Payment gateway port (LLD §9.3). Implementations must be idempotent per ``idempotency_key``: a retried
charge or refund with the same key returns the first response and never moves money twice."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field

from hotel_commons.domain.money import Money

from payment.enums import PaymentMethod


@dataclass(frozen=True, slots=True)
class GatewayChargeRequest:
    idempotency_key: str
    amount: Money
    method: PaymentMethod
    payload: Mapping[str, str] = field(repr=False)  # raw card / UPI / wallet details
    reference: str = ""


@dataclass(frozen=True, slots=True)
class GatewayChargeResponse:
    success: bool
    transaction_id: str | None
    failure_code: str | None
    failure_message: str | None


@dataclass(frozen=True, slots=True)
class GatewayRefundRequest:
    idempotency_key: str
    transaction_id: str
    amount: Money


@dataclass(frozen=True, slots=True)
class GatewayRefundResponse:
    success: bool
    refund_id: str | None
    failure_code: str | None


class PaymentGateway(ABC):
    @abstractmethod
    def charge(self, request: GatewayChargeRequest) -> GatewayChargeResponse: ...

    @abstractmethod
    def refund(self, request: GatewayRefundRequest) -> GatewayRefundResponse: ...
