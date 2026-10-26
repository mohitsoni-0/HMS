"""Charge and refund use cases over in-memory repositories and the mock gateway.

Pure unit tests: no Django, no database.
"""

import dataclasses
from datetime import UTC, datetime
from uuid import UUID

import pytest
from hotel_commons.domain.money import Money
from hotel_commons.exceptions import BusinessRuleViolation, UpstreamFailure
from hotel_commons.status_code import IDEMPOTENCY_KEY_REUSED
from hotel_commons.testing.clock import FixedClock
from hotel_commons.testing.unit_of_work import NoopUnitOfWork

from payment.domain.instrument import ParsedInstrument, PaymentInstrument
from payment.domain.payment import Payment
from payment.enums import PaymentMethod, PaymentStatus, RefundStatus
from payment.interfaces.gateway import GatewayChargeRequest
from payment.public.dto import ChargeCommand, RefundCommand
from payment.services.charge_service import ChargeService
from payment.services.refund_service import RefundService
from payment.status_code import PAYMENT_NOT_REFUNDABLE, REFUND_EXCEEDS_PAYMENT, REFUND_FAILED
from payment.tests.fakes import (
    CountingGateway,
    InMemoryPaymentRepository,
    InMemoryRefundRepository,
    fingerprinter,
    method_registry,
)

NOW = datetime(2026, 1, 5, 6, 30, tzinfo=UTC)
BOOKING_ID = UUID("0192f5c5-0000-7000-8000-000000000001")
CARD = {"card_number": "4242424242424242", "expiry_month": 12, "expiry_year": 2028, "cvv": "987",
        "holder_name": "Rahul Sharma"}


class Harness:
    def __init__(self) -> None:
        self.payments = InMemoryPaymentRepository()
        self.refunds = InMemoryRefundRepository()
        self.gateway = CountingGateway()
        self.uow = NoopUnitOfWork()
        self.charges = ChargeService(
            self.payments, method_registry(FixedClock(NOW)), self.gateway, self.uow, fingerprinter()
        )
        self.refund_service = RefundService(self.payments, self.refunds, self.gateway, self.uow)

    def charge(self, *, key: str = "pay:booking-1:attempt-1", rupees: str = "9600.00",
               method: str = "CARD", details: dict[str, object] | None = None) -> Payment:
        return self.charges.charge(
            ChargeCommand(
                booking_id=BOOKING_ID,
                payer_id="guest-1",
                amount=Money.of_rupees(rupees),
                method=method,
                method_details=CARD if details is None else details,
                idempotency_key=key,
            )
        )

    def refund(self, payment: Payment, rupees: str, *, key: str) -> object:
        return self.refund_service.refund(
            RefundCommand(payment_id=payment.id, amount=Money.of_rupees(rupees), reason="booking cancelled",
                          idempotency_key=key)
        )


@pytest.fixture
def harness() -> Harness:
    return Harness()


def test_successful_card_charge_is_persisted(harness: Harness) -> None:
    payment = harness.charge()
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.gateway_txn_id is not None and payment.gateway_txn_id.startswith("MOCKTXN-")
    assert payment.instrument.display == "VISA •••• 4242"
    assert harness.payments.get(payment.id) == payment


def test_charge_declined_for_card_ending_0002(harness: Harness) -> None:
    payment = harness.charge(details={**CARD, "card_number": "4000000000000002"})
    assert payment.status is PaymentStatus.FAILED
    assert payment.failure_code == "card_declined"
    assert payment.gateway_txn_id is None
    assert harness.payments.get(payment.id).status is PaymentStatus.FAILED  # committed, not rolled back
    assert harness.uow.rollbacks == 0


@pytest.mark.parametrize(
    ("method", "details", "rupees", "failure_code"),
    [
        ("CARD", {**CARD, "card_number": "4000000000000069"}, "100.00", "expired_card"),
        ("UPI", {"vpa": "failing@okaxis"}, "100.00", "upi_collect_rejected"),
        ("WALLET", {"provider": "PAYTM", "phone": "9876543210"}, "50000.01", "insufficient_wallet_balance"),
    ],
)
def test_mock_gateway_failures(harness: Harness, method: str, details: dict[str, object], rupees: str,
                               failure_code: str) -> None:
    payment = harness.charge(method=method, details=details, rupees=rupees)
    assert payment.status is PaymentStatus.FAILED
    assert payment.failure_code == failure_code


