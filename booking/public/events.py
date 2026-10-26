"""Domain events the booking context publishes (LLD §8.5).

Published inside the use case's unit of work, so a subscriber commits or rolls back with the booking. They
have no subscribers today; they are the hook for notifications. Ids only: never guest PII.
"""

from dataclasses import dataclass
from uuid import UUID

from hotel_commons.events.base import DomainEvent


@dataclass(frozen=True, kw_only=True)
class BookingEvent(DomainEvent):
    booking_id: UUID
    guest_id: str
    property_id: UUID


@dataclass(frozen=True, kw_only=True)
class BookingCreated(BookingEvent):
    """A booking was made: PENDING_PAYMENT (prepaid) or CONFIRMED (pay at property)."""


@dataclass(frozen=True, kw_only=True)
class BookingConfirmed(BookingEvent):
    """A prepaid booking was paid."""


@dataclass(frozen=True, kw_only=True)
class BookingCancelled(BookingEvent):
    """A booking was cancelled; its rooms are back on sale and any refund is done."""


@dataclass(frozen=True, kw_only=True)
class BookingExpired(BookingEvent):
    """A prepaid booking's hold lapsed unpaid; its rooms are back on sale."""
