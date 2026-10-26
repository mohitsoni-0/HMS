"""Onboard a property with its room types (LLD §4.4, HLD §6.1).

One unit of work covers the catalog rows, the inventory provisioned for each room type and the
``PropertyOnboarded`` subscribers (the discovery projection), so they commit or roll back together.
A standalone owner and a chain go through exactly this path.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from hotel_commons.domain.unit_of_work import UnitOfWork
from hotel_commons.events.interfaces import EventPublisher
from hotel_commons.exceptions import NotFound

from catalog.domain.property import Property
from catalog.domain.property_rules import DEFAULT_PROPERTY_RULES, PropertyTypeRule
from catalog.interfaces.ports import InventoryProvisioningPort
from catalog.interfaces.repositories import AmenityRepository, OwnerAccountRepository, PropertyRepository
from catalog.public.events import PropertyOnboarded
from catalog.services.commands import PropertyDraft
from catalog.services.query_service import property_snapshot
from catalog.status_code import OWNER_ACCOUNT_NOT_FOUND

logger = logging.getLogger(__name__)


class PropertyOnboardingService:
    def __init__(
        self,
        accounts: OwnerAccountRepository,
        properties: PropertyRepository,
        amenities: AmenityRepository,
        provisioning: InventoryProvisioningPort,
        events: EventPublisher,
        uow: UnitOfWork,
        rules: Sequence[PropertyTypeRule] = DEFAULT_PROPERTY_RULES,
    ) -> None:
        self._accounts = accounts
        self._properties = properties
        self._amenities = amenities
        self._provisioning = provisioning
        self._events = events
        self._uow = uow
        self._rules = tuple(rules)

    def onboard(self, account_id: UUID, draft: PropertyDraft) -> Property:
        with self._uow:
            account = self._accounts.get(account_id)
            if not account.is_active:
                raise NotFound(OWNER_ACCOUNT_NOT_FOUND)
            property_ = Property.onboard(
                owner_account_id=account.id,
                draft=draft,
                known_amenities=self._amenities.known_codes(),
                rules=self._rules,
            )
            self._properties.add(property_)
            for room_type in property_.room_types:
                self._provisioning.provision(room_type.id, room_type.total_rooms)
            self._events.publish(PropertyOnboarded(property=property_snapshot(property_)))
        logger.info("Property %s onboarded for account %s", property_.id, account.id)
        return property_