def test_wallet_charge_at_limit_succeeds(harness: Harness) -> None:
    payment = harness.charge(method="WALLET", details={"provider": "PAYTM", "phone": "9876543210"}, rupees="50000.00")
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.instrument.sensitive_ref == "9876543210"


def test_same_idempotency_key_returns_same_payment_single_gateway_call(harness: Harness) -> None:
    first = harness.charge(key="pay:booking-1:k1")
    second = harness.charge(key="pay:booking-1:k1")
    assert second == first
    assert harness.gateway.charge_calls == 1
    assert len(harness.payments.rows) == 1


def test_duplicate_insert_race_returns_the_winning_payment(harness: Harness) -> None:
    winner = harness.charge(key="pay:booking-1:race")
    harness.gateway.charge_calls = 0
    find = harness.payments.find_by_idempotency_key
    lookups: list[str] = []

    def find_missing_first(key: str) -> Payment | None:  # the racing request read before the winner committed
        lookups.append(key)
        return None if len(lookups) == 1 else find(key)

    harness.payments.find_by_idempotency_key = find_missing_first  # type: ignore[method-assign]
    assert harness.charge(key="pay:booking-1:race") == winner
    assert harness.gateway.charge_calls == 0
    assert harness.uow.rollbacks == 1


def test_same_key_with_other_method_is_reused_not_replayed(harness: Harness) -> None:
    declined = harness.charge(key="pay:booking-1:k1", details={**CARD, "card_number": "4000000000000002"})
    assert declined.status is PaymentStatus.FAILED

    with pytest.raises(BusinessRuleViolation) as error:
        harness.charge(key="pay:booking-1:k1", method="UPI", details={"vpa": "rahul@okaxis"})

    assert error.value.status_code is IDEMPOTENCY_KEY_REUSED
    assert harness.gateway.charge_calls == 1
    assert len(harness.payments.rows) == 1


def test_same_key_with_other_card_details_is_reused(harness: Harness) -> None:
    harness.charge(key="pay:booking-1:k1")
    with pytest.raises(BusinessRuleViolation) as error:
        harness.charge(key="pay:booking-1:k1", details={**CARD, "cvv": "123"})
    assert error.value.status_code is IDEMPOTENCY_KEY_REUSED


def test_same_request_in_another_key_order_is_a_replay(harness: Harness) -> None:
    first = harness.charge(key="pay:booking-1:k1")
    assert harness.charge(key="pay:booking-1:k1", details=dict(reversed(CARD.items()))) == first
    assert harness.gateway.charge_calls == 1


def test_find_by_idempotency_key_compares_the_request_when_given(harness: Harness) -> None:
    payment = harness.charge(key="pay:booking-1:k1")
    find = harness.charges.find_by_idempotency_key

    assert find("pay:booking-1:k1") == payment
    assert find("pay:booking-1:k1", method="CARD", method_details=CARD) == payment
    assert find("pay:booking-1:unused", method="UPI", method_details={"vpa": "rahul@okaxis"}) is None
    with pytest.raises(BusinessRuleViolation) as error:
        find("pay:booking-1:k1", method="UPI", method_details={"vpa": "rahul@okaxis"})
    assert error.value.status_code is IDEMPOTENCY_KEY_REUSED


def test_payment_without_fingerprint_is_replayed_for_any_request(harness: Harness) -> None:
    legacy = harness.charge(key="pay:booking-1:k1")
    harness.payments.rows[legacy.id] = dataclasses.replace(legacy, request_fingerprint=None)

    replay = harness.charge(key="pay:booking-1:k1", method="UPI", details={"vpa": "rahul@okaxis"})

    assert replay.id == legacy.id
    assert harness.gateway.charge_calls == 1


