"""Sliding-window-log rate limiter on a Redis sorted set (LLD §3.10, §11).

One Lua script per hit: drop entries older than the window, count, and record the hit only when it is
allowed, so a client hammering a closed window does not keep it closed. Time comes from Redis ``TIME``
inside the script, so every app instance shares one clock and skewed hosts cannot widen the window.
fakeredis (with Lua) supports ``TIME`` in scripts, so tests run the same script.

When Redis is unreachable the policy decides: ``fail_open`` lets the request through (logged as a
WARNING), otherwise it is refused with a short retry, so a payment is never charged unthrottled.
"""

import logging
import time
from functools import lru_cache
from typing import Any, Final
from uuid import uuid4

from redis.exceptions import RedisError

from hotel_commons.ratelimit.interfaces import RateLimitDecision, RateLimiter, RateLimitPolicy
from hotel_commons.redis.client import get_redis, redis_key

logger = logging.getLogger(__name__)

FAIL_CLOSED_RETRY_AFTER_SECONDS: Final = 5

# KEYS[1] = key; ARGV[1] = limit; ARGV[2] = window seconds; ARGV[3] = unique member.
# Returns {allowed (1|0), remaining, retry_after_seconds}.
SLIDING_WINDOW_SCRIPT: Final = """
local limit = tonumber(ARGV[1])
local window_us = tonumber(ARGV[2]) * 1000000
local t = redis.call('TIME')
local now_us = tonumber(t[1]) * 1000000 + tonumber(t[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now_us - window_us)
local count = redis.call('ZCARD', KEYS[1])
if count < limit then
  redis.call('ZADD', KEYS[1], now_us, ARGV[3])
  redis.call('PEXPIRE', KEYS[1], math.ceil(window_us / 1000))
  return {1, limit - count - 1, 0}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {0, 0, math.ceil((tonumber(oldest[2]) + window_us - now_us) / 1000000)}
"""


class RedisSlidingWindowRateLimiter(RateLimiter):
    def __init__(self, redis_client: Any) -> None:
        self._script = redis_client.register_script(SLIDING_WINDOW_SCRIPT)

    def hit(self, subject: str, policy: RateLimitPolicy) -> RateLimitDecision:
        key = redis_key("rl", "v1", policy.scope, subject)
        member = f"{time.time_ns()}-{uuid4().hex[:8]}"
        try:
            allowed, remaining, retry_after = self._script(
                keys=[key], args=[policy.limit, policy.window_seconds, member]
            )
        except RedisError:
            return _unavailable(policy)
        if int(allowed):
            return RateLimitDecision(allowed=True, limit=policy.limit, remaining=int(remaining), retry_after_seconds=0)
        return RateLimitDecision(
            allowed=False, limit=policy.limit, remaining=0, retry_after_seconds=max(1, int(retry_after))
        )


def _unavailable(policy: RateLimitPolicy) -> RateLimitDecision:
    """The decision when Redis cannot be reached: allow (as if the window were empty) or refuse, per policy."""
    if policy.fail_open:
        logger.warning("Rate limiter unavailable; allowing scope %s (fail open)", policy.scope)
        return RateLimitDecision(allowed=True, limit=policy.limit, remaining=policy.limit - 1, retry_after_seconds=0)
    logger.warning("Rate limiter unavailable; refusing scope %s (fail closed)", policy.scope)
    return RateLimitDecision(
        allowed=False, limit=policy.limit, remaining=0, retry_after_seconds=FAIL_CLOSED_RETRY_AFTER_SECONDS
    )


@lru_cache(maxsize=1)
def get_rate_limiter() -> RateLimiter:
    """The process-wide limiter over ``get_redis()`` (``cache_clear()`` after changing REDIS_URL)."""
    return RedisSlidingWindowRateLimiter(get_redis())
