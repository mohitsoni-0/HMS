"""Request-scoped context on ContextVars: the request id and the caller, readable anywhere (LLD §3.3).

Middlewares bind it per request; model saves, log filters and services read it without being handed a
request. ContextVars are per thread (and per asyncio task), so concurrent requests never see each other's
values. A WSGI thread keeps its context between requests, so every ``bind`` must be undone with ``reset``
in a ``finally``.
"""

from contextvars import ContextVar, Token
from typing import Any

from hotel_commons.security.principal import ANONYMOUS, SYSTEM_ACTOR, Principal

_request_id: ContextVar[str | None] = ContextVar("hotel_commons_request_id", default=None)
_principal: ContextVar[Principal] = ContextVar("hotel_commons_principal", default=ANONYMOUS)


class RequestContext:
    """Static facade over the context variables."""

    @staticmethod
    def bind(*, request_id: str | None = None, principal: Principal | None = None) -> list[Token[Any]]:
        """Set the given values (``None`` leaves a value untouched) and return the tokens that undo this bind."""
        tokens: list[Token[Any]] = []
        if request_id is not None:
            tokens.append(_request_id.set(request_id))
        if principal is not None:
            tokens.append(_principal.set(principal))
        return tokens

    @staticmethod
    def reset(tokens: list[Token[Any]]) -> None:
        """Undo a bind: each variable returns to its value before that bind, discarding any later rebind."""
        for token in reversed(tokens):
            token.var.reset(token)

    @staticmethod
    def request_id() -> str | None:
        return _request_id.get()

    @staticmethod
    def principal() -> Principal:
        """The caller, or ``ANONYMOUS`` outside a request."""
        return _principal.get()

    @staticmethod
    def actor() -> str:
        """Who to stamp on writes: the caller's user id, or ``SYSTEM_ACTOR`` when nobody is authenticated."""
        principal = _principal.get()
        return principal.user_id if principal.is_authenticated else SYSTEM_ACTOR
