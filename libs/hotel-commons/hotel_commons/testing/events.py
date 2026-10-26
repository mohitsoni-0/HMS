"""An ``EventPublisher`` that records events for assertions."""

from typing import TypeVar

from hotel_commons.events.base import DomainEvent
from hotel_commons.events.interfaces import EventPublisher

E = TypeVar("E", bound=DomainEvent)


class RecordingEventPublisher(EventPublisher):
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    def publish(self, event: DomainEvent) -> None:
        self.events.append(event)

    def of_type(self, event_type: type[E]) -> list[E]:
        """The recorded events of ``event_type`` (subclasses included), in publication order."""
        return [event for event in self.events if isinstance(event, event_type)]
