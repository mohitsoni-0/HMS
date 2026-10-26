"""Deterministic, idempotent stand-in for a payment gateway (LLD §9.4).

- card number ending 0002: declined, ``card_declined``
- card number ending 0069: declined, ``expired_card``
- UPI VPA starting with "fail": failed, ``upi_collect_rejected``
- wallet charge above ₹50,000: failed, ``insufficient_wallet_balance``
- any other charge: success, transaction id "MOCKTXN-<12 hex>"
- refund of a transaction id containing "REFUNDFAIL" (test hook only): failed, ``refund_rejected``
- any other refund: success, refund id "MOCKRFD-<12 hex>"

Responses are remembered per idempotency key in an in-process dict guarded by a lock, so a retry with the
same key gets the first response, including its transaction id. The container builds one instance per process.
"""

import logging
import threading
from typing import Final
from uuid import uuid4

from hotel_commons.domain.money import Money

from payment.enums import PaymentMethod
from payment.interfaces.gateway import (
    GatewayChargeRequest,
    GatewayChargeResponse,
    GatewayRefundRequest,
    GatewayRefundResponse,
    PaymentGateway,
)

logger = logging.getLogger(__name__)

WALLET_CHARGE_LIMIT: Final = Money.of_rupees(50_000)
REFUND_FAILURE_MARKER: Final = "REFUNDFAIL"
_ID_HEX_LENGTH: Final = 12
_DECLINED_CARD_SUFFIXES: Final = {
    "0002": ("card_declined", "The card was declined by the issuer"),
    "0069": ("expired_card", "The card has expired"),
}
_UPI_FAILURE_PREFIX: Final = "fail"


class MockPaymentGateway(PaymentGateway):
    """Thread-safe and idempotent per key; the rules above decide the first response for a key."""

    def __init__(self) -> None:
        self._charges: dict[str, GatewayChargeResponse] = {}
        self._refunds: dict[str, GatewayRefundResponse] = {}
        self._lock = threading.Lock()

    def charge(self, request: GatewayChargeRequest) -> GatewayChargeResponse:
        with self._lock:
            response = self._charges.get(request.idempotency_key)
            if response is None:
                response = self._charges[request.idempotency_key] = _decide_charge(request)
        logger.info("Mock gateway charge for reference %s: success=%s", request.reference, response.success)
        return response

    def refund(self, request: GatewayRefundRequest) -> GatewayRefundResponse:
        with self._lock:
            response = self._refunds.get(request.idempotency_key)
            if response is None:
                response = self._refunds[request.idempotency_key] = _decide_refund(request)
        logger.info("Mock gateway refund of transaction %s: success=%s", request.transaction_id, response.success)
        return response


def _decide_charge(request: GatewayChargeRequest) -> GatewayChargeResponse:
    failure = _charge_failure(request)
    if failure is not None:
        code, message = failure
        return GatewayChargeResponse(success=False, transaction_id=None, failure_code=code, failure_message=message)
    return GatewayChargeResponse(
        success=True, transaction_id=_new_id("MOCKTXN"), failure_code=None, failure_message=None
    )


def _charge_failure(request: GatewayChargeRequest) -> tuple[str, str] | None:
    if request.method is PaymentMethod.CARD:
        return _DECLINED_CARD_SUFFIXES.get(request.payload.get("card_number", "")[-4:])
    if request.method is PaymentMethod.UPI and request.payload.get("vpa", "").startswith(_UPI_FAILURE_PREFIX):
        return "upi_collect_rejected", "The UPI collect request was rejected"
    if request.method is PaymentMethod.WALLET and request.amount > WALLET_CHARGE_LIMIT:
        return "insufficient_wallet_balance", "The wallet balance is insufficient"
    return None


def _decide_refund(request: GatewayRefundRequest) -> GatewayRefundResponse:
    if REFUND_FAILURE_MARKER in request.transaction_id:
        return GatewayRefundResponse(success=False, refund_id=None, failure_code="refund_rejected")
    return GatewayRefundResponse(success=True, refund_id=_new_id("MOCKRFD"), failure_code=None)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:_ID_HEX_LENGTH]}"
