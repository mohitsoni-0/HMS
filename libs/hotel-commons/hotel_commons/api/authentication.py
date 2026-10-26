"""DRF authentication over the JWT middleware (LLD §3.9).

``JWTAuthenticationMiddleware`` has already verified the bearer token and set ``request.principal``; this
class only hands that principal to DRF as ``request.user``. An anonymous call returns None, so DRF answers
a failed permission check with 401 and the ``WWW-Authenticate: Bearer`` challenge.
"""

from typing import Any

from rest_framework.authentication import BaseAuthentication
from rest_framework.request import Request

from hotel_commons.security.principal import ANONYMOUS, Principal


class PrincipalAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> tuple[Principal, None] | None:
        principal = getattr(request._request, "principal", ANONYMOUS)
        return (principal, None) if principal.is_authenticated else None

    def authenticate_header(self, request: Any) -> str:
        return "Bearer"
