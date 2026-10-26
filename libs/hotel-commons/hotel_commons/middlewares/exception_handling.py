"""Last-resort error envelope (LLD §3.8 row 3).

DRF views answer their own errors through ``envelope_exception_handler``; this middleware catches what
escapes a view: an ``AppError`` from a plain Django view, a lock or statement timeout (409 RESOURCE_BUSY /
503 SERVICE_TIMEOUT with Retry-After, logged as a warning), or any unexpected exception, which is logged and
answered with a 500 INTERNAL_ERROR that reveals nothing. It answers with a ``JsonResponse``, never a DRF
``Response`` (there is no renderer outside DRF). Django's own client-error exceptions (Http404,
PermissionDenied, BadRequest, SuspiciousOperation) are left to Django, which answers them through the
project's ``handler404`` and friends.
"""

import logging
from collections.abc import Callable
from typing import Final

from django.core.exceptions import BadRequest, PermissionDenied, SuspiciousOperation
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse

from hotel_commons.api.exception_handler import log_app_error, retry_after_headers
from hotel_commons.api.response import error_body
from hotel_commons.db.errors import retryable_database_error
from hotel_commons.exceptions import AppError
from hotel_commons.status_code import INTERNAL_ERROR

logger = logging.getLogger(__name__)

DJANGO_CLIENT_ERRORS: Final = (Http404, PermissionDenied, BadRequest, SuspiciousOperation)


class ExceptionHandlingMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_exception(self, request: HttpRequest, exception: Exception) -> JsonResponse | None:
        if isinstance(exception, AppError):
            return _app_error_response(exception)
        retryable = retryable_database_error(exception)
        if retryable is not None:
            return _app_error_response(retryable)
        if isinstance(exception, DJANGO_CLIENT_ERRORS):
            return None
        logger.error("Unhandled exception on %s %s", request.method, request.path, exc_info=exception)
        return JsonResponse(error_body(INTERNAL_ERROR, INTERNAL_ERROR.message), status=INTERNAL_ERROR.http_status)


def _app_error_response(exc: AppError) -> JsonResponse:
    log_app_error(exc)
    return JsonResponse(error_body(exc.status_code, exc.message, exc.errors), status=exc.http_status,
                        headers=retry_after_headers(exc))
