"""Inventory constants. Pure values, importable from the domain."""

from typing import Final

# Nights provisioned ahead of today when INVENTORY_HORIZON_DAYS is not configured (LLD §5.4, §13).
DEFAULT_HORIZON_DAYS: Final = 365

# Length of inventory.hold.reference: the id of whatever the hold is for (a booking id today).
MAX_REFERENCE_LENGTH: Final = 64
