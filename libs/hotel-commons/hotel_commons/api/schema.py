"""OpenAPI pieces shared by every app (LLD §3.9): the bearer security scheme and the response envelopes.

``HotelCommonsConfig.ready()`` imports this module so drf-spectacular registers the authentication
extension. Every view action documents itself as
``@extend_schema(request=..., responses={200: envelope(XSerializer, "XEnvelope"), 400: ERROR_ENVELOPE})``,
or with ``envelope_list(XSerializer, "XListEnvelope", paginated=...)`` when ``data`` holds several items.

An envelope is always one JSON object, so every envelope opts out of drf-spectacular's list mechanics,
which would otherwise document the response of a ``list`` action as an array of envelopes.
"""

from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.utils import extend_schema_serializer, inline_serializer
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
    return _single_object(
        inline_serializer(
            name=name,
            fields={
                "status": HotelIntegerField(help_text="Service status code, 2100000 on success."),
                "message": HotelCharField(),
                "data": _data_field(serializer_or_none),
                "request_id": HotelCharField(allow_null=True),
            },
        )
    )


def envelope_list(
    serializer_class: type[serializers.BaseSerializer], name: str, *, paginated: bool = False
) -> serializers.Serializer:
    """The success envelope of a list endpoint: ``data`` is an array of ``serializer_class`` items, or with
    ``paginated=True`` the ``page_data`` object ``{"items": [...], "page", "page_size", "total"}``, documented
    as the component ``<name without "Envelope">Data``."""
    items = serializer_class(many=True)
    if not paginated:
        return envelope(items, name)
    page = inline_serializer(
        name=f"{name.removesuffix('Envelope')}Data",
        fields={
            "items": items,
            "page": HotelIntegerField(help_text="1-based page number."),
            "page_size": HotelIntegerField(),
            "total": HotelIntegerField(help_text="Items across all pages."),
        },
    )
    return envelope(_single_object(page), name)


def _single_object(serializer: serializers.Serializer) -> serializers.Serializer:
    """Mark ``serializer`` as one object, never wrapped in an array (drf-spectacular's ``many=False``)."""
    extend_schema_serializer(many=False)(type(serializer))
    return serializer


def _data_field(
    serializer_or_none: serializers.BaseSerializer | type[serializers.BaseSerializer] | None,
) -> serializers.Field:
    if serializer_or_none is None:
        return serializers.JSONField(allow_null=True)
    if isinstance(serializer_or_none, type):
        return serializer_or_none()
    return serializer_or_none


ERROR_ENVELOPE: serializers.Serializer = _single_object(
    inline_serializer(
        name="ErrorEnvelope",
        fields={
            "status": HotelIntegerField(help_text="Service status code, e.g. 2105002."),
            "message": HotelCharField(),
            "errors": serializers.DictField(
                child=serializers.ListField(child=serializers.CharField()),
                allow_null=True,
                help_text='Dotted field path -> error messages, e.g. {"card.card_number": ["is invalid"]}.',
            ),
            "data": serializers.JSONField(allow_null=True),
            "request_id": HotelCharField(allow_null=True),
        },
    )
)
