"""How a payment was made (LLD §9.2): a masked display, an optional sensitive reference, a transient payload.

Card data never leaves the request: it travels only in ``ParsedInstrument.gateway_payload`` to the gateway
and is never persisted or logged. ``sensitive_ref`` (UPI VPA or wallet phone) is stored encrypted.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from payment.enums import PaymentMethod


@dataclass(frozen=True, slots=True)
class PaymentInstrument:
    method: PaymentMethod
    display: str  # "VISA •••• 4242" | "r****@okaxis" | "PAYTM ******3210"
    sensitive_ref: str | None = field(default=None, repr=False)  # UPI VPA / wallet phone; never card data


@dataclass(frozen=True, slots=True)
class ParsedInstrument:
    instrument: PaymentInstrument
    gateway_payload: Mapping[str, str] = field(repr=False)  # transient; never persisted or logged
