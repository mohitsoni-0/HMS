"""Base class of every domain event (LLD §3.12).

Events are immutable facts. Each carries a UUIDv7 id (unique and time-ordered, ready for an outbox or a
broker later) and the aware-UTC instant it happened. Subclasses add their own keyword-only fields.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from uuid6 import uuid7


def _new_event_id() -> str:
    return str(uuid7())


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    event_id: str = field(default_factory=_new_event_id)
    occurred_at: datetime = field(default_factory=_utc_now)

    @property
    def event_type(self) -> str:
        """The event's class name, e.g. "BookingConfirmed"; handy for logs and routing."""
        return type(self).__name__
