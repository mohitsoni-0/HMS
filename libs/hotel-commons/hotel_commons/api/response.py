"""The response envelope (LLD §3.9, HLD §12).

Success: ``{"status": 2100000, "message": "Success", "data": {...}, "request_id": "..."}``.
Error: ``{"status": <code>, "message": "...", "errors": {...} | null, "data": null, "request_id": "..."}``.
The HTTP status is always the real one; ``status`` is the service's ``21MMEEE`` code.
"""

from collections.abc import Mapping
from typing import Any

from rest_framework.response import Response

from hotel_commons.context import RequestContext
from hotel_commons.status_code import SUCCESS, StatusCode


def api_response(
    data: Any = None,
    *,
    http_status: int = 200,
    status: StatusCode = SUCCESS,
    message: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """A DRF Response carrying ``data`` in the envelope; also used for outcomes such as 402 and 409 with data."""
    body = {
        "status": status.code,
        "message": message or status.message,
        "data": data,
        "request_id": RequestContext.request_id(),
    }
    return Response(body, status=http_status, headers=headers)


def error_body(
    status: StatusCode, message: str, errors: Mapping[str, Any] | None = None, data: Any = None
) -> dict[str, Any]:
    """The error envelope as a plain dict, for DRF responses and the middlewares' JsonResponses alike."""
    return {
        "status": status.code,
        "message": message,
        "errors": errors,
        "data": data,
        "request_id": RequestContext.request_id(),
    }
