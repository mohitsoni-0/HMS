"""Catalog enums shared with other contexts (LLD §4.1).

SMALLINT in the database through the model ``EnumField``, member names on the wire through ``HotelEnumField``.
Pricing, discovery and booking import these from here; ``catalog.enums`` re-exports them for the app itself.
"""

from hotel_commons.enum import Enum


class PropertyType(Enum):
    HOTEL = 1
    RESORT = 2
    APARTMENT = 3
    VILLA = 4
    HOSTEL = 5
    HOMESTAY = 6


class PricingStrategyCode(Enum):
    STANDARD = 1
    WEEKEND = 2
    DYNAMIC = 3


class CancellationPolicyCode(Enum):
    FLEXIBLE = 1
    MODERATE = 2
    STRICT = 3
    NON_REFUNDABLE = 4


class AmenityCategory(Enum):
    PROPERTY = 1
    ROOM = 2
