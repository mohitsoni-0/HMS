"""``GET /hotel/v1/discovery/search`` (LLD §10.2 row 10): public, rate limited under the ``search`` scope."""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from discovery import container
from discovery.serializers.search_serializers import SearchPageSerializer, SearchQuerySerializer
from hotel_commons.api.response import api_response
from hotel_commons.api.schema import ERROR_ENVELOPE, envelope
from hotel_commons.api.views import BaseViewSet


class SearchViewSet(BaseViewSet):
    permission_classes_by_action = {"search": (AllowAny,)}
    throttle_scope_by_action = {"search": "search"}

    @extend_schema(
        operation_id="discovery_search",
        summary="Search available properties for a stay",
        parameters=[SearchQuerySerializer],
        responses={
            200: envelope(SearchPageSerializer, "SearchPageEnvelope"),
            400: ERROR_ENVELOPE,
            429: ERROR_ENVELOPE,
        },
    )
    def search(self, request: Request) -> Response:
        query = SearchQuerySerializer(data=request.query_params, context={"request": request})
        query.is_valid(raise_exception=True)
        page = container.search_service().search(query.to_criteria())
        return api_response(SearchPageSerializer(page, context={"request": request}).data)
