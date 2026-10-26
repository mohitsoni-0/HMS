"""Fixed rules and names of the booking context. Configurable limits live in ``BookingSettings`` instead."""

from typing import Final

MAX_ROOMS_PER_BOOKING: Final = 10
MAX_GUESTS_PER_BOOKING: Final = 40
GUEST_NAME_MIN_LENGTH: Final = 2
GUEST_NAME_MAX_LENGTH: Final = 100
TEXT_MAX_LENGTH: Final = 255  # cancellation reason, last payment failure
USER_ID_MAX_LENGTH: Final = 64  # JWT ``sub``

REFUND_REASON: Final = "booking cancelled"
EXPIRE_BATCH_SIZE: Final = 100

BOOKING_RESOURCE: Final = "booking"  # IDOR resource name
PROPERTY_RESOURCE: Final = "property"

IDEMPOTENCY_SCOPE_CREATE: Final = "booking_create"
IDEMPOTENCY_SCOPE_PAY: Final = "booking_pay"
IDEMPOTENCY_SCOPE_CANCEL: Final = "booking_cancel"
THROTTLE_SCOPE_WRITE: Final = "booking_write"
THROTTLE_SCOPE_PAYMENT: Final = "payment"
