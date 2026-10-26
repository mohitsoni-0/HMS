"""Rate-limit contract (LLD §3.10): a policy per scope, a decision per hit. Pure Python."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    scope: str
    limit: int
    window_seconds: int
    fail_open: bool = True  # what to do when the limiter's store is unreachable


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RateLimiter(ABC):
    @abstractmethod
    def hit(self, subject: str, policy: RateLimitPolicy) -> RateLimitDecision:
        """Count one request by ``subject`` under ``policy`` and say whether it may proceed."""
