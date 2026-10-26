"""Inventory enums other contexts may use (LLD §5.5), re-exported from ``inventory.enums``. No Django imports."""

from inventory.enums import HoldStatus, ReleaseReason

__all__ = ["HoldStatus", "ReleaseReason"]
