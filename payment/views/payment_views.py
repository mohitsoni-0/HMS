"""``GET /hotel/v1/payments/{payment_id}`` (LLD §10.2 row 16).

GUEST or ADMIN only. The IDOR rule lets a guest read only payments they made; anyone else gets the same 404
as for a payment that does not exist.
"""

from uuid import UUID

from drf_spectacular.utils import extend_schema
from hotel_commons.api.response import api_response
from hotel_commons.api.schema import ERROR_ENVELOPE, envelope
from hotel_commons.api.views import BaseViewSet
from hotel_commons.security.idor import IdorRule
from hotel_commons.security.permissions import require_roles
from hotel_commons.security.principal import Role
from rest_framework.request import Request
from rest_framework.response import Response

from payment.public.api import get_payment_api
from payment.serializers.payment_serializers import PaymentSerializer


class PaymentViewSet(BaseViewSet):
    permission_classes_by_action = {"retrieve": [require_roles(Role.GUEST, Role.ADMIN)]}
    idor_rules = {"retrieve": (IdorRule("payment", "payment_id", "path", frozenset({"guest"})),)}

    @extend_schema(
        operation_id="payments_retrieve",
        responses={
            200: envelope(PaymentSerializer, "PaymentEnvelope"),
            401: ERROR_ENVELOPE,
            403: ERROR_ENVELOPE,
            404: ERROR_ENVELOPE,
            429: ERROR_ENVELOPE,
        },
    )
    def retrieve(self, request: Request, payment_id: UUID) -> Response:
        payment = get_payment_api().get_payment(payment_id)
        return api_response(PaymentSerializer(payment, context={"request": request}).data)
