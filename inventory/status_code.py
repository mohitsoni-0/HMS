"""Inventory status codes, module 02 of ``21 MM EEE`` (LLD §5.7)."""

from hotel_commons.status_code import StatusCode

INVENTORY_UNAVAILABLE = StatusCode(2102001, "Not enough rooms available for the selected dates", 409)
HOLD_NOT_FOUND = StatusCode(2102002, "Inventory hold not found", 404)
STAY_OUTSIDE_HORIZON = StatusCode(2102003, "Bookings can be made up to {days} days ahead", 422)
INVALID_HOLD_TRANSITION = StatusCode(2102004, "Hold is {state}", 409)
