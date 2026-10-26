"""The payment-method request body, published for the contexts that take payments (LLD §10.3).

``{"method": "CARD" | "UPI" | "WALLET", "<card|upi|wallet>": {...}}``: the method and exactly one details
object, the one of that method. The serializer checks only the shape; the payment method handlers validate
the details when the charge is made. Card number and CVV are write-only, so they are never echoed. A caller
hands ``method_payload()`` to ``ChargeCommand``. Adding a payment method adds its details serializer here,
next to its handler, and no caller changes.
"""

from enum import Enum
from typing import Any, Final

from hotel_commons.serializers.base import BaseHotelSerializer
from hotel_commons.serializers.fields import HotelCharField, HotelEnumField, HotelIntegerField, HotelPhoneField
from rest_framework import serializers

from payment.public.enums import PaymentMethod, WalletProvider


class CardPaymentDetailsSerializer(BaseHotelSerializer):
    card_number = HotelCharField(write_only=True, min_length=12, max_length=19)
    expiry_month = HotelIntegerField(min_value=1, max_value=12)
    expiry_year = HotelIntegerField(min_value=2000, max_value=2100)
    cvv = HotelCharField(write_only=True, min_length=3, max_length=4)
    holder_name = HotelCharField(min_length=2, max_length=100)


class UpiPaymentDetailsSerializer(BaseHotelSerializer):
    vpa = HotelCharField(max_length=321)


class WalletPaymentDetailsSerializer(BaseHotelSerializer):
    provider = HotelEnumField(WalletProvider)
    phone = HotelPhoneField()


_DETAILS_FIELD: Final = {PaymentMethod.CARD: "card", PaymentMethod.UPI: "upi", PaymentMethod.WALLET: "wallet"}


class PaymentMethodRequestSerializer(BaseHotelSerializer):
    """The method and exactly one matching details object; the payment context validates the details."""

    method = HotelEnumField(PaymentMethod)
    card = CardPaymentDetailsSerializer(required=False)
    upi = UpiPaymentDetailsSerializer(required=False)
    wallet = WalletPaymentDetailsSerializer(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        expected = _DETAILS_FIELD.get(attrs["method"])
        if expected is None:
            raise serializers.ValidationError({"method": "This payment method is not supported."})
        sent = [name for name in _DETAILS_FIELD.values() if name in attrs]
        if sent != [expected]:
            raise serializers.ValidationError({expected: f"Send {expected} details, and only those, for this method."})
        return attrs

    def method_payload(self) -> tuple[str, dict[str, Any]]:
        """(method name, the validated details of that method with enums as names), for ``ChargeCommand``."""
        method = self.validated_data["method"]
        details = self.validated_data[_DETAILS_FIELD[method]]
        return method.name, {name: _wire(value) for name, value in details.items()}


def _wire(value: Any) -> Any:
    return value.name if isinstance(value, Enum) else value
