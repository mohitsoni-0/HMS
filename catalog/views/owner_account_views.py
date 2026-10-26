"""``POST /catalog/owner-accounts`` (LLD §10.2 row 5): an OWNER (or ADMIN) opens an account they own."""

from drf_spectacular.utils import OpenApiParameter, extend_schema
from hotel_commons.api.response import api_response
from hotel_commons.api.schema import ERROR_ENVELOPE, envelope
from hotel_commons.api.views import BaseViewSet
from hotel_commons.consts import IDEMPOTENCY_KEY_HEADER
from hotel_commons.idempotency.decorators import idempotent
from hotel_commons.security.permissions import IsAuthenticatedPrincipal, principal_of, require_roles
from hotel_commons.security.principal import Role
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response

from catalog import container
from catalog.serializers.owner_account_serializers import OwnerAccountCreateSerializer, OwnerAccountSerializer

IDEMPOTENCY_KEY_PARAMETER = OpenApiParameter(
    IDEMPOTENCY_KEY_HEADER,
    location=OpenApiParameter.HEADER,
    required=False,
    description="Optional; repeating a request with the same key replays the first response.",
)


class OwnerAccountViewSet(BaseViewSet):
    permission_classes_by_action = {"create": [IsAuthenticatedPrincipal, require_roles(Role.OWNER, Role.ADMIN)]}
    throttle_scope_by_action = {"create": "catalog_write"}

    @extend_schema(
        operation_id="catalog_owner_accounts_create",
        tags=["catalog"],
        request=OwnerAccountCreateSerializer,
        parameters=[IDEMPOTENCY_KEY_PARAMETER],
        responses={
            201: envelope(OwnerAccountSerializer, "OwnerAccountEnvelope"),
            400: ERROR_ENVELOPE,
            401: ERROR_ENVELOPE,
            403: ERROR_ENVELOPE,
            409: ERROR_ENVELOPE,
            429: ERROR_ENVELOPE,
        },
    )
    @idempotent("catalog_write", required=False)
    def create(self, request: Request) -> Response:
        context = {"request": request}
        serializer = OwnerAccountCreateSerializer(data=request.data, context=context)
        serializer.is_valid(raise_exception=True)
        account = container.owner_account_service().open_account(principal_of(request).user_id, serializer.to_draft())
        return api_response(OwnerAccountSerializer(account, context=context).data, http_status=status.HTTP_201_CREATED)
