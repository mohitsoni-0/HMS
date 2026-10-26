"""Domain events the catalog publishes (LLD §4.5).

Published through the injected ``EventPublisher`` inside the onboarding transaction, so a subscriber such as
the discovery projector commits or rolls back together with the catalog write.
"""

from dataclasses import dataclass
from uuid import UUID

from hotel_commons.events.base import DomainEvent

from catalog.public.dto import PropertySnapshot


@dataclass(frozen=True, kw_only=True)
class PropertyOnboarded(DomainEvent):
    property: PropertySnapshot


@dataclass(frozen=True, kw_only=True)
class RoomTypeAdded(DomainEvent):
    property: PropertySnapshot
    room_type_id: UUID
