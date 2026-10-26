"""Library-wide constants: service identity, environments, wire-protocol names and key prefixes.

Pure values with no imports beyond the standard library, so any module (domain included) may depend on
this one without pulling in Django.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

HOTEL_SERVICE_NAME: Final = "hotel"

# Service name -> the two-digit service code "SS" of every "SS MM EEE" status code (like kuhoo's ERROR_CONFIG).
SERVICE_CODES: Final[Mapping[str, int]] = MappingProxyType({HOTEL_SERVICE_NAME: 21})

ENVIRONMENT_LOCAL: Final = "local"
ENVIRONMENT_PRODUCTION: Final = "production"

API_ENCRYPTION_OFF: Final = "off"
API_ENCRYPTION_OPTIONAL: Final = "optional"
API_ENCRYPTION_ENFORCED: Final = "enforced"
API_ENCRYPTION_MODES: Final = (API_ENCRYPTION_OFF, API_ENCRYPTION_OPTIONAL, API_ENCRYPTION_ENFORCED)

REDIS_KEY_PREFIX: Final = "hb"

# HTTP headers of the API contract (LLD §3.7-§3.11).
REQUEST_ID_HEADER: Final = "X-Request-ID"
IDEMPOTENCY_KEY_HEADER: Final = "Idempotency-Key"
IDEMPOTENT_REPLAYED_HEADER: Final = "Idempotent-Replayed"
PAYLOAD_ENCRYPTION_HEADER: Final = "X-Payload-Encryption"
ENCRYPTED_KEY_HEADER: Final = "X-Encrypted-Key"
PAYLOAD_TIMESTAMP_HEADER: Final = "X-Payload-Timestamp"
RETRY_AFTER_HEADER: Final = "Retry-After"
RATE_LIMIT_LIMIT_HEADER: Final = "X-RateLimit-Limit"
RATE_LIMIT_REMAINING_HEADER: Final = "X-RateLimit-Remaining"
