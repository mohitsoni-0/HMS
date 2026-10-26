"""Pricing status codes (module 03, 2103xxx), LLD §6."""

from hotel_commons.status_code import StatusCode

UNSUPPORTED_PRICING_STRATEGY = StatusCode(2103001, "Pricing strategy {strategy} is not supported", 422)
