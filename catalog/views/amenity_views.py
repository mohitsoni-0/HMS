"""``GET /catalog/amenities`` (LLD §10.2 row 4): public."""

from drf_spectacular.utils import extend_schema
from hotel_commons.api.response import api_response
from hotel_commons.api.schema import ERROR_ENVELOPE, envelope
from hotel_commons.api.views import BaseViewSet
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from catalog import container
from catalog.serializers.property_serializers import AmenitySerializer


class AmenityViewSet(BaseViewSet):
    permission_classes_by_action = {"list": [AllowAny]}

    @extend_schema(
        operation_id="catalog_amenities_list",
        tags=["catalog"],
        auth=[],
        responses={200: envelope(AmenitySerializer(many=True), "AmenityListEnvelope"), 429: ERROR_ENVELOPE},
    )
    def list(self, request: Request) -> Response:
        amenities = container.query_service().list_amenities()
        return api_response(AmenitySerializer(amenities, many=True, context={"request": request}).data)
