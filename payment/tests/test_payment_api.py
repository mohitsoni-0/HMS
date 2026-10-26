"""Payment persistence and ``GET /hotel/v1/payments/{payment_id}`` against Postgres (LLD §9, §10.2 row 16).

Every test needs the database and the full project: run by integration with pytest-django.
"""

from uuid import uuid4

import pytest
from django.db import connection
from django.urls import reverse
from hotel_commons.domain.money import Money
from hotel_commons.testing.tokens import auth_header
from rest_framework.test import APIClient
from uuid6 import uuid7

from payment.domain.errors import DuplicatePayment
from payment.domain.instrument import PaymentInstrument
from payment.domain.payment import Payment
from payment.enums import PaymentMethod, PaymentStatus
from payment.models.payment_models import PaymentLog, RefundLog
from payment.public.api import get_payment_api
from payment.public.dto import ChargeCommand, PaymentDTO, RefundCommand
from payment.repositories.payment_repository import DjangoPaymentRepository

pytestmark = pytest.mark.django_db

GUEST = "guest-1"


def charge(*, payer_id: str = GUEST, key: str | None = None, rupees: str = "9600.00") -> PaymentDTO:
    return get_payment_api().charge(
        ChargeCommand(
            booking_id=uuid7(),
            payer_id=payer_id,
            amount=Money.of_rupees(rupees),
            method="UPI",
            method_details={"vpa": "rahul@okaxis"},
            idempotency_key=key or f"pay:{uuid4().hex}",
        )
    )


def payment_url(payment_id: object) -> str:
    return reverse("payment-detail", kwargs={"payment_id": payment_id})


def test_guest_reads_own_payment_without_gateway_txn_id() -> None:
    payment = charge()
    response = APIClient().get(payment_url(payment.payment_id), **auth_header(GUEST, "GUEST"))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["payment_id"] == str(payment.payment_id)
    assert data["amount"] == "9600.00"
    assert data["refunded_amount"] == "0.00"
    assert data["currency"] == "INR"
    assert data["status"] == "SUCCEEDED"
    assert data["method"] == "UPI"
    assert data["instrument"] == "r****@okaxis"
    assert data["failure_code"] is None
    assert "gateway_txn_id" not in data


def test_admin_sees_gateway_txn_id() -> None:
    payment = charge()
    response = APIClient().get(payment_url(payment.payment_id), **auth_header("admin-1", "ADMIN"))

    assert response.status_code == 200
    assert response.json()["data"]["gateway_txn_id"] == payment.gateway_txn_id


def test_other_guest_gets_not_found() -> None:
    payment = charge()
    response = APIClient().get(payment_url(payment.payment_id), **auth_header("guest-2", "GUEST"))

    assert response.status_code == 404
    assert response.json()["data"] is None


def test_owner_role_is_forbidden_and_anonymous_unauthenticated() -> None:
    payment = charge()

    assert APIClient().get(payment_url(payment.payment_id), **auth_header("owner-1", "OWNER")).status_code == 403
    assert APIClient().get(payment_url(payment.payment_id)).status_code == 401


def test_admin_reading_unknown_payment_gets_payment_not_found() -> None:
    response = APIClient().get(payment_url(uuid7()), **auth_header("admin-1", "ADMIN"))

    assert response.status_code == 404
    assert response.json()["status"] == 2106001


def test_same_key_charges_once_and_upi_vpa_is_encrypted_at_rest() -> None:
    first = charge(key="pay:booking-1:attempt-1")
    second = charge(key="pay:booking-1:attempt-1")

    assert second == first
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT instrument_ref FROM "payment"."payment" WHERE idempotency_key = %s', ["pay:booking-1:attempt-1"]
        )
        rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0].startswith("ENC_")
    assert "rahul" not in rows[0][0]


def test_duplicate_insert_raises_duplicate_payment_and_keeps_transaction_usable() -> None:
    repository = DjangoPaymentRepository()
    instrument = PaymentInstrument(PaymentMethod.UPI, "r****@okaxis", "rahul@okaxis")

    def new_payment() -> Payment:
        return Payment.initiate(
            booking_id=uuid7(), payer_id=GUEST, amount=Money(100), instrument=instrument, idempotency_key="pay:dup"
        )

    first = new_payment()
    repository.add(first)
    with pytest.raises(DuplicatePayment):
        repository.add(new_payment())

    assert repository.find_by_idempotency_key("pay:dup") == first


def test_partial_refund_updates_payment_and_appends_log_rows() -> None:
    payment = charge(rupees="1000.00")
    api = get_payment_api()

    result = api.refund(
        RefundCommand(payment.payment_id, Money.of_rupees("400.00"), "booking cancelled", "refund:booking-1")
    )
    replay = api.refund(
        RefundCommand(payment.payment_id, Money.of_rupees("400.00"), "booking cancelled", "refund:booking-1")
    )

    assert replay == result
    assert result.amount == Money.of_rupees("400.00")
    stored = api.get_payment(payment.payment_id)
    assert stored.status is PaymentStatus.PARTIALLY_REFUNDED
    assert stored.refunded_amount == Money.of_rupees("400.00")
    assert PaymentLog.objects.filter(log_id=payment.payment_id).count() == 3  # insert, charge outcome, refund
    assert RefundLog.objects.filter(log_id=result.refund_id).count() == 1
