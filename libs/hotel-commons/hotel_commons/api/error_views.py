"""Django's 404 and 500 handlers answering with the error envelope (LLD §3.8).

The project's ``urls.py`` sets ``handler404`` and ``handler500`` to these, so an unknown path (or a
malformed ``<uuid:...>`` path id) gets the same JSON shape as every other error, never an HTML page.
"""

from django.http import HttpRequest, JsonResponse

from hotel_commons.api.response import error_body
from hotel_commons.status_code import INTERNAL_ERROR, NOT_FOUND


def not_found(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return JsonResponse(error_body(NOT_FOUND, NOT_FOUND.message), status=NOT_FOUND.http_status)


def server_error(request: HttpRequest) -> JsonResponse:
    return JsonResponse(error_body(INTERNAL_ERROR, INTERNAL_ERROR.message), status=INTERNAL_ERROR.http_status)
