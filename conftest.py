"""Repository-wide pytest fixtures: a DRF API client, bearer-token headers and per-test Redis isolation.

Django, DRF and hotel_commons are imported inside the fixtures, so this file also loads in pure unit
runs that never configure Django (``-p no:django``).
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from rest_framework.test import APIClient


@pytest.fixture
def api_client() -> "APIClient":
    """A DRF test client. The test settings make JSON its default request format."""
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def auth() -> Callable[..., dict[str, str]]:
    """Build request kwargs carrying a signed bearer token, e.g. ``api_client.get(url, **auth("guest-1", "GUEST"))``."""
    from hotel_commons.testing.tokens import make_token

    def bearer_headers(user_id: str, *roles: str) -> dict[str, str]:
        return {"HTTP_AUTHORIZATION": f"Bearer {make_token(user_id, *roles)}"}

    return bearer_headers


@pytest.fixture(autouse=True)
def reset_fake_redis() -> None:
    """Start every test with an empty fake Redis, so rate-limit windows and idempotency keys never leak between tests.

    Only the in-process fake is ever flushed, never a real server. Failing to obtain a client is ignored
    on purpose: pure unit runs have no Django settings or Redis client, and tests that need Redis fail
    on their own.
    """
    try:
        import fakeredis
        from hotel_commons.redis.client import get_redis

        client = get_redis()
    except Exception:  # noqa: BLE001 - no client in this run, see the docstring
        return
    if isinstance(client, (fakeredis.FakeRedis, fakeredis.FakeStrictRedis)):
        client.flushall()
