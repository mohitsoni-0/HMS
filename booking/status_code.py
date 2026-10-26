"""Status codes of the booking context, 2105xxx (LLD §8.8)."""

from hotel_commons.status_code import StatusCode

BOOKING_NOT_FOUND = StatusCode(2105001, "Booking not found", 404)
INVALID_STATE_TRANSITION = StatusCode(2105002, "Cannot {action} a booking that is {state}", 409)
BOOKING_EXPIRED = StatusCode(2105003, "Booking hold expired; please book again", 409)
PAYMENT_DECLINED = StatusCode(2105004, "Payment declined: {reason}", 402)
INVALID_BOOKING_REQUEST = StatusCode(2105005, "{reason}", 400)
ROOM_TYPE_NOT_BOOKABLE = StatusCode(2105006, "This room type is not available for booking", 422)
CANCELLATION_WINDOW_CLOSED = StatusCode(2105007, "Bookings cannot be cancelled after check-in", 422)
GUEST_CAPACITY_EXCEEDED = StatusCode(2105008, "{guests} guests exceed capacity of {capacity}", 422)
BOOKING_NOT_PAYABLE = StatusCode(2105009, "Pay-at-property bookings are paid at the property", 422)
UNSUPPORTED_CANCELLATION_POLICY = StatusCode(2105010, "Cancellation policy {policy} is not supported", 422)
LIST_FILTER_REQUIRED = StatusCode(2105011, "property_id is required for owners", 400)
