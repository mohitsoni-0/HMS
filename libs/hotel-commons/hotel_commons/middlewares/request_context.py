"""Request id and request context (LLD §3.8 row 2).

Takes a safe ``X-Request-ID`` from the client (up to 64 of ``[A-Za-z0-9-]``) or mints a uuid4 hex, puts it
on ``request.request_id``, binds it with the ANONYMOUS principal (authentication rebinds the caller later),
echoes it on the response, and always resets the context, since WSGI threads are reused across requests.
"""

import re
import uuid
from collections.abc import Callable
from typing import Final

from django.http import HttpRequest, HttpResponse

from hotel_commons.consts import REQUEST_ID_HEADER
from hotel_commons.context import RequestContext
from hotel_commons.security.principal import ANONYMOUS

REQUEST_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9-]{1,64}")


class RequestContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = _request_id(request)
        request.request_id = request_id
        tokens = RequestContext.bind(request_id=request_id, principal=ANONYMOUS)
        try:
            response = self.get_response(request)
            response[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            RequestContext.reset(tokens)


def _request_id(request: HttpRequest) -> str:
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    if REQUEST_ID_PATTERN.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex
