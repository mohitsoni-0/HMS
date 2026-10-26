"""In-memory ``IdempotencyStore`` and ``RateLimiter`` for tests (LLD §3.10, §3.11).

Import approach: the interfaces live in ``hotel_commons.idempotency.interfaces`` and
``hotel_commons.ratelimit.interfaces``, which belong to the security track and may not exist yet when this
module is imported. So the two classes are not defined at import time. A module-level ``__getattr__``
(PEP 562) builds each one on first access, as a real subclass of its ABC, and caches it.
``from hotel_commons.testing.stores import InMemoryRateLimiter`` therefore works like any import, and only
that access needs the interface module.

Both fakes mirror their Redis implementations: the same outcomes, TTLs and sliding window. Time comes
from an injectable monotonic source, so tests can expire locks and slide windows without sleeping.
"""

import copy
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

__all__ = ["InMemoryIdempotencyStore", "InMemoryRateLimiter"]

TimeSource = Callable[[], float]


@dataclass(slots=True)
class _IdempotencyRecord:
    fingerprint: str
    expires_at: float
    response: Any = None  # the StoredResponse once completed; None while in progress


@cache
def _idempotency_store_class() -> type:
    from hotel_commons.idempotency.interfaces import BeginOutcome, IdempotencyStore, StoredResponse

    class InMemoryIdempotencyStore(IdempotencyStore):
        """Dict-backed store: in-progress records expire after the lock TTL, completed ones after their TTL."""

        def __init__(self, now: TimeSource = time.monotonic) -> None:
            self._now = now
            self._records: dict[str, _IdempotencyRecord] = {}
            self._lock = threading.Lock()

        def begin(self, key: str, fingerprint: str, lock_ttl_seconds: int) -> BeginOutcome:
            with self._lock:
                record = self._live_record(key)
                if record is None:
                    self._records[key] = _IdempotencyRecord(fingerprint, self._now() + lock_ttl_seconds)
                    return BeginOutcome(kind="started")
                if record.fingerprint != fingerprint:
                    return BeginOutcome(kind="mismatch")
                if record.response is None:
                    return BeginOutcome(kind="in_progress")
                return BeginOutcome(kind="replay", response=copy.deepcopy(record.response))

        def complete(self, key: str, fingerprint: str, response: StoredResponse, ttl_seconds: int) -> None:
            with self._lock:
                self._records[key] = _IdempotencyRecord(fingerprint, self._now() + ttl_seconds, copy.deepcopy(response))

        def release(self, key: str) -> None:
            with self._lock:
                self._records.pop(key, None)

        def _live_record(self, key: str) -> _IdempotencyRecord | None:
            record = self._records.get(key)
            if record is not None and record.expires_at <= self._now():
                del self._records[key]
                return None
            return record

    InMemoryIdempotencyStore.__qualname__ = "InMemoryIdempotencyStore"
    return InMemoryIdempotencyStore


@cache
def _rate_limiter_class() -> type:
    from hotel_commons.ratelimit.interfaces import RateLimitDecision, RateLimiter, RateLimitPolicy

    class InMemoryRateLimiter(RateLimiter):
        """Sliding-window log per (scope, subject), the same algorithm as the Redis Lua script."""

        def __init__(self, now: TimeSource = time.monotonic) -> None:
            self._now = now
            self._hits: dict[str, deque[float]] = {}
            self._lock = threading.Lock()

        def hit(self, subject: str, policy: RateLimitPolicy) -> RateLimitDecision:
            with self._lock:
                now = self._now()
                hits = self._hits.setdefault(f"{policy.scope}:{subject}", deque())
                while hits and hits[0] <= now - policy.window_seconds:
                    hits.popleft()
                if len(hits) < policy.limit:
                    hits.append(now)
                    return RateLimitDecision(
                        allowed=True, limit=policy.limit, remaining=policy.limit - len(hits), retry_after_seconds=0
                    )
                retry_after = math.ceil(hits[0] + policy.window_seconds - now) if hits else policy.window_seconds
                return RateLimitDecision(
                    allowed=False, limit=policy.limit, remaining=0, retry_after_seconds=retry_after
                )

    InMemoryRateLimiter.__qualname__ = "InMemoryRateLimiter"
    return InMemoryRateLimiter


_LAZY_CLASSES: dict[str, Callable[[], type]] = {
    "InMemoryIdempotencyStore": _idempotency_store_class,
    "InMemoryRateLimiter": _rate_limiter_class,
}


def __getattr__(name: str) -> type:
    factory = _LAZY_CLASSES.get(name)
    if factory is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return factory()
