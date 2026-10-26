"""In-memory repositories and ports for the catalog unit tests, plus draft builders.

The repositories enforce the same uniqueness rules as the database (contact email, room-type name per
property, case-insensitive) and hand out copies, so a test only sees what a service actually persisted.
"""

import copy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from hotel_commons.domain.money import Money
from hotel_commons.domain.validation import normalize_email
from hotel_commons.exceptions import Conflict, NotFound

from catalog.constants import AMENITY_SEED, DEFAULT_CHECK_IN_TIME, DEFAULT_CHECK_OUT_TIME
from catalog.domain.address import Address
from catalog.domain.owner_account import OwnerAccount
from catalog.domain.property import Amenity, Property
from catalog.domain.room_type import RoomType
from catalog.enums import CancellationPolicyCode, PricingStrategyCode, PropertyType
from catalog.interfaces.ports import InventoryProvisioningPort
from catalog.interfaces.repositories import AmenityRepository, OwnerAccountRepository, PropertyRepository
from catalog.services.commands import OwnerAccountDraft, PropertyDraft, RoomTypeDraft
from catalog.status_code import (
    DUPLICATE_ROOM_TYPE_NAME,
    OWNER_ACCOUNT_EXISTS,
    OWNER_ACCOUNT_NOT_FOUND,
    PROPERTY_NOT_FOUND,
    ROOM_TYPE_NOT_FOUND,
)

CREATED_AT = datetime(2026, 1, 5, 6, 30, tzinfo=UTC)


class InMemoryOwnerAccountRepository(OwnerAccountRepository):
    def __init__(self) -> None:
        self.accounts: dict[UUID, OwnerAccount] = {}

    def add(self, account: OwnerAccount) -> None:
        email = normalize_email(account.contact.email)
        if any(normalize_email(stored.contact.email) == email for stored in self.accounts.values()):
            raise Conflict(OWNER_ACCOUNT_EXISTS)
        account.created_at = CREATED_AT
        self.accounts[account.id] = copy.deepcopy(account)

    def get(self, account_id: UUID) -> OwnerAccount:
        if account_id not in self.accounts:
            raise NotFound(OWNER_ACCOUNT_NOT_FOUND)
        return copy.deepcopy(self.accounts[account_id])


class InMemoryPropertyRepository(PropertyRepository):
    def __init__(self) -> None:
        self.properties: dict[UUID, Property] = {}

    def add(self, property_: Property) -> None:
        self.properties[property_.id] = copy.deepcopy(property_)

    def add_room_type(self, room_type: RoomType) -> None:
        stored = self.properties[room_type.property_id]
        if any(existing.name_key == room_type.name_key for existing in stored.room_types):
            raise Conflict(DUPLICATE_ROOM_TYPE_NAME, name=room_type.name)
        stored.room_types.append(copy.deepcopy(room_type))

    def get(self, property_id: UUID, *, for_update: bool = False) -> Property:
        if property_id not in self.properties:
            raise NotFound(PROPERTY_NOT_FOUND)
        return copy.deepcopy(self.properties[property_id])

    def list_for_account(self, account_id: UUID) -> list[Property]:
        return [copy.deepcopy(p) for p in self.properties.values() if p.owner_account_id == account_id]

    def get_room_type(self, room_type_id: UUID) -> tuple[Property, RoomType]:
        for property_ in self.properties.values():
            for room_type in property_.room_types:
                if room_type.id == room_type_id:
                    found = copy.deepcopy(property_)
                    return found, next(rt for rt in found.room_types if rt.id == room_type_id)
        raise NotFound(ROOM_TYPE_NOT_FOUND)


class InMemoryAmenityRepository(AmenityRepository):
    def __init__(self) -> None:
        self.amenities = [Amenity(code=code, name=name, category=category) for code, name, category in AMENITY_SEED]

    def known_codes(self) -> frozenset[str]:
        return frozenset(amenity.code for amenity in self.amenities)

    def list_all(self) -> list[Amenity]:
        return sorted(self.amenities, key=lambda amenity: (amenity.category.value, amenity.code))


class RecordingProvisioning(InventoryProvisioningPort):
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, int]] = []

    def provision(self, room_type_id: UUID, total_rooms: int) -> None:
        self.calls.append((room_type_id, total_rooms))


KNOWN_AMENITIES = frozenset(code for code, _, _ in AMENITY_SEED)

GOA = Address(line1="12 Beach Road", locality="Candolim", city="Goa", state="Goa", postal_code="403515")


def owner_draft(email: str = "owner@example.com", display_name: str = "Coastal Stays") -> OwnerAccountDraft:
    return OwnerAccountDraft(display_name=display_name, contact_email=email, contact_phone="9876543210")


def room_type_draft(name: str = "Deluxe", **overrides: object) -> RoomTypeDraft:
    draft = RoomTypeDraft(
        name=name,
        max_occupancy=2,
        total_rooms=5,
        base_price=Money.of_rupees("4800"),
        amenity_codes=frozenset({"TV", "AIR_CONDITIONING"}),
    )
    return replace(draft, **overrides)


def property_draft(**overrides: object) -> PropertyDraft:
    draft = PropertyDraft(
        name="Sea Breeze Resort",
        property_type=PropertyType.RESORT,
        star_rating=4,
        address=GOA,
        amenity_codes=frozenset({"POOL", "WIFI"}),
        pricing_strategy=PricingStrategyCode.WEEKEND,
        cancellation_policy=CancellationPolicyCode.MODERATE,
        check_in_time=DEFAULT_CHECK_IN_TIME,
        check_out_time=DEFAULT_CHECK_OUT_TIME,
        description="Steps from the beach",
        room_types=(room_type_draft(),),
    )
    return replace(draft, **overrides)


def villa_draft(**overrides: object) -> PropertyDraft:
    entire_villa = room_type_draft("Entire Villa", max_occupancy=8, total_rooms=1)
    return property_draft(
        **{
            "name": "Casa Anjuna",
            "property_type": PropertyType.VILLA,
            "star_rating": None,
            "room_types": (entire_villa,),
            **overrides,
        }
    )
