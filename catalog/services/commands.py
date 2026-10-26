"""Commands the catalog use cases accept (LLD §4.2).

Built by the views from validated serializer data (and by ``seed_demo``). Amounts are already ``Money``:
rupees become paisa only in ``HotelAmountField``. Contact details are PII, so they stay out of ``repr``.
"""

from dataclasses import dataclass, field
from datetime import time

from hotel_commons.domain.money import Money

from catalog.domain.address import Address
from catalog.enums import CancellationPolicyCode, PricingStrategyCode, PropertyType


@dataclass(frozen=True, slots=True)
class OwnerAccountDraft:
    display_name: str
    contact_email: str = field(repr=False)
    contact_phone: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RoomTypeDraft:
    name: str
    max_occupancy: int
    total_rooms: int
    base_price: Money
    amenity_codes: frozenset[str]
    description: str = ""


@dataclass(frozen=True, slots=True)
class PropertyDraft:
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
    room_types: tuple[RoomTypeDraft, ...]
