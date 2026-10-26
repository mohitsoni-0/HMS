"""Room type: a sellable kind of room at a property, with its base nightly price (LLD §4.2)."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self
from uuid import UUID

from hotel_commons.domain.money import Money
from hotel_commons.exceptions import ValidationFailed
from uuid6 import uuid7

from catalog.constants import (
    MAX_OCCUPANCY_MAX,
    MAX_OCCUPANCY_MIN,
    ROOM_TYPE_NAME_MAX_LENGTH,
    ROOM_TYPE_NAME_MIN_LENGTH,
    TOTAL_ROOMS_MAX,
    TOTAL_ROOMS_MIN,
)
from catalog.status_code import INVALID_PROPERTY

if TYPE_CHECKING:
    from catalog.services.commands import RoomTypeDraft


@dataclass(slots=True)
class RoomType:
    id: UUID
    property_id: UUID
    name: str
    max_occupancy: int
    total_rooms: int
    base_price: Money
    amenity_codes: frozenset[str]
    description: str = ""
    is_active: bool = True

    @classmethod
    def create(cls, *, property_id: UUID, draft: "RoomTypeDraft") -> Self:
        """A new room type from ``draft``; limits are checked here, amenities and names by the property."""
        name = draft.name.strip()
        _require_length("name", name, ROOM_TYPE_NAME_MIN_LENGTH, ROOM_TYPE_NAME_MAX_LENGTH)
        _require_range("max_occupancy", draft.max_occupancy, MAX_OCCUPANCY_MIN, MAX_OCCUPANCY_MAX)
        _require_range("total_rooms", draft.total_rooms, TOTAL_ROOMS_MIN, TOTAL_ROOMS_MAX)
        if not draft.base_price.is_positive():
            raise ValidationFailed(INVALID_PROPERTY, reason=f"Room type '{name}': base_price must be positive")
        return cls(
            id=uuid7(),
            property_id=property_id,
            name=name,
            max_occupancy=draft.max_occupancy,
            total_rooms=draft.total_rooms,
            base_price=draft.base_price,
            amenity_codes=frozenset(draft.amenity_codes),
            description=draft.description.strip(),
        )

    @property
    def name_key(self) -> str:
        """The name as the uniqueness rule compares it: case-insensitive, like ``Lower(name)`` in the database."""
        return self.name.lower()


def _require_length(field_name: str, value: str, minimum: int, maximum: int) -> None:
    if not minimum <= len(value) <= maximum:
        reason = f"Room type {field_name} must be {minimum}-{maximum} characters"
        raise ValidationFailed(INVALID_PROPERTY, reason=reason)


def _require_range(field_name: str, value: int, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        reason = f"Room type {field_name} must be between {minimum} and {maximum}"
        raise ValidationFailed(INVALID_PROPERTY, reason=reason)
