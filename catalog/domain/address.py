"""Postal address of a property (LLD §4.2)."""

import re
from dataclasses import dataclass
from typing import Final

from hotel_commons.exceptions import ValidationFailed

from catalog.constants import ADDRESS_LINE_MAX_LENGTH, ADDRESS_PART_MAX_LENGTH, DEFAULT_COUNTRY, POSTAL_CODE_PATTERN
from catalog.status_code import INVALID_PROPERTY

_POSTAL_CODE: Final = re.compile(POSTAL_CODE_PATTERN)
_COUNTRY_CODE: Final = re.compile(r"[A-Z]{2}")


@dataclass(frozen=True, slots=True)
class Address:
    line1: str
    locality: str
    city: str
    state: str
    postal_code: str
    country: str = DEFAULT_COUNTRY

    def __post_init__(self) -> None:
        for name in ("line1", "locality", "city", "state", "postal_code", "country"):
            object.__setattr__(self, name, getattr(self, name).strip())
        _require_text("line1", self.line1, ADDRESS_LINE_MAX_LENGTH)
        for name in ("locality", "city", "state"):
            _require_text(name, getattr(self, name), ADDRESS_PART_MAX_LENGTH)
        if not _POSTAL_CODE.fullmatch(self.postal_code):
            raise ValidationFailed(INVALID_PROPERTY, reason="address.postal_code must be 6 digits")
        if not _COUNTRY_CODE.fullmatch(self.country):
            raise ValidationFailed(INVALID_PROPERTY, reason="address.country must be a 2-letter ISO code")

    @property
    def city_key(self) -> str:
        """Case-insensitive search key of the city: "  Goa " -> "goa"."""
        return self.city.strip().lower()


def _require_text(name: str, value: str, max_length: int) -> None:
    if not value or len(value) > max_length:
        raise ValidationFailed(INVALID_PROPERTY, reason=f"address.{name} must be 1-{max_length} characters")
