"""DRF exception handler (``REST_FRAMEWORK["EXCEPTION_HANDLER"]``) answering every client error with the
error envelope (LLD §3.9).

``AppError`` keeps its own StatusCode; DRF and Django exceptions map to the common codes. Anything else
returns ``None``, so DRF re-raises it and ``ExceptionHandlingMiddleware`` answers 500 INTERNAL_ERROR.
"""

import logging
import math
from collections.abc import Mapping
from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.settings import api_settings

from hotel_commons.api.response import error_body
from hotel_commons.consts import RETRY_AFTER_HEADER
from hotel_commons.exceptions import AppError
from hotel_commons.status_code import (
    FORBIDDEN,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    RATE_LIMITED,
    UNAUTHENTICATED,
    VALIDATION_FAILED,
    StatusCode,
)

logger = logging.getLogger(__name__)


def envelope_exception_handler(exc: Exception, context: Mapping[str, Any]) -> Response | None:
    """The envelope Response for ``exc``, or None to let the exception reach the middleware (500)."""
    if isinstance(exc, AppError):
        return _app_error(exc)
    if isinstance(exc, exceptions.ValidationError):
        return _envelope(VALIDATION_FAILED, errors=_field_errors(exc.detail))
    if isinstance(exc, exceptions.NotAuthenticated | exceptions.AuthenticationFailed):
        auth_header = getattr(exc, "auth_header", None)
        return _envelope(UNAUTHENTICATED, headers={"WWW-Authenticate": auth_header} if auth_header else None)
    if isinstance(exc, exceptions.PermissionDenied | DjangoPermissionDenied):
        return _envelope(FORBIDDEN)
    if isinstance(exc, exceptions.NotFound | Http404):
        return _envelope(NOT_FOUND)
    if isinstance(exc, exceptions.Throttled):
        retry_after = max(1, math.ceil(exc.wait or 0))
        return _envelope(RATE_LIMITED, message=RATE_LIMITED.render(retry_after=retry_after),
                         headers={RETRY_AFTER_HEADER: str(retry_after)})
    if isinstance(exc, exceptions.MethodNotAllowed):
        method = getattr(context.get("request"), "method", "")
        return _envelope(METHOD_NOT_ALLOWED, message=METHOD_NOT_ALLOWED.render(method=method))
    if isinstance(exc, exceptions.APIException) and exc.status_code < 500:
        # ParseError (malformed JSON), UnsupportedMediaType, NotAcceptable: client errors, never a 500.
        return _envelope(VALIDATION_FAILED, message=str(exc.detail), http_status=exc.status_code)
    return None


def _app_error(exc: AppError) -> Response:
    if exc.http_status >= 500:
        logger.error("Request failed with status code %s", exc.status_code.code, exc_info=exc)
    retry_after = getattr(exc, "retry_after_seconds", None)
    headers = {RETRY_AFTER_HEADER: str(retry_after)} if retry_after is not None else None
    return Response(error_body(exc.status_code, exc.message, exc.errors), status=exc.http_status, headers=headers)


def _envelope(
    status: StatusCode,
    *,
    message: str | None = None,
    errors: Mapping[str, Any] | None = None,
    http_status: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    body = error_body(status, message or status.message, errors)
    return Response(body, status=http_status or status.http_status, headers=headers)


def _field_errors(detail: Any) -> Mapping[str, Any]:
    """Serializer errors are already keyed by field; a bare list comes from ``validate()`` or a view."""
    return detail if isinstance(detail, Mapping) else {api_settings.NON_FIELD_ERRORS_KEY: detail}
