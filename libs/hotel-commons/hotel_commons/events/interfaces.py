"""Port for publishing domain events (LLD §3.12).

Services depend on this interface only. The in-process bus implements it today; a transactional outbox
feeding a broker can replace it without touching publishers or handlers (HLD §14).
"""

from abc import ABC, abstractmethod

from hotel_commons.events.base import DomainEvent


class EventPublisher(ABC):
    @abstractmethod
    def publish(self, event: DomainEvent) -> None:
        """Deliver ``event`` to everything subscribed to its type."""
