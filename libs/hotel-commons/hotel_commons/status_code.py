"""Status-code catalogue shared by every app (LLD §3.2).

A code reads ``SS MM EEE``: service (21), module (00 common, 01 catalog, 02 inventory, 03 pricing,
04 discovery, 05 booking, 06 payment) and error number. Each ``StatusCode`` carries its HTTP status, so code
that raises an error never picks one. Apps keep their own catalogue in ``<app>/status_code.py``.
"""

from dataclasses import dataclass
from typing import Final

from hotel_commons.consts import HOTEL_SERVICE_NAME, SERVICE_CODES


class _SafeDict(dict[str, object]):
    """``format_map`` mapping that leaves an unknown placeholder as ``{name}`` instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True, slots=True)
class StatusCode:
    code: int
    message: str
    http_status: int = 400

    def render(self, **params: object) -> str:
        """The message with ``params`` filled in; missing placeholders stay visible rather than failing."""
        return self.message.format_map(_SafeDict(params))


SERVICE_CODE: Final = SERVICE_CODES[HOTEL_SERVICE_NAME]  # 21

SUCCESS = StatusCode(2100000, "Success", 200)
VALIDATION_FAILED = StatusCode(2100001, "Invalid request details", 400)
UNAUTHENTICATED = StatusCode(2100002, "Authentication credentials are missing or invalid", 401)
TOKEN_EXPIRED = StatusCode(2100003, "Token has expired", 401)
FORBIDDEN = StatusCode(2100004, "You are not allowed to perform this action", 403)
FIELD_ACCESS_DENIED = StatusCode(2100005, "Field '{field}' cannot be set by your role", 403)
RESOURCE_NOT_FOUND = StatusCode(2100006, "{resource} not found", 404)
RATE_LIMITED = StatusCode(2100007, "Too many requests, retry after {retry_after}s", 429)
IDEMPOTENCY_KEY_REQUIRED = StatusCode(2100008, "Idempotency-Key header is required", 400)
IDEMPOTENCY_KEY_INVALID = StatusCode(2100009, "Idempotency-Key must be 8-128 characters of [A-Za-z0-9_-]", 400)
IDEMPOTENCY_IN_PROGRESS = StatusCode(2100010, "A request with this Idempotency-Key is still in progress", 409)
IDEMPOTENCY_KEY_REUSED = StatusCode(2100011, "Idempotency-Key was already used with a different request", 422)
PAYLOAD_NOT_ENCRYPTED = StatusCode(2100012, "Payload should be encrypted", 400)
PAYLOAD_DECRYPTION_FAILED = StatusCode(2100013, "Encrypted payload could not be decrypted", 400)
SECURITY_ERROR = StatusCode(2100014, "Please try again after some time", 500)
INTERNAL_ERROR = StatusCode(2100015, "Please try again after some time", 500)
LOG_MODEL_NOT_FOUND = StatusCode(2100016, "Invalid log model configured for {model}", 500)
METHOD_NOT_ALLOWED = StatusCode(2100017, "Method {method} not allowed", 405)
NOT_FOUND = StatusCode(2100018, "Resource not found", 404)
RESOURCE_BUSY = StatusCode(2100019, "The resource is busy, please retry", 409)
SERVICE_TIMEOUT = StatusCode(2100020, "The request took too long, please retry", 503)
