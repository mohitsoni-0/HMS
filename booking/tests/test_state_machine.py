"""Booking lifecycle (LLD §8.2): every allowed transition, and every other (state, trigger) pair is a 409."""

import itertools

import pytest
from hotel_commons.exceptions import Conflict

from booking.domain.state_machine import TRANSITIONS, BookingStateMachine
from booking.enums import BookingStatus, BookingTrigger
from booking.status_code import INVALID_STATE_TRANSITION

INVALID_PAIRS = [pair for pair in itertools.product(BookingStatus, BookingTrigger) if pair not in TRANSITIONS]


@pytest.mark.parametrize(
    ("current", "trigger", "expected"),
    [
        (BookingStatus.PENDING_PAYMENT, BookingTrigger.CONFIRM_PAYMENT, BookingStatus.CONFIRMED),
        (BookingStatus.PENDING_PAYMENT, BookingTrigger.CANCEL, BookingStatus.CANCELLED),
        (BookingStatus.PENDING_PAYMENT, BookingTrigger.EXPIRE, BookingStatus.EXPIRED),
        (BookingStatus.CONFIRMED, BookingTrigger.CANCEL, BookingStatus.CANCELLED),
    ],
)
def test_every_valid_transition(current: BookingStatus, trigger: BookingTrigger, expected: BookingStatus) -> None:
    assert BookingStateMachine.next_state(current, trigger) is expected
    assert trigger in BookingStateMachine.allowed_triggers(current)


def test_transition_table_is_exactly_the_documented_lifecycle() -> None:
    assert len(TRANSITIONS) == 4
    assert BookingStateMachine.allowed_triggers(BookingStatus.CANCELLED) == frozenset()
    assert BookingStateMachine.allowed_triggers(BookingStatus.EXPIRED) == frozenset()


@pytest.mark.parametrize(("current", "trigger"), INVALID_PAIRS)
def test_invalid_transitions_raise_409(current: BookingStatus, trigger: BookingTrigger) -> None:
    with pytest.raises(Conflict) as raised:
        BookingStateMachine.next_state(current, trigger)

    assert raised.value.status_code is INVALID_STATE_TRANSITION
    assert raised.value.http_status == 409
    assert raised.value.message == f"Cannot {trigger.name.lower()} a booking that is {current.name}"
