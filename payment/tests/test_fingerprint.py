"""Charge request fingerprints: stable for the same request, different for any other, never the raw details.

Pure unit tests: no Django, no database.
"""

import re

from payment.domain.fingerprint import RequestFingerprinter
from payment.enums import PaymentMethod, WalletProvider

CARD = {"card_number": "4242424242424242", "expiry_month": 12, "expiry_year": 2028, "cvv": "987"}


def test_fingerprint_is_hex_sha256_and_hides_the_details() -> None:
    fingerprint = RequestFingerprinter(b"k" * 32).fingerprint("CARD", CARD)
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    assert "4242" not in fingerprint


def test_fingerprint_ignores_key_order_and_enum_spelling() -> None:
    fingerprinter = RequestFingerprinter(b"k" * 32)
    wallet = {"provider": "PAYTM", "phone": "9876543210"}

    assert fingerprinter.fingerprint("CARD", CARD) == fingerprinter.fingerprint("CARD", dict(reversed(CARD.items())))
    assert fingerprinter.fingerprint("WALLET", wallet) == fingerprinter.fingerprint(
        PaymentMethod.WALLET, {"phone": "9876543210", "provider": WalletProvider.PAYTM}
    )


def test_fingerprint_differs_by_method_details_and_key() -> None:
    fingerprinter = RequestFingerprinter(b"k" * 32)
    card = fingerprinter.fingerprint("CARD", CARD)

    assert fingerprinter.fingerprint("UPI", CARD) != card
    assert fingerprinter.fingerprint("CARD", {**CARD, "cvv": "123"}) != card
    assert RequestFingerprinter(b"j" * 32).fingerprint("CARD", CARD) != card
