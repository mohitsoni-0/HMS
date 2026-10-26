"""Bearer-token authentication (LLD §3.8 row 5).

``Authorization: Bearer <jwt>`` becomes ``request.principal`` and is bound into ``RequestContext`` for the
rest of the request; no header means ANONYMOUS. A header that is present but unusable (another scheme, an
empty, forged or expired token) is answered right here with a 401 envelope, never raised, so no view runs
for a caller who presented bad credentials.
"""

from collections.abc import Callable
from typing import Final

from django.http import HttpRequest, HttpResponse, JsonResponse

from hotel_commons.api.response import error_body
from hotel_commons.context import RequestContext
from hotel_commons.exceptions import Unauthenticated
from hotel_commons.security.jwt_tokens import get_token_service
from hotel_commons.security.principal import ANONYMOUS, Principal
from hotel_commons.status_code import UNAUTHENTICATED

BEARER_SCHEME: Final = "bearer"
WWW_AUTHENTICATE_HEADER: Final = "WWW-Authenticate"


class JWTAuthenticationMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            principal = _principal(request.headers.get("Authorization"))
        except Unauthenticated as exc:
            return _unauthenticated(exc)
        request.principal = principal
        tokens = RequestContext.bind(principal=principal)
        try:
            return self.get_response(request)
        finally:
            RequestContext.reset(tokens)


def _principal(authorization: str | None) -> Principal:
    if authorization is None:
        return ANONYMOUS
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != BEARER_SCHEME or not token:
        raise Unauthenticated(UNAUTHENTICATED)
    return get_token_service().verify(token)


def _unauthenticated(exc: Unauthenticated) -> JsonResponse:
    return JsonResponse(
        error_body(exc.status_code, exc.message, exc.errors),
        status=exc.http_status,
        headers={WWW_AUTHENTICATE_HEADER: "Bearer"},
    )
