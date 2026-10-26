"""The booking lifecycle as a transition table (LLD §8.2, HLD §7.1).

Every state change goes through ``BookingStateMachine.next_state``; a pair missing from ``TRANSITIONS`` is a
409, so adding a state or trigger means adding rows here and nowhere else.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from hotel_commons.exceptions import Conflict

from booking.enums import BookingStatus, BookingTrigger
from booking.status_code import INVALID_STATE_TRANSITION

TRANSITIONS: Final[Mapping[tuple[BookingStatus, BookingTrigger], BookingStatus]] = MappingProxyType(
    {
        (BookingStatus.PENDING_PAYMENT, BookingTrigger.CONFIRM_PAYMENT): BookingStatus.CONFIRMED,
        (BookingStatus.PENDING_PAYMENT, BookingTrigger.CANCEL): BookingStatus.CANCELLED,
        (BookingStatus.PENDING_PAYMENT, BookingTrigger.EXPIRE): BookingStatus.EXPIRED,
        (BookingStatus.CONFIRMED, BookingTrigger.CANCEL): BookingStatus.CANCELLED,
    }
)


class BookingStateMachine:
    @staticmethod
    def next_state(current: BookingStatus, trigger: BookingTrigger) -> BookingStatus:
        """The state ``trigger`` leads to from ``current``; an undefined pair raises Conflict (409)."""
        try:
            return TRANSITIONS[(current, trigger)]
        except KeyError:
            raise Conflict(INVALID_STATE_TRANSITION, action=trigger.name.lower(), state=current.name) from None

    @staticmethod
    def allowed_triggers(current: BookingStatus) -> frozenset[BookingTrigger]:
        return frozenset(trigger for status, trigger in TRANSITIONS if status is current)
