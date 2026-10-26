"""Idempotency store contract (LLD §3.11). Pure Python.

A key moves from absent to in progress (``begin``) to completed (``complete``) or back to absent
(``release``, after a server error). ``begin`` on a live key reports what is there: the stored response
to replay, a request still in progress, or a different request reusing the key.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

BeginKind = Literal["started", "in_progress", "replay", "mismatch"]


@dataclass(frozen=True, slots=True)
class StoredResponse:
    http_status: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BeginOutcome:
    kind: BeginKind
    response: StoredResponse | None = None  # set for "replay" only


class IdempotencyStore(ABC):
    @abstractmethod
    def begin(self, key: str, fingerprint: str, lock_ttl_seconds: int) -> BeginOutcome:
        """Claim ``key`` for a request with ``fingerprint`` (the claim expires after ``lock_ttl_seconds``)."""

    @abstractmethod
    def complete(self, key: str, fingerprint: str, response: StoredResponse, ttl_seconds: int) -> None:
        """Store the final response under ``key`` for ``ttl_seconds``."""

    @abstractmethod
    def release(self, key: str) -> None:
        """Forget ``key`` so the client may retry."""
