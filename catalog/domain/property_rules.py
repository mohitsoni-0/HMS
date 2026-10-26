"""Property-type rules: the extension point for property kinds (LLD §4.2, HLD §11).

A new kind of property gets a ``PropertyType`` member and, when it has rules of its own, a ``PropertyTypeRule``
added to ``DEFAULT_PROPERTY_RULES``. The services and the aggregate stay untouched.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from hotel_commons.exceptions import BusinessRuleViolation

from catalog.enums import PropertyType
from catalog.status_code import PROPERTY_TYPE_RULE_VIOLATED

if TYPE_CHECKING:
    from catalog.domain.property import Property


class PropertyTypeRule(ABC):
    applies_to: ClassVar[frozenset[PropertyType]]

    @abstractmethod
    def validate(self, property_: "Property") -> None:
        """Raise BusinessRuleViolation(PROPERTY_TYPE_RULE_VIOLATED, reason=...) when the property breaks the rule."""


class StarRatingRequiredRule(PropertyTypeRule):
    """Hotels and resorts are classified, so they need a star rating."""

    applies_to = frozenset({PropertyType.HOTEL, PropertyType.RESORT})

    def validate(self, property_: "Property") -> None:
        if property_.star_rating is None:
            reason = f"A {property_.property_type.name} needs a star rating"
            raise BusinessRuleViolation(PROPERTY_TYPE_RULE_VIOLATED, reason=reason)


class EntirePlaceRule(PropertyTypeRule):
    """A villa is rented as a whole: exactly one room type, of exactly one unit."""

    applies_to = frozenset({PropertyType.VILLA})

    def validate(self, property_: "Property") -> None:
        room_types = property_.room_types
        if len(room_types) != 1 or room_types[0].total_rooms != 1:
            reason = f"A {property_.property_type.name} is rented as an entire place: one room type with total_rooms 1"
            raise BusinessRuleViolation(PROPERTY_TYPE_RULE_VIOLATED, reason=reason)


DEFAULT_PROPERTY_RULES: tuple[PropertyTypeRule, ...] = (StarRatingRequiredRule(), EntirePlaceRule())
