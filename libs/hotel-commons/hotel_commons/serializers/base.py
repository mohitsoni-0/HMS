"""Base serializers (LLD §3.14). Every request and response serializer extends one of these.

Views pass ``context={"request": request}`` so field-level security knows the caller. The model serializer
also builds Hotel*Fields for the columns it generates, so a generated field follows the same wire rules as a
declared one: an ``EnumField`` column speaks member names and an ``EncryptedTextField`` column is a PII field
masked by its PI type.
"""

from typing import Any, Final

from django.db import models
from rest_framework import serializers

from hotel_commons.models.fields import EncryptedTextField, EnumField
from hotel_commons.security.field_level import FieldLevelSecurityMixin
from hotel_commons.serializers.fields import (
    HotelBooleanField,
    HotelCharField,
    HotelDateField,
    HotelDateTimeField,
    HotelEmailField,
    HotelEncryptedCharField,
    HotelEnumField,
    HotelIntegerField,
    HotelListField,
    HotelTimeField,
    HotelUUIDField,
)

_HOTEL_FIELDS: Final[dict[type[serializers.Field], type[serializers.Field]]] = {
    serializers.BooleanField: HotelBooleanField,
    serializers.CharField: HotelCharField,
    serializers.DateField: HotelDateField,
    serializers.DateTimeField: HotelDateTimeField,
    serializers.EmailField: HotelEmailField,
    serializers.IntegerField: HotelIntegerField,
    serializers.ListField: HotelListField,
    serializers.TimeField: HotelTimeField,
    serializers.UUIDField: HotelUUIDField,
}
_ENUM_FIELD_KWARGS: Final = frozenset({"read_only", "required", "allow_null", "label", "help_text"})


class BaseHotelSerializer(FieldLevelSecurityMixin, serializers.Serializer):
    """Plain serializer with field-level security."""


class BaseHotelModelSerializer(FieldLevelSecurityMixin, serializers.ModelSerializer):
    """Model serializer with field-level security whose generated fields are Hotel*Fields."""

    def build_standard_field(
        self, field_name: str, model_field: models.Field
    ) -> tuple[type[serializers.Field], dict[str, Any]]:
        field_class, field_kwargs = super().build_standard_field(field_name, model_field)
        if isinstance(model_field, EnumField):
            kwargs = {key: value for key, value in field_kwargs.items() if key in _ENUM_FIELD_KWARGS}
            return HotelEnumField, {"enum": model_field.enum, **kwargs}
        if isinstance(model_field, EncryptedTextField):
            return HotelEncryptedCharField, {**field_kwargs, "mask_as": model_field.pi_type}
        return _HOTEL_FIELDS.get(field_class, field_class), field_kwargs
