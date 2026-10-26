"""OpenAPI pieces shared by every app (LLD §3.9): the bearer security scheme and the response envelopes.

``HotelCommonsConfig.ready()`` imports this module so drf-spectacular registers the authentication
extension. Every view action documents itself as
``@extend_schema(request=..., responses={200: envelope(XSerializer, "XEnvelope"), 400: ERROR_ENVELOPE})``.
"""

from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.utils import inline_serializer
from rest_framework import serializers

from hotel_commons.serializers.fields import HotelCharField, HotelIntegerField


class PrincipalAuthenticationScheme(OpenApiAuthenticationExtension):
    """Documents ``PrincipalAuthentication`` as an HTTP bearer (JWT) scheme."""

    target_class = "hotel_commons.api.authentication.PrincipalAuthentication"
    name = "BearerAuth"

    def get_security_definition(self, auto_schema: Any) -> dict[str, str]:
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}


def envelope(
    serializer_or_none: serializers.BaseSerializer | type[serializers.BaseSerializer] | None, name: str
) -> serializers.Serializer:
    """The success envelope around ``serializer_or_none`` (a class or an instance, e.g. ``X(many=True)``);
    ``None`` documents ``"data": null``."""
    return inline_serializer(
        name=name,
        fields={
            "status": HotelIntegerField(help_text="Service status code, 2100000 on success."),
            "message": HotelCharField(),
            "data": _data_field(serializer_or_none),
            "request_id": HotelCharField(allow_null=True),
        },
    )


def _data_field(
    serializer_or_none: serializers.BaseSerializer | type[serializers.BaseSerializer] | None,
) -> serializers.Field:
    if serializer_or_none is None:
        return serializers.JSONField(allow_null=True)
    if isinstance(serializer_or_none, type):
        return serializer_or_none()
    return serializer_or_none


ERROR_ENVELOPE: serializers.Serializer = inline_serializer(
    name="ErrorEnvelope",
    fields={
        "status": HotelIntegerField(help_text="Service status code, e.g. 2105002."),
        "message": HotelCharField(),
        "errors": serializers.DictField(allow_null=True, help_text="Field name -> error messages."),
        "data": serializers.JSONField(allow_null=True),
        "request_id": HotelCharField(allow_null=True),
    },
)
