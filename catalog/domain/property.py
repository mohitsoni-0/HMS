"""Property aggregate: a hotel, resort, villa... with its room types (LLD §4.2).

Invariants checked on onboarding and on every room-type addition:
- name 2-200 characters, star rating 1-5 or None -> ValidationFailed(INVALID_PROPERTY);
- at least one room type -> ValidationFailed(ROOM_TYPE_REQUIRED);
- room-type limits (``RoomType.create``) -> ValidationFailed(INVALID_PROPERTY);
- room-type names unique per property, case-insensitive -> Conflict(DUPLICATE_ROOM_TYPE_NAME);
- amenity codes known -> ValidationFailed(UNKNOWN_AMENITIES);
- the property-type rules that apply to its type -> BusinessRuleViolation(PROPERTY_TYPE_RULE_VIOLATED).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING, Self
from uuid import UUID

from hotel_commons.exceptions import Conflict, ValidationFailed
from uuid6 import uuid7

from catalog.constants import PROPERTY_NAME_MAX_LENGTH, PROPERTY_NAME_MIN_LENGTH, STAR_RATING_MAX, STAR_RATING_MIN
from catalog.domain.address import Address
from catalog.domain.room_type import RoomType
from catalog.enums import AmenityCategory, CancellationPolicyCode, PricingStrategyCode, PropertyType
from catalog.status_code import DUPLICATE_ROOM_TYPE_NAME, INVALID_PROPERTY, ROOM_TYPE_REQUIRED, UNKNOWN_AMENITIES

if TYPE_CHECKING:
    from catalog.domain.property_rules import PropertyTypeRule
    from catalog.services.commands import PropertyDraft, RoomTypeDraft


@dataclass(frozen=True, slots=True)
class Amenity:
    code: str
    name: str
    category: AmenityCategory


@dataclass(slots=True)
class Property:
    id: UUID
    owner_account_id: UUID
    name: str
    property_type: PropertyType
    star_rating: int | None
    address: Address
    amenity_codes: frozenset[str]
    pricing_strategy: PricingStrategyCode
    cancellation_policy: CancellationPolicyCode
    check_in_time: time
    check_out_time: time
    description: str
    room_types: list[RoomType]
    is_active: bool = True

    @classmethod
    def onboard(
        cls,
        *,
        owner_account_id: UUID,
        draft: "PropertyDraft",
        known_amenities: frozenset[str],
        rules: Sequence["PropertyTypeRule"],
    ) -> Self:
        """A new property with its room types, once every invariant and applicable rule holds."""
        name = draft.name.strip()
        _require_name(name)
        _require_star_rating(draft.star_rating)
        if not draft.room_types:
            raise ValidationFailed(ROOM_TYPE_REQUIRED)
        _require_known_amenities(
            [draft.amenity_codes, *(room_type.amenity_codes for room_type in draft.room_types)], known_amenities
        )
        property_ = cls(
            id=uuid7(),
            owner_account_id=owner_account_id,
            name=name,
            property_type=draft.property_type,
            star_rating=draft.star_rating,
            address=draft.address,
            amenity_codes=frozenset(draft.amenity_codes),
            pricing_strategy=draft.pricing_strategy,
            cancellation_policy=draft.cancellation_policy,
            check_in_time=draft.check_in_time,
            check_out_time=draft.check_out_time,
            description=draft.description.strip(),
            room_types=[],
        )
        for room_type_draft in draft.room_types:
            property_._append_room_type(room_type_draft)
        property_._apply_rules(rules)
        return property_

    def add_room_type(
        self, draft: "RoomTypeDraft", known_amenities: frozenset[str], rules: Sequence["PropertyTypeRule"]
    ) -> RoomType:
        """Append a room type and re-run the applicable rules; on any violation the property is left unchanged."""
        _require_known_amenities([draft.amenity_codes], known_amenities)
        room_type = self._append_room_type(draft)
        try:
            self._apply_rules(rules)
        except Exception:
            self.room_types.remove(room_type)
            raise
        return room_type

    def room_type(self, room_type_id: UUID) -> RoomType | None:
        return next((room_type for room_type in self.room_types if room_type.id == room_type_id), None)

    def _append_room_type(self, draft: "RoomTypeDraft") -> RoomType:
        room_type = RoomType.create(property_id=self.id, draft=draft)
        if any(existing.name_key == room_type.name_key for existing in self.room_types):
            raise Conflict(DUPLICATE_ROOM_TYPE_NAME, name=room_type.name)
        self.room_types.append(room_type)
        return room_type

    def _apply_rules(self, rules: Sequence["PropertyTypeRule"]) -> None:
        for rule in rules:
            if self.property_type in rule.applies_to:
                rule.validate(self)


def _require_name(name: str) -> None:
    if not PROPERTY_NAME_MIN_LENGTH <= len(name) <= PROPERTY_NAME_MAX_LENGTH:
        limits = f"{PROPERTY_NAME_MIN_LENGTH}-{PROPERTY_NAME_MAX_LENGTH}"
        raise ValidationFailed(INVALID_PROPERTY, reason=f"Property name must be {limits} characters")


def _require_star_rating(star_rating: int | None) -> None:
    if star_rating is None:
        return
    if type(star_rating) is not int or not STAR_RATING_MIN <= star_rating <= STAR_RATING_MAX:
        reason = f"star_rating must be between {STAR_RATING_MIN} and {STAR_RATING_MAX}"
        raise ValidationFailed(INVALID_PROPERTY, reason=reason)


def _require_known_amenities(code_sets: Iterable[frozenset[str]], known_amenities: frozenset[str]) -> None:
    unknown = set().union(*code_sets) - known_amenities
    if unknown:
        raise ValidationFailed(UNKNOWN_AMENITIES, codes=", ".join(sorted(unknown)))
