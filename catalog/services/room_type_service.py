"""Add a room type to an existing property (LLD §4.4).

The property row is locked for the unit of work, so two concurrent additions cannot both pass a rule that
counts room types (a villa has exactly one).
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from hotel_commons.domain.unit_of_work import UnitOfWork
from hotel_commons.events.interfaces import EventPublisher
from hotel_commons.exceptions import NotFound

from catalog.domain.property_rules import DEFAULT_PROPERTY_RULES, PropertyTypeRule
from catalog.domain.room_type import RoomType
from catalog.interfaces.ports import InventoryProvisioningPort
from catalog.interfaces.repositories import AmenityRepository, PropertyRepository
from catalog.public.events import RoomTypeAdded
from catalog.services.commands import RoomTypeDraft
from catalog.services.query_service import property_snapshot
from catalog.status_code import PROPERTY_NOT_FOUND

logger = logging.getLogger(__name__)


class RoomTypeService:
    def __init__(
        self,
        properties: PropertyRepository,
        amenities: AmenityRepository,
        provisioning: InventoryProvisioningPort,
        events: EventPublisher,
        uow: UnitOfWork,
        rules: Sequence[PropertyTypeRule] = DEFAULT_PROPERTY_RULES,
    ) -> None:
        self._properties = properties
        self._amenities = amenities
        self._provisioning = provisioning
        self._events = events
        self._uow = uow
        self._rules = tuple(rules)

    def add_room_type(self, property_id: UUID, draft: RoomTypeDraft) -> RoomType:
        with self._uow:
            property_ = self._properties.get(property_id, for_update=True)
            if not property_.is_active:
                raise NotFound(PROPERTY_NOT_FOUND)
            room_type = property_.add_room_type(draft, self._amenities.known_codes(), self._rules)
            self._properties.add_room_type(room_type)
            self._provisioning.provision(room_type.id, room_type.total_rooms)
            self._events.publish(RoomTypeAdded(property=property_snapshot(property_), room_type_id=room_type.id))
        logger.info("Room type %s added to property %s", room_type.id, property_.id)
        return room_type
