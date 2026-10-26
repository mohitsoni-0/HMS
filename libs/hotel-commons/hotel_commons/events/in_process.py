"""Synchronous in-process event bus (LLD §3.12).

Handlers run in the publisher's thread, inside the publisher's unit of work, so a catalog write and the
discovery projection it triggers commit or roll back together. A handler error propagates to the publisher
(and rolls its transaction back); handlers after the failing one do not run.
"""

import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from hotel_commons.events.base import DomainEvent
from hotel_commons.events.interfaces import EventPublisher

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=DomainEvent)


class InProcessEventBus(EventPublisher):
    """Calls each handler whose subscribed type matches the event (subclasses included), in subscription order."""

    def __init__(self) -> None:
        self._subscriptions: list[tuple[type[DomainEvent], Callable[[Any], None]]] = []
        self._lock = threading.Lock()

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> None:
        """Register ``handler`` for ``event_type``. Repeating the same pair (e.g. a second ``ready()``) is a no-op."""
        with self._lock:
            if (event_type, handler) in self._subscriptions:
                logger.debug("Ignoring duplicate subscription to %s", event_type.__name__)
                return
            self._subscriptions.append((event_type, handler))

    def publish(self, event: DomainEvent) -> None:
        with self._lock:
            subscriptions = tuple(self._subscriptions)
        handlers = [handler for event_type, handler in subscriptions if isinstance(event, event_type)]
        logger.debug("Publishing %s %s to %d handler(s)", event.event_type, event.event_id, len(handlers))
        for handler in handlers:
            handler(event)


event_bus = InProcessEventBus()
