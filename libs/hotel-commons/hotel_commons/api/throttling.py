"""DRF throttle over the Redis sliding-window limiter (LLD §3.10).

The scope comes from ``view.get_throttle_scope()`` (BaseViewSet, per action), else the view's
``throttle_scope``, else ``"default"``; an unknown scope uses the default policy. Authenticated callers are
limited per user, anonymous ones per client IP. The decision is left on ``request.rate_limit_decision`` so
``BaseViewSet`` can report it in the X-RateLimit-* headers.
"""

from typing import Any, Final

from django.conf import settings
from rest_framework.request import Request
from rest_framework.throttling import BaseThrottle

from hotel_commons.ratelimit.interfaces import RateLimitDecision, RateLimitPolicy
from hotel_commons.ratelimit.redis_sliding_window import get_rate_limiter
from hotel_commons.security.client_ip import client_ip
from hotel_commons.security.permissions import principal_of

DEFAULT_SCOPE: Final = "default"


class RedisRateLimitThrottle(BaseThrottle):
    def __init__(self) -> None:
        self._decision: RateLimitDecision | None = None

    def allow_request(self, request: Request, view: Any) -> bool:
        if not settings.RATE_LIMIT_ENABLED:
            return True
        decision = get_rate_limiter().hit(_subject(request), rate_limit_policy(_scope(view)))
        request.rate_limit_decision = decision
        self._decision = decision
        return decision.allowed

    def wait(self) -> float | None:
        if self._decision is None or self._decision.allowed:
            return None
        return self._decision.retry_after_seconds


def rate_limit_policy(scope: str) -> RateLimitPolicy:
    """The ``RATE_LIMIT_POLICIES`` entry for ``scope``, falling back to the ``default`` policy."""
    policies = settings.RATE_LIMIT_POLICIES
    if scope not in policies:
        scope = DEFAULT_SCOPE
    entry = policies[scope]
    return RateLimitPolicy(
        scope=scope,
        limit=entry["limit"],
        window_seconds=entry["window_seconds"],
        fail_open=entry.get("fail_open", True),
    )


def _scope(view: Any) -> str:
    if hasattr(view, "get_throttle_scope"):
        return view.get_throttle_scope()
    return getattr(view, "throttle_scope", None) or DEFAULT_SCOPE


def _subject(request: Request) -> str:
    principal = principal_of(request)
    if principal.is_authenticated:
        return f"u:{principal.user_id}"
    return f"ip:{client_ip(request)}"
