"""Inventory enums (LLD §5.1): SMALLINT in the database through ``EnumField``.

Other contexts import them from ``inventory.public.enums``, which re-exports this module.
"""

from hotel_commons.enum import Enum


class HoldStatus(Enum):
    HELD = 1
    CONFIRMED = 2
    RELEASED = 3


class ReleaseReason(Enum):
    CANCELLED = 1
    EXPIRED = 2