def test_insert_race_lost_to_another_request_is_reused(harness: Harness) -> None:
    harness.charge(key="pay:booking-1:race")
    find = harness.payments.find_by_idempotency_key
    lookups: list[str] = []

    def find_missing_first(key: str) -> Payment | None:
        lookups.append(key)
        return None if len(lookups) == 1 else find(key)

    harness.payments.find_by_idempotency_key = find_missing_first  # type: ignore[method-assign]
    with pytest.raises(BusinessRuleViolation) as error:
        harness.charge(key="pay:booking-1:race", method="UPI", details={"vpa": "rahul@okaxis"})
    assert error.value.status_code is IDEMPOTENCY_KEY_REUSED


def test_refund_cannot_exceed_payment(harness: Harness) -> None:
    payment = harness.charge(rupees="1000.00")
    with pytest.raises(BusinessRuleViolation) as error:
        harness.refund(payment, "1000.01", key="refund:1")
    assert error.value.status_code is REFUND_EXCEEDS_PAYMENT
    assert harness.gateway.refund_calls == 0
    assert harness.payments.get(payment.id).refunded_amount == Money.zero()


def test_failed_payment_is_not_refundable(harness: Harness) -> None:
    payment = harness.charge(details={**CARD, "card_number": "4000000000000002"})
    with pytest.raises(BusinessRuleViolation) as error:
        harness.refund(payment, "1.00", key="refund:1")
    assert error.value.status_code is PAYMENT_NOT_REFUNDABLE


def test_partial_then_full_refund_status(harness: Harness) -> None:
    payment = harness.charge(rupees="1000.00")

    partial = harness.refund(payment, "400.00", key="refund:1")
    assert partial.status is RefundStatus.SUCCEEDED
    assert partial.gateway_refund_id.startswith("MOCKRFD-")
    stored = harness.payments.get(payment.id)
    assert stored.status is PaymentStatus.PARTIALLY_REFUNDED
    assert stored.refundable_amount() == Money.of_rupees("600.00")

    assert harness.refund(payment, "400.00", key="refund:1") == partial  # replay: no second refund
    assert harness.gateway.refund_calls == 1

    harness.refund(payment, "600.00", key="refund:2")
    stored = harness.payments.get(payment.id)
    assert stored.status is PaymentStatus.REFUNDED
    assert stored.refunded_amount == Money.of_rupees("1000.00")
    assert len(harness.refunds.rows) == 2

    with pytest.raises(BusinessRuleViolation) as error:
        harness.refund(payment, "0.01", key="refund:3")
    assert error.value.status_code is PAYMENT_NOT_REFUNDABLE


def test_refund_failure_raises_upstream_failure(harness: Harness) -> None:
    payment = harness.charge(rupees="1000.00")
    harness.payments.rows[payment.id] = dataclasses.replace(payment, gateway_txn_id="MOCKTXN-REFUNDFAIL-1")

    with pytest.raises(UpstreamFailure) as error:
        harness.refund(payment, "100.00", key="refund:1")

    assert error.value.status_code is REFUND_FAILED
    assert error.value.http_status == 502
    assert harness.refunds.rows == {}
    assert harness.payments.get(payment.id).status is PaymentStatus.SUCCEEDED
    assert harness.uow.rollbacks == 1


def test_charge_command_repr_hides_card_details() -> None:
    command = ChargeCommand(
        booking_id=BOOKING_ID, payer_id="guest-1", amount=Money(100), method="CARD", method_details=CARD,
        idempotency_key="pay:1",
    )
    gateway_request = GatewayChargeRequest("pay:1", Money(100), PaymentMethod.CARD, {"card_number": CARD["card_number"]})
    parsed = ParsedInstrument(PaymentInstrument(PaymentMethod.UPI, "r****@okaxis", "rahul@okaxis"), {"vpa": "rahul@okaxis"})
    for text in (repr(command), repr(gateway_request), repr(parsed)):
        assert "4242424242424242" not in text
        assert "987" not in text
        assert "rahul@okaxis" not in text
    assert "r****@okaxis" in repr(parsed)
