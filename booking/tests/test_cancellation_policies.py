"""Cancellation policies (LLD §8.2): tier boundaries are inclusive and computed without floats."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from catalog.public.enums import CancellationPolicyCode
from hotel_commons.domain.clock import BUSINESS_TIME_ZONE
from hotel_commons.domain.money import Money
from hotel_commons.exceptions import BusinessRuleViolation

from booking.domain.cancellation import (
    DEFAULT_CANCELLATION_POLICIES,
    CancellationPolicyRegistry,
    RefundDecision,
    RefundTier,
    TieredCancellationPolicy,
)
from booking.status_code import UNSUPPORTED_CANCELLATION_POLICY

CHECK_IN_AT = datetime(2026, 1, 15, 14, 0, tzinfo=BUSINESS_TIME_ZONE)
PAID = Money(800_001)  # odd paisa, so a 50 % refund exercises ROUND_HALF_UP


def evaluate(code: CancellationPolicyCode, lead_time: timedelta) -> RefundDecision:
    policy = CancellationPolicyRegistry().get(code)
    return policy.evaluate(amount_paid=PAID, check_in_at=CHECK_IN_AT, cancelled_at=CHECK_IN_AT - lead_time)


@pytest.mark.parametrize(
    ("code", "lead_time", "percent"),
    [
        (CancellationPolicyCode.FLEXIBLE, timedelta(hours=24), 100),
        (CancellationPolicyCode.FLEXIBLE, timedelta(hours=23, minutes=59), 50),
        (CancellationPolicyCode.FLEXIBLE, timedelta(minutes=1), 50),
        (CancellationPolicyCode.MODERATE, timedelta(hours=120), 100),
        (CancellationPolicyCode.MODERATE, timedelta(hours=119, minutes=59), 50),
        (CancellationPolicyCode.MODERATE, timedelta(hours=24), 50),
        (CancellationPolicyCode.MODERATE, timedelta(hours=23, minutes=59), 0),
        (CancellationPolicyCode.STRICT, timedelta(hours=168), 50),
        (CancellationPolicyCode.STRICT, timedelta(hours=167, minutes=59), 0),
        (CancellationPolicyCode.NON_REFUNDABLE, timedelta(days=60), 0),
    ],
)
def test_cancellation_tier_boundaries(code: CancellationPolicyCode, lead_time: timedelta, percent: int) -> None:
    decision = evaluate(code, lead_time)

    assert decision.percent == Decimal(percent)
    assert decision.amount == PAID.percent(Decimal(percent))
    assert decision.rule.startswith(code.name)


def test_half_refund_rounds_half_up_to_whole_paisa() -> None:
    assert evaluate(CancellationPolicyCode.FLEXIBLE, timedelta(hours=1)).amount == Money(400_001)


def test_cancelling_after_check_in_refunds_nothing() -> None:
    assert evaluate(CancellationPolicyCode.FLEXIBLE, -timedelta(minutes=1)).amount == Money.zero()


def test_tiers_are_applied_from_the_longest_lead_time_whatever_their_order() -> None:
    policy = TieredCancellationPolicy(
        CancellationPolicyCode.MODERATE, [RefundTier(24, Decimal(50)), RefundTier(120, Decimal(100))]
    )
    decision = policy.evaluate(amount_paid=PAID, check_in_at=CHECK_IN_AT, cancelled_at=CHECK_IN_AT - timedelta(days=6))

    assert decision.percent == Decimal(100)


def test_every_policy_code_is_registered_by_default() -> None:
    registry = CancellationPolicyRegistry()

    assert {policy.code for policy in DEFAULT_CANCELLATION_POLICIES} == set(CancellationPolicyCode)
    for code in CancellationPolicyCode:
        assert registry.get(code).code is code


def test_unregistered_policy_is_rejected() -> None:
    registry = CancellationPolicyRegistry(policies=())

    with pytest.raises(BusinessRuleViolation) as raised:
        registry.get(CancellationPolicyCode.STRICT)

    assert raised.value.status_code is UNSUPPORTED_CANCELLATION_POLICY


def test_nothing_refunds_zero() -> None:
    decision = RefundDecision.nothing()

    assert decision.amount == Money.zero()
    assert decision.percent == Decimal(0)
    assert decision.rule == "nothing paid"
