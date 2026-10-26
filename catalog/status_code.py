"""Catalog status codes, module 01 of ``21 MM EEE`` (LLD §4.8)."""

from hotel_commons.status_code import StatusCode

OWNER_ACCOUNT_NOT_FOUND = StatusCode(2101001, "Owner account not found", 404)
PROPERTY_NOT_FOUND = StatusCode(2101002, "Property not found", 404)
ROOM_TYPE_NOT_FOUND = StatusCode(2101003, "Room type not found", 404)
UNKNOWN_AMENITIES = StatusCode(2101004, "Unknown amenity codes: {codes}", 400)
DUPLICATE_ROOM_TYPE_NAME = StatusCode(2101005, "Room type '{name}' already exists", 409)
PROPERTY_TYPE_RULE_VIOLATED = StatusCode(2101006, "{reason}", 422)
INVALID_PROPERTY = StatusCode(2101007, "{reason}", 400)
ROOM_TYPE_REQUIRED = StatusCode(2101008, "At least one room type is required", 400)
INVALID_CONTACT = StatusCode(2101009, "{reason}", 400)
OWNER_ACCOUNT_EXISTS = StatusCode(2101010, "An owner account with this contact email already exists", 409)
