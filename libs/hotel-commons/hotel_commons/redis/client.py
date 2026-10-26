"""The process's Redis client and the library's key naming (LLD §3.13, §11).

``REDIS_URL=fakeredis://`` (tests) gives one in-process FakeRedis shared by the whole process: separate
FakeRedis instances do not share data, so the rate limiter, idempotency store and replay guard must all
see the same one. Any other URL gives a redis-py client with short timeouts, so a slow or absent Redis
fails fast and the callers' fail-open/fail-closed policies decide what happens.
"""

import threading
from functools import cache, lru_cache

from django.conf import settings
from redis import Redis

from hotel_commons.consts import REDIS_KEY_PREFIX

FAKE_REDIS_SCHEME = "fakeredis://"
SOCKET_TIMEOUT_SECONDS = 0.5

_fake_redis_lock = threading.Lock()


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    """Client for ``settings.REDIS_URL``; cached, so call ``get_redis.cache_clear()`` after changing the URL."""
    return redis_from_url(settings.REDIS_URL)


def redis_from_url(url: str) -> Redis:
    """A client for ``url``; no connection is opened until the first command."""
    if url.startswith(FAKE_REDIS_SCHEME):
        with _fake_redis_lock:  # serialises the first call, so exactly one FakeRedis is ever built
            return _process_fake_redis()
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
    )


@cache
def _process_fake_redis() -> Redis:
    import fakeredis  # test dependency only; production images never import it

    return fakeredis.FakeRedis(decode_responses=True)


def redis_key(*parts: str | int) -> str:
    """A key under the library prefix: ``redis_key("rl", "v1", scope, subject) == "hb:rl:v1:<scope>:<subject>"``."""
    if not parts:
        raise ValueError("redis_key() needs at least one part")
    return ":".join((REDIS_KEY_PREFIX, *map(str, parts)))
