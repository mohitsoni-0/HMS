"""Read models the catalog hands to other contexts (LLD §4.5).

Immutable, Django-free snapshots: discovery projects ``PropertySnapshot`` into its listing, booking prices
and validates a stay from ``RoomTypeSnapshot``. Amounts are ``Money`` (integer paisa).
"""

from dataclasses import dataclass
from datetime import time
from uuid import UUID

from hotel_commons.domain.money import Money

from catalog.public.enums import CancellationPolicyCode, PricingStrategyCode, PropertyType


@dataclass(frozen=True, slots=True)
class RoomTypeSummary:
    room_type_id: UUID
    name: str
    max_occupancy: int
    total_rooms: int
    base_price: Money
    amenity_codes: frozenset[str]
    is_active: bool


@dataclass(frozen=True, slots=True)
class PropertySnapshot:
    property_id: UUID
    owner_account_id: UUID
    name: str
    property_type: PropertyType
    star_rating: int | None
    city: str
    locality: str
    amenity_codes: frozenset[str]
    pricing_strategy: PricingStrategyCode
    is_active: bool
    room_types: tuple[RoomTypeSummary, ...]


@dataclass(frozen=True, slots=True)
class RoomTypeSnapshot:
    room_type_id: UUID
    property_id: UUID
    owner_account_id: UUID
    property_name: str
    room_type_name: str
    max_occupancy: int
    total_rooms: int
    base_price: Money
    pricing_strategy: PricingStrategyCode
    cancellation_policy: CancellationPolicyCode
    check_in_time: time
    is_active: bool
