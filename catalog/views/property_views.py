"""Property and room-type endpoints (LLD §10.2 rows 6-9).

Owners reach their own accounts and properties only (IDOR rules, relation "owner"); ADMIN reaches all.
Property details are public.
"""

from uuid import UUID

from drf_spectacular.utils import extend_schema
from hotel_commons.api.response import api_response
from hotel_commons.api.schema import ERROR_ENVELOPE, envelope
from hotel_commons.api.views import BaseViewSet
from hotel_commons.idempotency.decorators import idempotent
from hotel_commons.security.idor import IdorRule
from hotel_commons.security.permissions import IsAuthenticatedPrincipal, require_roles
from hotel_commons.security.principal import Role
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from catalog import container
from catalog.serializers.property_serializers import (
    PropertyOnboardSerializer,
    PropertySerializer,
    RoomTypeCreateSerializer,
    RoomTypeSerializer,
)
from catalog.views.owner_account_views import IDEMPOTENCY_KEY_PARAMETER

OWNER_OR_ADMIN = [IsAuthenticatedPrincipal, require_roles(Role.OWNER, Role.ADMIN)]
OWN_ACCOUNT = (IdorRule("owner_account", "account_id", "path", frozenset({"owner"})),)
OWN_PROPERTY = (IdorRule("property", "property_id", "path", frozenset({"owner"})),)
WRITE_ERRORS = {
    400: ERROR_ENVELOPE,
    401: ERROR_ENVELOPE,
    403: ERROR_ENVELOPE,
    404: ERROR_ENVELOPE,
    409: ERROR_ENVELOPE,
    422: ERROR_ENVELOPE,
    429: ERROR_ENVELOPE,
}


class PropertyViewSet(BaseViewSet):
    permission_classes_by_action = {
        "list_for_account": OWNER_OR_ADMIN,
        "onboard": OWNER_OR_ADMIN,
        "retrieve": [AllowAny],
    }
    idor_rules = {"list_for_account": OWN_ACCOUNT, "onboard": OWN_ACCOUNT}
    throttle_scope_by_action = {"onboard": "catalog_write"}

    @extend_schema(
        operation_id="catalog_owner_account_properties_list",
        tags=["catalog"],
        responses={
            200: envelope(PropertySerializer(many=True), "PropertyListEnvelope"),
            401: ERROR_ENVELOPE,
            403: ERROR_ENVELOPE,
            404: ERROR_ENVELOPE,
            429: ERROR_ENVELOPE,
        },
    )
    def list_for_account(self, request: Request, account_id: UUID) -> Response:
        properties = container.query_service().list_properties(account_id)
        return api_response(PropertySerializer(properties, many=True, context={"request": request}).data)

    @extend_schema(
        operation_id="catalog_owner_account_properties_onboard",
        tags=["catalog"],
        request=PropertyOnboardSerializer,
        parameters=[IDEMPOTENCY_KEY_PARAMETER],
        responses={201: envelope(PropertySerializer, "PropertyEnvelope"), **WRITE_ERRORS},
    )
    @idempotent("catalog_write", required=False)
    def onboard(self, request: Request, account_id: UUID) -> Response:
        context = {"request": request}
        serializer = PropertyOnboardSerializer(data=request.data, context=context)
        serializer.is_valid(raise_exception=True)
        property_ = container.onboarding_service().onboard(account_id, serializer.to_draft())
        return api_response(PropertySerializer(property_, context=context).data, http_status=status.HTTP_201_CREATED)

    @extend_schema(
        operation_id="catalog_properties_retrieve",
        tags=["catalog"],
        auth=[],
        responses={
            200: envelope(PropertySerializer, "PropertyDetailEnvelope"),
            404: ERROR_ENVELOPE,
            429: ERROR_ENVELOPE,
        },
    )
    def retrieve(self, request: Request, property_id: UUID) -> Response:
        property_ = container.query_service().get_property(property_id)
        return api_response(PropertySerializer(property_, context={"request": request}).data)


class RoomTypeViewSet(BaseViewSet):
    permission_classes_by_action = {"create": OWNER_OR_ADMIN}
    idor_rules = {"create": OWN_PROPERTY}
    throttle_scope_by_action = {"create": "catalog_write"}

    @extend_schema(
        operation_id="catalog_property_room_types_create",
        tags=["catalog"],
        request=RoomTypeCreateSerializer,
        parameters=[IDEMPOTENCY_KEY_PARAMETER],
        responses={201: envelope(RoomTypeSerializer, "RoomTypeEnvelope"), **WRITE_ERRORS},
    )
    @idempotent("catalog_write", required=False)
    def create(self, request: Request, property_id: UUID) -> Response:
        context = {"request": request}
        serializer = RoomTypeCreateSerializer(data=request.data, context=context)
        serializer.is_valid(raise_exception=True)
        room_type = container.room_type_service().add_room_type(property_id, serializer.to_draft())
        return api_response(RoomTypeSerializer(room_type, context=context).data, http_status=status.HTTP_201_CREATED)
