"""Sliding-window rate limiter over Redis (LLD §3.10).

Pure: the limiter is handed a FakeRedis (with Lua) directly. The window is slid by moving the recorded hits
back in time, so no test sleeps.
"""

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from hotel_commons.ratelimit.interfaces import RateLimitDecision, RateLimiter, RateLimitPolicy
from hotel_commons.ratelimit.redis_sliding_window import RedisSlidingWindowRateLimiter

PAYMENT = RateLimitPolicy(scope="payment", limit=3, window_seconds=60, fail_open=False)
SEARCH = RateLimitPolicy(scope="search", limit=3, window_seconds=60, fail_open=True)
MICROS = 1_000_000


@pytest.fixture
def redis_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def limiter(redis_client) -> RedisSlidingWindowRateLimiter:
    return RedisSlidingWindowRateLimiter(redis_client)


class _BrokenScript:
    def __call__(self, *args, **kwargs):
        raise RedisConnectionError("redis is down")


class _DownRedis:
    def register_script(self, script: str) -> _BrokenScript:
        return _BrokenScript()


def _age_hits(redis_client: fakeredis.FakeRedis, key: str, seconds: int) -> None:
    """Move every recorded hit ``seconds`` into the past, as if that much time had passed."""
    for member, score in redis_client.zrange(key, 0, -1, withscores=True):
        redis_client.zadd(key, {member: score - seconds * MICROS})


def test_limiter_is_a_rate_limiter(limiter):
    assert isinstance(limiter, RateLimiter)


def test_allows_up_to_the_limit_then_denies_with_retry_after(limiter):
    decisions = [limiter.hit("u:guest-1", PAYMENT) for _ in range(3)]
    denied = limiter.hit("u:guest-1", PAYMENT)

    assert [(d.allowed, d.remaining) for d in decisions] == [(True, 2), (True, 1), (True, 0)]
    assert all(d.limit == 3 for d in decisions)
    assert (denied.allowed, denied.remaining) == (False, 0)
    assert 59 <= denied.retry_after_seconds <= 60


def test_denied_hits_are_not_recorded(limiter, redis_client):
    for _ in range(5):
        limiter.hit("u:guest-1", PAYMENT)

    assert redis_client.zcard("hb:rl:v1:payment:u:guest-1") == 3


def test_window_slides(limiter, redis_client):
    for _ in range(3):
        limiter.hit("u:guest-1", PAYMENT)
    _age_hits(redis_client, "hb:rl:v1:payment:u:guest-1", 45)

    still_denied = limiter.hit("u:guest-1", PAYMENT)
    _age_hits(redis_client, "hb:rl:v1:payment:u:guest-1", 16)
    allowed_again = limiter.hit("u:guest-1", PAYMENT)

    assert not still_denied.allowed and 14 <= still_denied.retry_after_seconds <= 15
    assert allowed_again == RateLimitDecision(allowed=True, limit=3, remaining=2, retry_after_seconds=0)


def test_buckets_are_per_subject_and_scope(limiter):
    for _ in range(3):
        limiter.hit("u:guest-1", PAYMENT)

    assert limiter.hit("u:guest-2", PAYMENT).allowed
    assert limiter.hit("u:guest-1", SEARCH).allowed


def test_key_expires_with_the_window(limiter, redis_client):
    limiter.hit("ip:10.0.0.1", SEARCH)

    assert 0 < redis_client.pttl("hb:rl:v1:search:ip:10.0.0.1") <= 60_000


def test_fail_closed_policy_denies_on_redis_error():
    decision = RedisSlidingWindowRateLimiter(_DownRedis()).hit("u:guest-1", PAYMENT)

    assert decision == RateLimitDecision(allowed=False, limit=3, remaining=0, retry_after_seconds=5)


def test_fail_open_policy_allows_on_redis_error(caplog):
    decision = RedisSlidingWindowRateLimiter(_DownRedis()).hit("u:guest-1", SEARCH)

    assert decision.allowed and decision.retry_after_seconds == 0
    assert any(record.levelname == "WARNING" for record in caplog.records)
