"""Payment status codes, module 06 (LLD §9.9). Messages name fields and states, never card or PII values."""

from hotel_commons.status_code import StatusCode

PAYMENT_NOT_FOUND = StatusCode(2106001, "Payment not found", 404)
INVALID_PAYMENT_DETAILS = StatusCode(2106002, "Invalid {reason}", 400)
UNSUPPORTED_PAYMENT_METHOD = StatusCode(2106003, "Unsupported payment method", 400)
REFUND_EXCEEDS_PAYMENT = StatusCode(2106004, "Refund exceeds the refundable amount of the payment", 422)
PAYMENT_NOT_REFUNDABLE = StatusCode(2106005, "A payment that is {state} cannot be refunded", 422)
INVALID_PAYMENT_TRANSITION = StatusCode(2106006, "Cannot move a payment from {current} to {target}", 409)
REFUND_FAILED = StatusCode(2106007, "Refund could not be processed, please retry", 502)
