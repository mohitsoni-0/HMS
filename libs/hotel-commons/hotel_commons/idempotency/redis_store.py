"""Idempotency records in Redis (LLD §3.11, §11), encrypted at rest.

One string per key holding ``{"state", "fingerprint", "http_status", "body"}``. The body is a stored API
response and may contain guest PII, so the record is sealed with the PII cipher under the associated data
``"idem"`` and Redis only ever holds ``ENC_...`` ciphertext.

- begin: ``SET key <in progress> NX EX lock_ttl``; when the key exists, ``GET`` it and compare.
- complete: ``SET key <completed> EX ttl``.
- release: ``DEL key``.

RedisError propagates: the decorator decides what an unreachable store means.
"""

import json
from functools import lru_cache
from typing import Any, Final

from rest_framework.utils.encoders import JSONEncoder

from hotel_commons.idempotency.interfaces import BeginOutcome, IdempotencyStore, StoredResponse
from hotel_commons.redis.client import get_redis
from hotel_commons.security.pii import PiiCipher, get_pii_cipher

RECORD_AAD: Final = "idem"
STATE_IN_PROGRESS: Final = "in_progress"
STATE_COMPLETED: Final = "completed"
# SET NX can lose to a key that expires before the GET; one more claim settles it.
_CLAIM_ATTEMPTS: Final = 2


class RedisIdempotencyStore(IdempotencyStore):
    def __init__(self, redis_client: Any, cipher: PiiCipher) -> None:
        self._redis = redis_client
        self._cipher = cipher

    def begin(self, key: str, fingerprint: str, lock_ttl_seconds: int) -> BeginOutcome:
        claim = self._seal(_record(STATE_IN_PROGRESS, fingerprint))
        for _ in range(_CLAIM_ATTEMPTS):
            if self._redis.set(key, claim, nx=True, ex=lock_ttl_seconds):
                return BeginOutcome(kind="started")
            stored = self._redis.get(key)
            if stored is not None:
                return _outcome(self._open(stored), fingerprint)
        return BeginOutcome(kind="in_progress")

    def complete(self, key: str, fingerprint: str, response: StoredResponse, ttl_seconds: int) -> None:
        record = _record(STATE_COMPLETED, fingerprint, response)
        self._redis.set(key, self._seal(record), ex=ttl_seconds)

    def release(self, key: str) -> None:
        self._redis.delete(key)

    def _seal(self, record: dict[str, Any]) -> str:
        return self._cipher.encrypt(json.dumps(record, cls=JSONEncoder), aad=RECORD_AAD)

    def _open(self, stored: str) -> dict[str, Any]:
        return json.loads(self._cipher.decrypt(stored, aad=RECORD_AAD))


def _record(state: str, fingerprint: str, response: StoredResponse | None = None) -> dict[str, Any]:
    return {
        "state": state,
        "fingerprint": fingerprint,
        "http_status": response.http_status if response else None,
        "body": response.body if response else None,
    }


def _outcome(record: dict[str, Any], fingerprint: str) -> BeginOutcome:
    if record["fingerprint"] != fingerprint:
        return BeginOutcome(kind="mismatch")
    if record["state"] != STATE_COMPLETED:
        return BeginOutcome(kind="in_progress")
    return BeginOutcome(kind="replay", response=StoredResponse(http_status=record["http_status"], body=record["body"]))


@lru_cache(maxsize=1)
def get_idempotency_store() -> IdempotencyStore:
    """The process-wide store over ``get_redis()`` and the PII cipher (``cache_clear()`` after a change)."""
    return RedisIdempotencyStore(get_redis(), get_pii_cipher())
