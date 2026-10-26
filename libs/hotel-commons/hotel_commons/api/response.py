"""The response envelope (LLD §3.9, HLD §12).

Success: ``{"status": 2100000, "message": "Success", "data": {...}, "request_id": "..."}``.
Error: ``{"status": <code>, "message": "...", "errors": {...} | null, "data": null, "request_id": "..."}``.
The HTTP status is always the real one; ``status`` is the service's ``21MMEEE`` code.

``errors`` always has one shape, whoever raised the error (a serializer or a domain rule): a dotted field
path to a list of messages, e.g. ``{"card.card_number": ["is invalid"], "room_types.0.base_price": [...]}``.
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
        "errors": flatten_errors(errors) if errors is not None else None,
        "data": data,
        "request_id": RequestContext.request_id(),
    }


def flatten_errors(errors: Mapping[str, Any]) -> dict[str, list[str]]:
    """Field errors as ``{"dotted.path": ["message", ...]}``.

    Nested serializers become dotted keys and list items their index (``room_types.0.base_price``); a bare
    message becomes a one-item list, so a client can always map ``errors[path]`` onto an input.
    """
    flat: dict[str, list[str]] = {}
    for name, value in errors.items():
        _collect(str(name), value, flat)
    return flat


def _collect(path: str, value: Any, flat: dict[str, list[str]]) -> None:
    if isinstance(value, Mapping):
        for name, nested in value.items():
            _collect(f"{path}.{name}", nested, flat)
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            if isinstance(item, Mapping | list | tuple):
                _collect(f"{path}.{index}", item, flat)  # one entry per list item; valid items are empty
            else:
                flat.setdefault(path, []).append(str(item))
        return
    if value is not None:
        flat.setdefault(path, []).append(str(value))
