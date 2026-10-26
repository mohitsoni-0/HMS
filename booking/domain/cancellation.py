"""Cancellation policies (LLD §8.2): how much of the amount paid a cancellation refunds.

A property picks a policy code; the booking snapshots it, so later property edits never change an existing
booking's terms (assumption A7). Lead times compare as ``timedelta`` (no float hours) and a tier boundary is
inclusive: exactly 24 hours before check-in still earns the 24-hour tier.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from catalog.public.enums import CancellationPolicyCode
from hotel_commons.domain.money import Money
from hotel_commons.exceptions import BusinessRuleViolation

from booking.status_code import UNSUPPORTED_CANCELLATION_POLICY

_NO_REFUND: Final = Decimal(0)


@dataclass(frozen=True, slots=True)
class RefundDecision:
    amount: Money
    percent: Decimal
    rule: str  # why, for support and audit: e.g. "FLEXIBLE: 24h+ before check-in -> 100%"

    @classmethod
    def nothing(cls, rule: str = "nothing paid") -> "RefundDecision":
        return cls(amount=Money.zero(), percent=_NO_REFUND, rule=rule)


@dataclass(frozen=True, slots=True)
class RefundTier:
    min_hours_before_check_in: int
    refund_percent: Decimal

    @property
    def min_lead_time(self) -> timedelta:
        return timedelta(hours=self.min_hours_before_check_in)


class CancellationPolicy(ABC):
    code: CancellationPolicyCode

    @abstractmethod
    def evaluate(self, *, amount_paid: Money, check_in_at: datetime, cancelled_at: datetime) -> RefundDecision: ...


class TieredCancellationPolicy(CancellationPolicy):
    """The first tier, from the longest lead time down, whose minimum the cancellation meets; else 0 %."""

    def __init__(self, code: CancellationPolicyCode, tiers: Sequence[RefundTier]) -> None:
        self.code = code
        self._tiers = tuple(sorted(tiers, key=lambda tier: tier.min_hours_before_check_in, reverse=True))

    def evaluate(self, *, amount_paid: Money, check_in_at: datetime, cancelled_at: datetime) -> RefundDecision:
        lead_time = check_in_at - cancelled_at
        for tier in self._tiers:
            if lead_time >= tier.min_lead_time:
                return RefundDecision(
                    amount=amount_paid.percent(tier.refund_percent),
                    percent=tier.refund_percent,
                    rule=f"{self.code.name}: {tier.min_hours_before_check_in}h+ before check-in -> "
                    f"{tier.refund_percent}%",
                )
        return RefundDecision(
            amount=Money.zero(amount_paid.currency), percent=_NO_REFUND, rule=f"{self.code.name}: no refund"
        )


DEFAULT_CANCELLATION_POLICIES: tuple[CancellationPolicy, ...] = (
    TieredCancellationPolicy(
        CancellationPolicyCode.FLEXIBLE, [RefundTier(24, Decimal(100)), RefundTier(0, Decimal(50))]
    ),
    TieredCancellationPolicy(
        CancellationPolicyCode.MODERATE, [RefundTier(120, Decimal(100)), RefundTier(24, Decimal(50))]
    ),
    TieredCancellationPolicy(CancellationPolicyCode.STRICT, [RefundTier(168, Decimal(50))]),
    TieredCancellationPolicy(CancellationPolicyCode.NON_REFUNDABLE, []),
)


class CancellationPolicyRegistry:
    def __init__(self, policies: Iterable[CancellationPolicy] = DEFAULT_CANCELLATION_POLICIES) -> None:
        self._policies: dict[CancellationPolicyCode, CancellationPolicy] = {}
        for policy in policies:
            self.register(policy)

    def register(self, policy: CancellationPolicy) -> None:
        self._policies[policy.code] = policy

    def get(self, code: CancellationPolicyCode) -> CancellationPolicy:
        try:
            return self._policies[code]
        except KeyError:
            raise BusinessRuleViolation(UNSUPPORTED_CANCELLATION_POLICY, policy=code.name) from None
