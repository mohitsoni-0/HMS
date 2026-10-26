"""Charge request fingerprints (LLD §9.5): what a payment's idempotency key was first used for.

The fingerprint is an HMAC-SHA256 (hex, 64 characters) under the service's fingerprint key over the
canonical JSON of ``{"method": NAME, "details": details}``. It is stored on the payment instead of the
details themselves, so no card data is kept, and a retry with the same key but another method or instrument
is told apart from a true replay.
"""

import enum
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any


class RequestFingerprinter:
    def __init__(self, key: bytes) -> None:
        self._key = key

    def fingerprint(self, method: object, details: Mapping[str, Any]) -> str:
        """The fingerprint of a charge with ``method`` (a ``PaymentMethod`` member or name) and ``details``."""
        canonical = json.dumps(
            {"method": _name(method), "details": dict(details)},
            default=_name,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hmac.new(self._key, canonical.encode(), hashlib.sha256).hexdigest()


def _name(value: object) -> str:
    """Enum members by name (the wire form), anything else JSON cannot encode as its text."""
    return value.name if isinstance(value, enum.Enum) else str(value)
