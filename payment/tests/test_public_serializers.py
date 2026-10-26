"""The published payment-method request serializer: one details object per method, the payload for a charge.

No database: the serializer only checks the shape of the body.
"""

import pytest

from payment.public import serializers as public_serializers
from payment.public.enums import PaymentMethod
from payment.public.serializers import PaymentMethodRequestSerializer

CARD = {"card_number": "4242424242424242", "expiry_month": 12, "expiry_year": 2028, "cvv": "987",
        "holder_name": "Rahul Sharma"}


def valid(body: dict[str, object]) -> PaymentMethodRequestSerializer:
    serializer = PaymentMethodRequestSerializer(data=body)
    assert serializer.is_valid(), serializer.errors
    return serializer


@pytest.mark.parametrize(
    ("body", "payload"),
    [
        ({"method": "CARD", "card": CARD}, ("CARD", CARD)),
        ({"method": "UPI", "upi": {"vpa": "rahul@okaxis"}}, ("UPI", {"vpa": "rahul@okaxis"})),
        (
            {"method": "WALLET", "wallet": {"provider": "PAYTM", "phone": "9876543210"}},
            ("WALLET", {"provider": "PAYTM", "phone": "9876543210"}),
        ),
    ],
)
def test_method_payload_is_the_method_name_and_its_details(body: dict[str, object], payload: tuple) -> None:
    assert valid(body).method_payload() == payload


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"method": "CARD", "upi": {"vpa": "rahul@okaxis"}}, "card"),
        ({"method": "UPI", "upi": {"vpa": "rahul@okaxis"}, "card": CARD}, "upi"),
        ({"method": "WALLET"}, "wallet"),
        ({"method": "NETBANKING", "card": CARD}, "method"),
        ({"method": "WALLET", "wallet": {"provider": "GPAY", "phone": "9876543210"}}, "wallet"),
        ({"method": "WALLET", "wallet": {"provider": "PAYTM", "phone": "5876543210"}}, "wallet"),
    ],
)
def test_body_needs_exactly_the_details_of_its_method(body: dict[str, object], field: str) -> None:
    serializer = PaymentMethodRequestSerializer(data=body)
    assert not serializer.is_valid()
    assert field in serializer.errors


def test_method_without_a_details_serializer_is_refused_not_a_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(public_serializers._DETAILS_FIELD, PaymentMethod.WALLET)  # a member added without one
    serializer = PaymentMethodRequestSerializer(data={"method": "WALLET", "upi": {"vpa": "rahul@okaxis"}})
    assert not serializer.is_valid()
    assert "method" in serializer.errors


def test_card_number_and_cvv_are_never_echoed() -> None:
    data = valid({"method": "CARD", "card": CARD}).data
    assert "card_number" not in data["card"]
    assert "cvv" not in data["card"]
    assert data["card"]["holder_name"] == "Rahul Sharma"
