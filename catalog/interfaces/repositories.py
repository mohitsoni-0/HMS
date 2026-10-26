"""Repository ports of the catalog (LLD §4.3). Django implementations live in ``catalog.repositories``;
in-memory ones in ``catalog.tests.fakes``."""

from abc import ABC, abstractmethod
from uuid import UUID

from catalog.domain.owner_account import OwnerAccount
from catalog.domain.property import Amenity, Property
from catalog.domain.room_type import RoomType


class OwnerAccountRepository(ABC):
    @abstractmethod
    def add(self, account: OwnerAccount) -> None:
        """Persist a new account; a second account with the same contact email -> Conflict(OWNER_ACCOUNT_EXISTS)."""

    @abstractmethod
    def get(self, account_id: UUID) -> OwnerAccount:
        """The account; NotFound(OWNER_ACCOUNT_NOT_FOUND) when there is none."""


class PropertyRepository(ABC):
    @abstractmethod
    def add(self, property_: Property) -> None:
        """Persist a new property together with its room types."""

    @abstractmethod
    def add_room_type(self, room_type: RoomType) -> None:
        """Persist a room type of an existing property; a taken name -> Conflict(DUPLICATE_ROOM_TYPE_NAME)."""

    @abstractmethod
    def get(self, property_id: UUID, *, for_update: bool = False) -> Property:
        """The property with its room types; NotFound(PROPERTY_NOT_FOUND) when there is none.

        ``for_update`` locks the property row until the transaction ends, so rules that look at all room types
        (e.g. a villa has exactly one) hold under concurrent additions.
        """

    @abstractmethod
    def list_for_account(self, account_id: UUID) -> list[Property]:
        """The account's properties, oldest first."""

    @abstractmethod
    def get_room_type(self, room_type_id: UUID) -> tuple[Property, RoomType]:
        """The room type and its property; NotFound(ROOM_TYPE_NOT_FOUND) when there is none."""


class AmenityRepository(ABC):
    @abstractmethod
    def known_codes(self) -> frozenset[str]:
        """Codes of the active amenities."""

    @abstractmethod
    def list_all(self) -> list[Amenity]:
        """Every active amenity, ordered by category then code."""
