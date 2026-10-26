"""Payment response serializer (LLD §10.3, §9.8).

Serialises a ``PaymentDTO``: amounts as rupee strings with a sibling ``currency``, enums as member names.
``failure_code``/``failure_message`` are null unless the payment failed. ``gateway_txn_id`` is visible to ADMIN
only and dropped from everyone else's response, so the OpenAPI schema lists it as optional.
"""

from typing import Any

from drf_spectacular.extensions import OpenApiSerializerExtension
from hotel_commons.security.principal import Role
from hotel_commons.serializers.base import BaseHotelSerializer
from hotel_commons.serializers.fields import HotelAmountField, HotelCharField, HotelEnumField, HotelUUIDField

from payment.enums import PaymentMethod, PaymentStatus


class PaymentSerializer(BaseHotelSerializer):
    payment_id = HotelUUIDField(read_only=True)
    booking_id = HotelUUIDField(read_only=True)
    amount = HotelAmountField(read_only=True)
    refunded_amount = HotelAmountField(read_only=True)
    currency = HotelCharField(source="amount.currency", read_only=True)
    status = HotelEnumField(PaymentStatus, read_only=True)
    method = HotelEnumField(PaymentMethod, read_only=True)
    instrument = HotelCharField(source="instrument_display", read_only=True)
    failure_code = HotelCharField(read_only=True, allow_null=True)
    failure_message = HotelCharField(read_only=True, allow_null=True)
    gateway_txn_id = HotelCharField(read_only=True, allow_null=True, read_roles=[Role.ADMIN])


class PaymentSerializerSchema(OpenApiSerializerExtension):
    """drf-spectacular marks every read-only field required; a field omitted for some readers is not."""

    target_class = PaymentSerializer

    def map_serializer(self, auto_schema: Any, direction: str) -> dict[str, Any]:
        schema = auto_schema._map_serializer(self.target, direction, bypass_extensions=True)
        omitted = {name for name, field in self.target.fields.items() if field.read_roles and not field.mask_as}
        schema["required"] = [name for name in schema.get("required", []) if name not in omitted]
        return schema
