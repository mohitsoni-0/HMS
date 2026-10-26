"""Payment method handlers: card (Luhn, brand, expiry, CVV), UPI and wallet validation, and the registry.

Pure unit tests: no Django, no database.
"""

from datetime import UTC, datetime

import pytest
from hotel_commons.exceptions import ValidationFailed
from hotel_commons.testing.clock import FixedClock

from payment.domain.methods import CardHandler, PaymentMethodRegistry, UpiHandler, WalletHandler
from payment.enums import PaymentMethod, WalletProvider
from payment.status_code import INVALID_PAYMENT_DETAILS, UNSUPPORTED_PAYMENT_METHOD
from payment.tests.fakes import method_registry

NOW = datetime(2026, 1, 5, 6, 30, tzinfo=UTC)


def card(**overrides: object) -> dict[str, object]:
    details: dict[str, object] = {
        "card_number": "4242424242424242",
        "expiry_month": 12,
        "expiry_year": 2028,
        "cvv": "123",
        "holder_name": "Rahul Sharma",
    }
    details.update(overrides)
    return details


def assert_invalid(error: pytest.ExceptionInfo[ValidationFailed], field: str, secret: str) -> None:
    assert error.value.status_code is INVALID_PAYMENT_DETAILS
    assert error.value.message == f"Invalid {field}"
    assert secret not in repr(error.value)
    assert secret not in str(error.value.errors)


@pytest.fixture
def card_handler() -> CardHandler:
    return CardHandler(FixedClock(NOW))


def test_card_luhn_brand_expiry_cvv(card_handler: CardHandler) -> None:
    parsed = card_handler.parse(card())
    assert parsed.instrument.method is PaymentMethod.CARD
    assert parsed.instrument.display == "VISA •••• 4242"
    assert parsed.instrument.sensitive_ref is None
    assert parsed.gateway_payload["card_number"] == "4242424242424242"

    with pytest.raises(ValidationFailed) as bad_luhn:
        card_handler.parse(card(card_number="4242424242424241"))
    assert_invalid(bad_luhn, "card_number", "4242424242424241")

    with pytest.raises(ValidationFailed) as too_short:
        card_handler.parse(card(card_number="42424"))
    assert_invalid(too_short, "card_number", "42424")

    with pytest.raises(ValidationFailed) as past:
        card_handler.parse(card(expiry_month=12, expiry_year=2025))
    assert past.value.message == "Invalid expiry_year"

    assert card_handler.parse(card(expiry_month=1, expiry_year=2026)).instrument.display == "VISA •••• 4242"

    with pytest.raises(ValidationFailed) as bad_month:
        card_handler.parse(card(expiry_month=13))
    assert bad_month.value.message == "Invalid expiry_month"

    with pytest.raises(ValidationFailed) as bad_cvv:
        card_handler.parse(card(cvv="12"))
    assert_invalid(bad_cvv, "cvv", "12")

    with pytest.raises(ValidationFailed) as no_holder:
        card_handler.parse(card(holder_name=" R "))
    assert no_holder.value.message == "Invalid holder_name"


@pytest.mark.parametrize(
    ("number", "cvv", "display"),
    [
        ("5555555555554444", "123", "MASTERCARD •••• 4444"),
        ("2223003122003222", "123", "MASTERCARD •••• 3222"),
        ("6522840000000005", "123", "RUPAY •••• 0005"),
        ("378282246310005", "1234", "AMEX •••• 0005"),
    ],
)
def test_card_brands_by_prefix(card_handler: CardHandler, number: str, cvv: str, display: str) -> None:
    assert card_handler.parse(card(card_number=number, cvv=cvv)).instrument.display == display


def test_amex_needs_four_digit_cvv(card_handler: CardHandler) -> None:
    with pytest.raises(ValidationFailed) as error:
        card_handler.parse(card(card_number="378282246310005", cvv="123"))
    assert error.value.message == "Invalid cvv"


def test_unknown_card_brand_is_invalid(card_handler: CardHandler) -> None:
    with pytest.raises(ValidationFailed) as error:
        card_handler.parse(card(card_number="9999999999999995"))
    assert_invalid(error, "card_number", "9999999999999995")


def test_upi_and_wallet_validation() -> None:
    upi = UpiHandler().parse({"vpa": "rahul@okaxis"})
    assert upi.instrument.method is PaymentMethod.UPI
    assert upi.instrument.display == "r****@okaxis"
    assert upi.instrument.sensitive_ref == "rahul@okaxis"
    assert "rahul@okaxis" not in repr(upi)

    for vpa in ("rahul", "r@okaxis", "rahul@ok1axis", "rahul@okaxis "):
        with pytest.raises(ValidationFailed) as bad_vpa:
            UpiHandler().parse({"vpa": vpa})
        assert_invalid(bad_vpa, "vpa", vpa)

    wallet = WalletHandler().parse({"provider": "PAYTM", "phone": "9876543210"})
    assert wallet.instrument.method is PaymentMethod.WALLET
    assert wallet.instrument.display == "PAYTM ******3210"
    assert wallet.instrument.sensitive_ref == "9876543210"
    assert WalletHandler().parse({"provider": WalletProvider.PHONEPE, "phone": "6000000001"}).instrument.display == (
        "PHONEPE ******0001"
    )

    with pytest.raises(ValidationFailed) as bad_provider:
        WalletHandler().parse({"provider": "GPAY", "phone": "9876543210"})
    assert bad_provider.value.message == "Invalid provider"

    for phone in ("5876543210", "987654321", "98765432101", "٩876543210"):
        with pytest.raises(ValidationFailed) as bad_phone:
            WalletHandler().parse({"provider": "PAYTM", "phone": phone})
        assert_invalid(bad_phone, "phone", phone)


def test_registry_resolves_by_member_or_name_and_rejects_unknown() -> None:
    registry = method_registry(FixedClock(NOW))
    assert registry.get("UPI").method is PaymentMethod.UPI
    assert registry.get(PaymentMethod.CARD).method is PaymentMethod.CARD
    for unknown in ("NETBANKING", "upi", 2):
        with pytest.raises(ValidationFailed) as error:
            registry.get(unknown)  # type: ignore[arg-type]
        assert error.value.status_code is UNSUPPORTED_PAYMENT_METHOD

    with pytest.raises(ValidationFailed):
        PaymentMethodRegistry().get(PaymentMethod.WALLET)
