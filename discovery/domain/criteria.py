"""What a guest searches for (LLD §7.1): city, stay, party size, filters, sort order and page.

``SearchCriteria`` re-checks its own invariants, so the service never runs on a criteria object the API
layer would have refused, whoever built it.
"""

from dataclasses import dataclass, field
from typing import Final

from catalog.public.enums import PropertyType
from discovery.status_code import INVALID_SEARCH
from hotel_commons.domain.date_range import DateRange
from hotel_commons.domain.money import Money
from hotel_commons.enum import Enum
from hotel_commons.exceptions import ValidationFailed

MAX_PAGE_SIZE: Final = 50
MAX_STAR_RATING: Final = 5


class SortOrder(Enum):
    """Result order. The labels are the wire names: ``?sort=price_asc``."""

    __labels__ = {1: "price_asc", 2: "price_desc", 3: "rating_desc"}

    PRICE_LOW_TO_HIGH = 1
    PRICE_HIGH_TO_LOW = 2
    RATING_HIGH_TO_LOW = 3


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    city: str
    stay: DateRange
    guests: int
    rooms: int = 1
    locality: str | None = None
    min_price: Money | None = None
    max_price: Money | None = None
    amenities: frozenset[str] = field(default_factory=frozenset)
    min_star_rating: int | None = None
    property_types: frozenset[PropertyType] = field(default_factory=frozenset)
    sort: SortOrder = SortOrder.PRICE_LOW_TO_HIGH
    page: int = 1
    page_size: int = 20

    def __post_init__(self) -> None:
        reason = _first_violation(self)
        if reason is not None:
            raise ValidationFailed(INVALID_SEARCH, reason=reason)

    @property
    def min_occupancy_per_room(self) -> int:
        """The guests spread over the rooms, rounded up: 5 guests in 2 rooms need rooms for 3."""
        return -(-self.guests // self.rooms)


def _first_violation(criteria: SearchCriteria) -> str | None:
    if not criteria.city.strip():
        return "city is required"
    if criteria.guests < 1 or criteria.rooms < 1:
        return "guests and rooms must be at least 1"
    if criteria.page < 1 or not 1 <= criteria.page_size <= MAX_PAGE_SIZE:
        return f"page must be at least 1 and page_size between 1 and {MAX_PAGE_SIZE}"
    if criteria.min_star_rating is not None and not 1 <= criteria.min_star_rating <= MAX_STAR_RATING:
        return f"min_rating must be between 1 and {MAX_STAR_RATING}"
    if criteria.min_price is not None and criteria.max_price is not None and criteria.min_price > criteria.max_price:
        return "min_price must not exceed max_price"
    return None
